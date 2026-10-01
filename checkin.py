"""The evening check-in, independent of any chat app.

Everything here returns *screens* (text + button rows) and handles *button data*.
bot.py turns screens into Telegram messages; a WhatsApp adapter could do the same
without touching this file. That split also means all of this is testable without
Telegram.

Stateless buttons
Every button carries everything answered so far in its data, e.g.
    "c:14:done:45:4"  = session 14, outcome done, 45 minutes, confidence 4
so the bot never has to remember where you are in the check-in. If it restarts
halfway through, the buttons on your screen still work. (Same idea as a stateless
HTTP request carrying all it needs. Telegram limits button data to 64 bytes.)

Button kinds: o/m/c/f = check-in steps, p = planner proposal, k = review suggestion.

Flow per session:  outcome -> minutes -> confidence -> [item finished?] -> logged
  - skipped: logged straight away (0 minutes, no confidence)
  - "item finished?" only when outcome is done and the item isn't open-ended
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import db

OPEN_ENDED_MINUTES = 1000     # items estimated at this or more are never "finished"
FMT = "%Y-%m-%d %H:%M"


@dataclass
class Screen:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)   # rows of (label, data)


@dataclass
class Result:
    screen: Screen            # replaces the message the button was on
    finished: bool = False    # True when the last session has been logged
    changed_plan: bool = False   # a proposal approval changed sessions/items -> sync


# ---------------------------------------------------------------- helpers

def _session(conn, session_id: int) -> dict | None:
    row = conn.execute(
        """SELECT s.*, i.title, i.est_minutes, t.name AS track
           FROM sessions s JOIN items i ON i.item_id = s.item_id
           JOIN tracks t ON t.track_id = i.track_id
           WHERE s.session_id = ?""", (session_id,)).fetchone()
    return dict(row) if row else None


def _planned_minutes(s: dict) -> int:
    return int((datetime.strptime(s["end_at"], FMT) - datetime.strptime(s["start_at"], FMT))
               .total_seconds() // 60)


def _heading(s: dict) -> str:
    return f"{s['start_at'][11:]}–{s['end_at'][11:]} · {s['track']}\n{s['title']}"


def _already_logged(conn, session_id: int) -> bool:
    return conn.execute("SELECT 1 FROM completions WHERE session_id = ?",
                        (session_id,)).fetchone() is not None


def minute_options(planned: int) -> list[int]:
    """Planned length plus a few common alternatives, up to double the plan."""
    opts = {15, 30, 45, 60, 90, 120, planned}
    return sorted(m for m in opts if m <= max(planned * 2, 30))


# ---------------------------------------------------------------- screens

def next_screen(conn, day: str | None = None) -> Screen | None:
    """The question for the next unlogged session today, or None if all logged."""
    todo = db.unlogged_sessions(conn, day)
    if not todo:
        return None
    s = _session(conn, todo[0]["session_id"])
    left = f"  ({len(todo)} left)" if len(todo) > 1 else ""
    sid = s["session_id"]
    return Screen(f"{_heading(s)}\n\nHow did it go?{left}",
                  [[("✅ Done", f"o:{sid}:done"), ("◐ Partial", f"o:{sid}:partial"),
                    ("✗ Skipped", f"o:{sid}:skipped")]])


def _minutes_screen(s: dict, outcome: str) -> Screen:
    planned = _planned_minutes(s)
    sid = s["session_id"]
    buttons = [(f"{m}{' (plan)' if m == planned else ''}", f"m:{sid}:{outcome}:{m}")
               for m in minute_options(planned)]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return Screen(f"{_heading(s)}\n\n{outcome.capitalize()}. How many minutes?", rows)


def _confidence_screen(s: dict, outcome: str, minutes: int) -> Screen:
    sid = s["session_id"]
    return Screen(f"{_heading(s)}\n\n{outcome.capitalize()}, {minutes} min. "
                  f"Confidence? (1 = lost, 5 = solid)",
                  [[(str(c), f"c:{sid}:{outcome}:{minutes}:{c}") for c in range(1, 6)]])


def _finished_screen(s: dict, outcome: str, minutes: int, conf: int) -> Screen:
    sid = s["session_id"]
    return Screen(f"{_heading(s)}\n\nIs the whole item finished, or is there more to do?",
                  [[("Finished", f"f:{sid}:{outcome}:{minutes}:{conf}:1"),
                    ("More to do", f"f:{sid}:{outcome}:{minutes}:{conf}:0")]])


def proposal_screens(conn) -> list[Screen]:
    """One screen per pending proposal, each with Approve / Reject."""
    screens = []
    for p in db.pending_proposals(conn):
        screens.append(Screen(
            f"The planner asks: {describe_proposal(conn, p)}\nWhy: {p['reason']}",
            [[("Approve", f"p:{p['proposal_id']}:1"), ("Reject", f"p:{p['proposal_id']}:0")]]))
    return screens


def change_screens(conn) -> list[Screen]:
    """One screen per instruction change the weekly review suggested."""
    return [Screen(f"Review suggests: {db.describe_change(conn, ch)}\nWhy: {ch['reason']}",
                   [[("Approve", f"k:{ch['change_id']}:1"), ("Reject", f"k:{ch['change_id']}:0")]])
            for ch in db.pending_changes(conn)]


def describe_proposal(conn, p: dict) -> str:
    if p["action"] == "cancel_session":
        s = _session(conn, p["target_id"])
        return (f"cancel {s['title']} on {s['start_at']}" if s else
                f"cancel session {p['target_id']}")
    if p["action"] == "pause_track":
        row = conn.execute("SELECT name FROM tracks WHERE track_id = ?",
                           (p["target_id"],)).fetchone()
        return f"pause the {row['name'] if row else p['target_id']} track"
    item = db.get_item(conn, p["target_id"])
    return f"mark '{item['title'] if item else p['target_id']}' as finished"


# ---------------------------------------------------------------- button presses

def handle(conn, data: str, day: str | None = None) -> Result:
    """React to a button press. Returns the screen that replaces the pressed message."""
    kind, *parts = data.split(":")

    if kind == "p":                                   # proposal decision
        pid, approve = int(parts[0]), parts[1] == "1"
        try:
            p = db.decide_proposal(conn, pid, approve)
        except Exception as e:                        # already decided, locked, gone
            return Result(Screen(f"Couldn't do that: {e}"))
        verdict = "Approved" if approve else "Rejected"
        return Result(Screen(f"{verdict}: {describe_proposal(conn, p)}"),
                      changed_plan=approve)

    if kind == "k":                                   # instruction change decision
        cid, approve = int(parts[0]), parts[1] == "1"
        try:
            ch = db.decide_change(conn, cid, approve)
        except Exception as e:                        # already decided, no longer valid
            return Result(Screen(f"Couldn't do that: {e}"))
        text = f"{'Approved' if approve else 'Rejected'}: {db.describe_change(conn, ch)}"
        if ch["sessions_cancelled"]:
            text += (f"\n{ch['sessions_cancelled']} planned session(s) in that window were "
                     f"cancelled; tonight's re-plan will rebook them (or /plan now).")
        return Result(Screen(text), changed_plan=ch["sessions_cancelled"] > 0)

    sid = int(parts[0])
    s = _session(conn, sid)
    if s is None:
        return Result(Screen("That session no longer exists."))
    if _already_logged(conn, sid):
        return _after_log(conn, day, f"Already logged: {s['title']}")

    outcome = parts[1]
    if kind == "o":
        if outcome == "skipped":
            db.log_completion(conn, s["item_id"], "skipped", session_id=sid)
            return _after_log(conn, day, f"Skipped: {s['title']}")
        return Result(_minutes_screen(s, outcome))

    minutes = int(parts[2])
    if kind == "m":
        return Result(_confidence_screen(s, outcome, minutes))

    conf = int(parts[3])
    open_ended = s["est_minutes"] >= OPEN_ENDED_MINUTES
    if kind == "c" and outcome == "done" and not open_ended:
        return Result(_finished_screen(s, outcome, minutes, conf))

    # kind "c" (partial, or open-ended) or "f" (answered "finished?")
    finished = kind == "f" and parts[4] == "1"
    db.log_completion(conn, s["item_id"], outcome, minutes, conf, session_id=sid,
                      mark_item_done=finished)
    note = " (item finished)" if finished else ""
    return _after_log(conn, day, f"Logged: {s['title']}, {outcome}, {minutes} min, "
                                 f"confidence {conf}{note}")


def _after_log(conn, day: str | None, logged: str) -> Result:
    nxt = next_screen(conn, day)
    if nxt is None:
        return Result(Screen(f"{logged}\n\nThat's everything for today. Re-planning..."),
                      finished=True)
    return Result(Screen(f"{logged}\n\n{nxt.text}", nxt.buttons))
