# discourse-ics-sync-tags

> **ICS → Discourse topic sync**  
> Extended version of the simple [ICS sync script](https://meta.discourse.org/t/syncing-ical-ics-feeds-into-discourse-topics-simple-python-script-cron-friendly/379361) with support for **preserving manual tags**.

---

## ✨ Features

- **Idempotent sync**: one Discourse topic per ICS `UID`.
- **Preserves human-edited titles** (never overwritten).
- **Never moves category** once created.
- **Preserves + merges tags**  
  (keeps any tags added by moderators, while ensuring static/default/UID tags stay).
- Updates the **first post content** only if it changed.
- Embeds a hidden marker (`<!-- ICSUID:xxxx -->`) so updates always match the right topic.

---

## ⚙️ Requirements

Python 3.9+  
Dependencies:
- [`requests`](https://pypi.org/project/requests/)  
- [`python-dateutil`](https://pypi.org/project/python-dateutil/)  
- [`icalendar`](https://pypi.org/project/icalendar/)  

Install:
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install requests python-dateutil icalendar
