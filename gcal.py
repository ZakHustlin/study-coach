"""Google Calendar: the agent's own "Study (agent)" calendar.

Named gcal.py, not calendar.py, because that would shadow Python's built-in
`calendar` module for every file in the project.

Key decisions
- Scope `calendar.app.created`: the app can create calendars and manage events on
  the calendars IT created, and nothing else. It can't read or change your other
  calendars even if it had a bug. Least privilege.
- One-way sync, database -> calendar. The database is the source of truth; the
  calendar is just a view of the `sessions` table.
- Re-planning never edits events: the planner cancels a session and plans a new one;
  sync deletes the old event and creates the new one.
- Each event stores its session_id in a private extended property, so an event can
  always be traced back to its row.

Commands
    python gcal.py auth        one-off: sign in with Google, saves token.json
    python gcal.py setup       one-off: create the "Study (agent)" calendar
    python gcal.py sync        push planned/cancelled sessions to Google
    python gcal.py free        print free slots for the next 7 days
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import availability
import db

ROOT = Path(__file__).parent
SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
CREDENTIALS = ROOT / "credentials.json"   # OAuth client, downloaded from Google Cloud
TOKEN = ROOT / "token.json"               # your saved sign-in (created by `auth`)
STATE = ROOT / "gcal_state.json"          # remembers the Study calendar's id
CALENDAR_NAME = "Study (agent)"
FMT = "%Y-%m-%d %H:%M"


# ---------------------------------------------------------------- auth

def authorise() -> None:
    """Sign in once and save token.json.

    Uses the copy-paste flow rather than a local web server: after you approve,
    Google redirects to http://localhost/?code=..., which fails to load in a browser
    Codespace, but the code is in the URL. You paste that URL back here.
    """
    from google_auth_oauthlib.flow import Flow

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # the redirect is plain http://localhost
    flow = Flow.from_client_secrets_file(str(CREDENTIALS), SCOPES,
                                         redirect_uri="http://localhost")
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("\n1. Open this link and approve access:\n\n" + url + "\n")
    print("2. You'll land on a page that won't load. Copy its full URL from the address bar.")
    flow.fetch_token(authorization_response=input("\n3. Paste it here: ").strip())
    TOKEN.write_text(flow.credentials.to_json())
    print(f"\nSaved {TOKEN.name}. Next: python gcal.py setup")


def service():
    """An authorised Calendar API client, refreshing the access token when needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN.exists():
        raise SystemExit("No token.json. Run: python gcal.py auth")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        creds.refresh(Request())          # uses the refresh token; no browser needed
        TOKEN.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------- calendar

def calendar_id() -> str:
    if not STATE.exists():
        raise SystemExit("No Study calendar yet. Run: python gcal.py setup")
    return json.loads(STATE.read_text())["calendar_id"]


def setup_calendar(svc, tz: str) -> str:
    """Create the Study (agent) calendar once and remember its id."""
    if STATE.exists():
        return calendar_id()
    cal = svc.calendars().insert(body={"summary": CALENDAR_NAME, "timeZone": tz}).execute()
    STATE.write_text(json.dumps({"calendar_id": cal["id"]}))
    return cal["id"]


def event_body(session: dict, tz: str) -> dict:
    def iso(s: str) -> str:
        return datetime.strptime(s, FMT).isoformat()

    footer = f"Planned by study coach (session {session['session_id']})."
    brief = (session.get("brief") or "").strip()
    return {
        "summary": f"{session['track']}: {session['title']}",
        "description": f"{brief}\n\n{footer}" if brief else footer,
        "start": {"dateTime": iso(session["start_at"]), "timeZone": tz},
        "end":   {"dateTime": iso(session["end_at"]),   "timeZone": tz},
        "extendedProperties": {"private": {"session_id": str(session["session_id"])}},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 5}]},
    }


def sync(conn, svc, cal_id: str, tz: str, now: str | None = None) -> dict:
    """Make the calendar match the database. Safe to run as often as you like
    (idempotent): a second run straight after the first does nothing."""
    from googleapiclient.errors import HttpError

    created = removed = 0
    for s in db.sessions_to_create(conn, now):
        event = svc.events().insert(calendarId=cal_id, body=event_body(s, tz)).execute()
        db.set_calendar_event_id(conn, s["session_id"], event["id"])
        created += 1

    for s in db.sessions_to_remove(conn):
        try:
            svc.events().delete(calendarId=cal_id, eventId=s["calendar_event_id"]).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):   # already gone (you deleted it by hand)
                raise
        db.clear_calendar_event_id(conn, s["session_id"])
        removed += 1

    return {"created": created, "removed": removed}


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    import argparse
    from datetime import date, timedelta

    p = argparse.ArgumentParser(description="Google Calendar sync")
    p.add_argument("command", choices=["auth", "setup", "sync", "free"])
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()
    config = availability.load_config()
    tz = config["timezone"]

    if args.command == "auth":
        authorise()
    elif args.command == "setup":
        print(f"Study calendar ready: {setup_calendar(service(), tz)}")
    elif args.command == "sync":
        print(sync(db.connect(), service(), calendar_id(), tz))
    else:
        conn = db.connect()
        start = date.today()
        end = (start + timedelta(days=args.days)).isoformat()
        planned = db.sessions_between(conn, start.isoformat(), end)
        for slot in availability.free_slots_range(start, args.days, config, planned):
            print(f"{slot['start']} -> {slot['end'][11:]}  ({slot['minutes']} min)")
