#!/usr/bin/env python3
"""
Sync ICS -> Discourse topics (create/update by UID).

Key behaviors:
- Idempotent: one topic per ICS UID.
- Preserves human-edited titles on update.
- Does NOT change category on update.
- **Merges tags** on update (never drops existing manual tags).
- Updates the first post only when content changes (ignores marker).
- Adds an invisible marker to the first post to find the topic next time.

Env (recommended):
  DISCOURSE_BASE_URL       e.g. "https://forum.example.com"
  DISCOURSE_API_KEY        your admin/mod API key
  DISCOURSE_API_USERNAME   e.g. "system" or your staff username
  DISCOURSE_CATEGORY_ID    default numeric category id for CREATE only (override with --category-id)
  DISCOURSE_DEFAULT_TAGS   comma separated list, e.g. "calendar,events"

Usage examples:
  python3 ics_to_discourse.py --ics my.ics --category-id 12
  python3 ics_to_discourse.py --ics https://example.com/cal.ics --static-tags calendar,google
"""

import os, sys, argparse, logging, hashlib, json, re
from datetime import datetime
from dateutil import tz
from icalendar import Calendar
from urllib.parse import urlparse
import requests

log = logging.getLogger("ics2disc")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# --------- Config from environment ----------
BASE        = os.environ.get("DISCOURSE_BASE_URL", "").rstrip("/")
API_KEY     = os.environ.get("DISCOURSE_API_KEY", "")
API_USER    = os.environ.get("DISCOURSE_API_USERNAME", "system")
ENV_CAT_ID  = os.environ.get("DISCOURSE_CATEGORY_ID", "")
DEFAULT_TAGS= [t.strip() for t in os.environ.get("DISCOURSE_DEFAULT_TAGS", "").split(",") if t.strip()]

# --------- HTTP helpers ----------
def session():
    if not BASE or not API_KEY or not API_USER:
        log.error("Missing DISCOURSE_* env vars. Need DISCOURSE_BASE_URL, DISCOURSE_API_KEY, DISCOURSE_API_USERNAME.")
        sys.exit(2)
    s = requests.Session()
    s.headers.update({
        "Api-Key": API_KEY,
        "Api-Username": API_USER,
        "Accept": "application/json"
    })
    return s

def get_json(s, path, **params):
    r = s.get(f"{BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def post_json(s, path, data):
    r = s.post(f"{BASE}{path}", data=data, timeout=60)
    if r.status_code >= 400:
        log.error("POST %s failed: %s\nBody: %s", path, r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()

def put_json(s, path, data):
    r = s.put(f"{BASE}{path}", data=data, timeout=60)
    if r.status_code >= 400:
        log.error("PUT %s failed: %s\nBody: %s", path, r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()

# --------- Discourse helpers ----------
def search_topic_by_marker(s, marker_token):
    """
    Uses Discourse search to find topics containing our exact marker token.
    Returns topic_id or None.
    """
    q = f'"{marker_token}"'
    data = get_json(s, "/search.json", q=q)
    topics = data.get("topics", [])
    if not topics:
        # Sometimes the marker might only be in the first post raw; search the posts index too:
        # search.json also returns 'posts' with topic_id
        posts = data.get("posts", [])
        if posts:
            return posts[0].get("topic_id")
        return None
    return topics[0].get("id")

def read_topic_full(s, topic_id):
    return get_json(s, f"/t/{topic_id}.json", include_raw="true")

def first_post_id_and_raw(topic_json):
    posts = topic_json.get("post_stream", {}).get("posts", [])
    if not posts:
        return None, ""
    p0 = posts[0]
    return p0.get("id"), p0.get("raw", "")

def update_first_post_raw(s, post_id, new_raw):
    return put_json(s, f"/posts/{post_id}.json", {"post[raw]": new_raw})

def create_topic(s, category_id, title, raw, tags):
    payload = {
        "title": title,
        "raw": raw,
        "category": category_id,
    }
    # tags[] must be repeated for Discourse
    for i, t in enumerate(tags):
        payload[f"tags[{i}]"] = t
    j = post_json(s, "/posts.json", payload)
    return j.get("topic_id")

def update_topic_tags(s, topic_id, merged_tags):
    # PUT /t/{id}.json requires title or category if changing; but we can send just tags[]
    payload = {}
    for i, t in enumerate(merged_tags):
        payload[f"tags[{i}]"] = t
    return put_json(s, f"/t/{topic_id}.json", payload)

# --------- ICS helpers ----------
def read_ics(path_or_url):
    if re.match(r"^https?://", path_or_url, re.I):
        r = requests.get(path_or_url, timeout=60)
        r.raise_for_status()
        return Calendar.from_ical(r.content)
    else:
        with open(path_or_url, "rb") as f:
            return Calendar.from_ical(f.read())

def to_local_iso(dt, tzname="Europe/London"):
    """
    Return 'YYYY-MM-DD HH:MM' in provided timezone. Accepts date or datetime.
    """
    target = tz.gettz(tzname)
    if hasattr(dt, "dt"):
        dt = dt.dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            # assume UTC if floating, then convert
            dt = dt.replace(tzinfo=tz.UTC)
        dt = dt.astimezone(target)
    else:
        # date: make naive 00:00 local
        dt = datetime(dt.year, dt.month, dt.day, 0, 0, tzinfo=target)
    return dt.strftime("%Y-%m-%d %H:%M")

def short_uid_tag(uid):
    # Keep tag short for Discourse limits; stable hash
    h = hashlib.sha1(uid.encode("utf-8")).hexdigest()[:10]
    return f"ics-{h}"

def build_marker(uid):
    # Short token that we can search verbatim
    return f"ICSUID:{hashlib.sha1(uid.encode('utf-8')).hexdigest()[:16]}"

def strip_marker(raw):
    return re.sub(r"<!--\s*ICSUID:[0-9a-f]{16}\s*-->\s*", "", raw or "", flags=re.I)

def make_event_block(ev, site_tz, include_details=True):
    uid = str(ev.get("UID"))
    summary = str(ev.get("SUMMARY", "Untitled event"))
    location = str(ev.get("LOCATION", "")).strip()
    url = str(ev.get("URL", "")).strip()
    desc = str(ev.get("DESCRIPTION", "")).strip()

    dtstart = ev.get("DTSTART")
    dtend = ev.get("DTEND")
    start_str = to_local_iso(dtstart, site_tz) if dtstart else ""
    end_str = to_local_iso(dtend, site_tz) if dtend else ""

    event_open = f'[event start="{start_str}"'
    if end_str:
        event_open += f' end="{end_str}"'
    event_open += f' status="public" name="{summary}"'
    if location:
        event_open += f' location="{location}"'
    event_open += f' timezone="{site_tz}"]'

    body_lines = []
    if include_details:
        if location:
            body_lines.append(f"**Location:** {location}")
        if url:
            body_lines.append(f"**Link:** {url}")
        if desc:
            body_lines.append("")
            body_lines.append(desc)

    close = "[/event]"
    content = "\n".join([event_open] + body_lines + [close])
    return summary, content, uid

# --------- Main sync logic ----------
def sync_event(s, ev, args):
    site_tz = args.site_tz
    summary, event_block, uid = make_event_block(ev, site_tz)
    marker_token = build_marker(uid)
    marker_html = f"<!-- {marker_token} -->"

    # First post content to write
    fresh_raw = f"{marker_html}\n{event_block}\n"

    # Find existing topic by marker
    topic_id = search_topic_by_marker(s, marker_token)

    if topic_id:
        # Read existing topic
        topic = read_topic_full(s, topic_id)
        post_id, old_raw = first_post_id_and_raw(topic)

        # Compare bodies without the marker
        old_clean = strip_marker(old_raw)
        fresh_clean = strip_marker(fresh_raw)

        if old_clean.strip() != fresh_clean.strip():
            log.info("Updating topic %s first post.", topic_id)
            update_first_post_raw(s, post_id, fresh_raw)
        else:
            log.info("No body change for topic %s.", topic_id)

        # Merge tags (preserve any existing)
        existing_tags = topic.get("tags", []) or []
        desired_tags = set(existing_tags)  # start with existing to preserve manual tags
        desired_tags.update(DEFAULT_TAGS)
        desired_tags.update(args.static_tags)
        desired_tags.add(short_uid_tag(uid))  # ensure our UID tag sticks

        # Only push if changed
        if set(existing_tags) != desired_tags:
            merged = sorted(desired_tags)
            log.info("Merging tags on topic %s -> %s", topic_id, ", ".join(merged))
            update_topic_tags(s, topic_id, merged)
        else:
            log.info("Tags unchanged for topic %s.", topic_id)

        # DO NOT change title or category on update
        return topic_id, False

    # CREATE path
    category_id = args.category_id or ENV_CAT_ID
    if not category_id:
        log.error("Missing category id for CREATE (use --category-id or DISCOURSE_CATEGORY_ID). Skipping UID=%s", uid)
        return None, False

    # Tags for create = defaults + static + uid-tag
    tags = set()
    tags.update(DEFAULT_TAGS)
    tags.update(args.static_tags)
    tags.add(short_uid_tag(uid))
    tags = sorted(tags)

    title = summary  # Human may edit later; we won't change it next time
    topic_id = create_topic(s, category_id, title, fresh_raw, tags)
    log.info("Created topic %s for UID=%s", topic_id, uid)
    return topic_id, True

def main():
    ap = argparse.ArgumentParser(description="Sync an ICS into Discourse topics (idempotent by UID).")
    ap.add_argument("--ics", required=True, help="Path or URL to .ics")
    ap.add_argument("--category-id", help="Numeric category id (CREATE only; update never moves category)")
    ap.add_argument("--site-tz", default="Europe/London", help="Timezone name for rendering times (default: Europe/London)")
    ap.add_argument("--static-tags", default="", help="Comma separated static tags to add on create/update (merged with existing)")
    args = ap.parse_args()

    args.static_tags = [t.strip() for t in args.static_tags.split(",") if t.strip()]

    s = session()
    cal = read_ics(args.ics)

    count = 0
    created = 0
    for ev in cal.walk("VEVENT"):
        try:
            _, was_created = sync_event(s, ev, args)
            count += 1
            if was_created:
                created += 1
        except Exception as e:
            log.error("Error syncing event: %s", e, exc_info=True)

    log.info("Done. Processed %d events (%d created, %d updated).", count, created, count - created)

if __name__ == "__main__":
    main()
