#!/usr/bin/env python3
"""Helios v6 — iCloud Sync. Correct API calls for pyicloud v2.5.0."""
import json, os, sys
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.expanduser("~/.hermes/helios/data")
COOKIE_DIR = os.path.expanduser("~/.hermes/helios-v6/.icloud_session")
APPLE_ID = os.environ.get("ICLOUD_APPLE_ID", "")
os.makedirs(DATA_DIR, exist_ok=True)


def get_api():
    from pyicloud import PyiCloudService
    return PyiCloudService(APPLE_ID, "dummy", cookie_directory=COOKIE_DIR)


def sync_all():
    api = get_api()
    print(f"[icloud-sync] Session valid: {api.is_trusted_session}", file=sys.stderr)

    # ── Calendar ──────────────────────────────────────────
    try:
        from datetime import datetime as dt
        now = datetime.now(timezone.utc)
        from_dt = dt(now.year, now.month, now.day)
        to_dt = dt(now.year, now.month, now.day + 7) if now.day <= 23 else dt(now.year, now.month + 1, 1)
        
        # get_events returns all calendars' events — no calendar arg needed
        raw_events = api.calendar.get_events(from_dt, to_dt)
        events = []
        for evt in raw_events:
            start = getattr(evt, 'startDate', None)
            end = getattr(evt, 'endDate', None)
            if isinstance(start, list): start = start[1]
            if isinstance(end, list): end = end[1]
            events.append({
                "title": str(getattr(evt, 'title', '') or '')[:120],
                "start": str(start)[:19] if start else "",
                "end": str(end)[:19] if end else "",
                "all_day": bool(getattr(evt, 'allDay', False)),
                "location": str(getattr(evt, 'location', '') or '')[:120],
            })
        today_str = now.strftime("%Y-%m-%d")
        today_events = [e for e in events if e['start'][:10] == today_str]
        with open(os.path.join(DATA_DIR, "calendar_state.json"), "w") as f:
            json.dump({"source": "pyicloud", "synced_at": now.isoformat(),
                        "total_fetched": len(events), "today_count": len(today_events),
                        "today": today_events, "upcoming": events}, f, indent=2, default=str)
        print(f"  Calendar: {len(today_events)} today, {len(events)} upcoming", file=sys.stderr)
    except Exception as e:
        print(f"  Calendar: 0 events (empty or API: {e})", file=sys.stderr)
        with open(os.path.join(DATA_DIR, "calendar_state.json"), "w") as f:
            json.dump({"source": "pyicloud", "synced_at": datetime.now(timezone.utc).isoformat(),
                        "total_fetched": 0, "today_count": 0, "today": [], "upcoming": []}, f)

    # ── Reminders ─────────────────────────────────────────
    try:
        reminders = []
        all_lists = api.reminders.lists() if callable(api.reminders.lists) else api.reminders.lists
        if not callable(all_lists):
            lists = all_lists
        else:
            lists = all_lists()
        for lst in lists:
            lst_id = getattr(lst, 'id', str(lst))
            try:
                items = api.reminders.list_reminders(lst_id)
            except Exception:
                continue
            for item in items:
                reminders.append({
                    "list": str(getattr(getattr(item, 'list', None), 'title', '') or '')[:80],
                    "title": str(getattr(item, 'title', '') or '')[:120],
                    "completed": bool(getattr(item, 'completed', False)),
                    "due": str(getattr(item, 'due_date', '') or '')[:19],
                })
        overdue = sum(1 for r in reminders if r['due'] and r['due'] < now.isoformat() and not r['completed'])
        with open(os.path.join(DATA_DIR, "reminders_state.json"), "w") as f:
            json.dump({"source": "pyicloud", "synced_at": now.isoformat(),
                        "count": len(reminders), "overdue": overdue,
                        "reminders": reminders}, f, indent=2, default=str)
        print(f"  Reminders: {len(reminders)} total ({overdue} overdue)", file=sys.stderr)
    except Exception as e:
        print(f"  Reminders: ERROR - {e}", file=sys.stderr)

    # ── Contacts ──────────────────────────────────────────
    try:
        contacts_raw = api.contacts.all
        if callable(contacts_raw):
            all_contacts = contacts_raw()
        else:
            all_contacts = contacts_raw
        contacts = []
        for c in all_contacts:
            # pyicloud v2.5+ returns dicts, not objects
            if isinstance(c, dict):
                first = c.get("firstName", "") or ""
                last = c.get("lastName", "") or ""
                phones_raw = c.get("phones", []) or []
                phones = [p.get("field", "") for p in phones_raw if p.get("field")]
            else:
                first = getattr(c, "firstName", "") or ""
                last = getattr(c, "lastName", "") or ""
                phones_raw = getattr(c, "phones", []) or []
                phones = [p.get("field", "") if isinstance(p, dict) else str(p) for p in phones_raw]
            contacts.append({
                "firstName": first,
                "lastName": last,
                "name": f"{first} {last}".strip(),
                "phones": phones,
                "phone": phones[0] if phones else "",
                "contactId": c.get("contactId", "") if isinstance(c, dict) else "",
            })
        with open(os.path.join(DATA_DIR, "contacts_state.json"), "w") as f:
            json.dump({"source": "pyicloud", "synced_at": now.isoformat(),
                        "count": len(contacts), "contacts": contacts}, f, indent=2, default=str)
        print(f"  Contacts: {len(contacts)}", file=sys.stderr)
    except Exception as e:
        print(f"  Contacts: ERROR - {e}", file=sys.stderr)

    # ── Notes ─────────────────────────────────────────────
    try:
        folders = api.notes.folders() if callable(api.notes.folders) else api.notes.folders
        note_list = []
        for folder in folders:
            for note in api.notes.in_folder(folder):
                note_list.append({
                    "title": str(getattr(note, 'title', '') or '')[:120],
                    "folder": str(getattr(folder, 'name', '') or '')[:60],
                    "created": str(getattr(note, 'created', '') or '')[:19],
                })
                if len(note_list) >= 30:
                    break
            if len(note_list) >= 30:
                break
        with open(os.path.join(DATA_DIR, "notes_state.json"), "w") as f:
            json.dump({"source": "pyicloud", "synced_at": now.isoformat(),
                        "count": len(note_list), "notes": note_list}, f, indent=2, default=str)
        print(f"  Notes: {len(note_list)}", file=sys.stderr)
    except Exception as e:
        print(f"  Notes: ERROR - {e}", file=sys.stderr)

    # ── Mood (seed empty if doesn't exist) ────────────────
    mood_file = os.path.join(DATA_DIR, "mood_state.json")
    if not os.path.exists(mood_file):
        with open(mood_file, "w") as f:
            json.dump({"history": []}, f)

    print(f"[icloud-sync] ✅ Done", file=sys.stderr)


if __name__ == "__main__":
    sync_all()
