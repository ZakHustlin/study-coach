"""The planner's tools: what the LLM can look at and do.

Each tool has two halves:
  - a SCHEMA (name, description, JSON Schema for the arguments). This is all the
    model ever sees. The descriptions are part of the prompt, so they matter.
  - a FUNCTION that checks the arguments against hard rules and then calls db.py.

The model never touches the database directly. If a rule fails, the function raises
ToolError and run_tool() returns {"error": "..."} to the model, which then tries
again. The rules live here, in code, so no prompt wording can talk its way past them.

This file knows nothing about which LLM provider is used. planner.py (the loop)
turns SCHEMAS into whatever format the provider wants.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import availability
import db

FMT = "%Y-%m-%d %H:%M"
MAX_NEW_ITEMS_PER_RUN = 5


class ToolError(Exception):
    """A rule the model broke. The message goes back to the model, so write it
    as an instruction it can act on."""


@dataclass
class Context:
    """Everything a tool call needs besides its arguments. One per planner run."""
    conn: sqlite3.Connection
    config: dict
    now: str                                   # 'YYYY-MM-DD HH:MM'
    new_items: int = 0                         # add_item calls so far this run
    log: list[dict] = field(default_factory=list)   # every change made, for the summary

    @property
    def today(self) -> date:
        return datetime.strptime(self.now, FMT).date()

    @property
    def horizon_end(self) -> date:
        """Last day the planner may plan: this Sunday, or next Sunday if today is
        Sunday (the Sunday-evening run plans the coming week)."""
        days_to_sunday = 6 - self.today.weekday()
        return self.today + timedelta(days=days_to_sunday or 7)


# ---------------------------------------------------------------- helpers

def _parse(s: str, what: str) -> datetime:
    try:
        return datetime.strptime(s, FMT)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must look like 'YYYY-MM-DD HH:MM', got {s!r}")


def _parse_day(s: str, what: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must look like 'YYYY-MM-DD', got {s!r}")


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _cap(ctx: Context, d: date) -> int:
    return ctx.config["daily_cap_minutes"]["weekend" if _is_weekend(d) else "weekday"]


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _planned_on(ctx: Context, d: date, exclude: int | None = None) -> list[dict]:
    rows = db.sessions_between(ctx.conn, d.isoformat(), (d + timedelta(days=1)).isoformat())
    return [r for r in rows if r["session_id"] != exclude]


def _free_on(ctx: Context, d: date, exclude: int | None = None) -> list[tuple[datetime, datetime]]:
    """Free slots on day d, treating other sessions as padded by the gap so blocks
    never sit back to back. exclude: a session being moved doesn't block itself."""
    gap = timedelta(minutes=ctx.config.get("session_gap_minutes", 0))
    taken = [(_parse(r["start_at"], "start_at") - gap, _parse(r["end_at"], "end_at") + gap)
             for r in _planned_on(ctx, d, exclude)]
    return availability.free_slots(d, ctx.config, taken)


def _check_block(ctx: Context, item: dict, start_s: str, end_s: str,
                 exclude: int | None = None) -> tuple[datetime, datetime]:
    """Every hard rule for putting `item` in [start, end), including Zak's track
    blocks ("no Greek on weekend mornings"). Raises ToolError."""
    start, end = _parse(start_s, "start"), _parse(end_s, "end")
    now = _parse(ctx.now, "now")
    length = _minutes(start, end)
    limits = ctx.config["session_minutes"]

    if end <= start:
        raise ToolError("end must be after start")
    if start.date() != end.date():
        raise ToolError("a session must start and end on the same day")
    if start < now:
        raise ToolError(f"start is in the past (now is {ctx.now})")
    if start.date() > ctx.horizon_end:
        raise ToolError(f"you can only plan up to {ctx.horizon_end.isoformat()}")
    if not limits["min"] <= length <= limits["max"]:
        raise ToolError(f"sessions must be {limits['min']}-{limits['max']} minutes, "
                        f"this one is {length}. Split long items across several sessions.")

    if not any(s <= start and end <= e for s, e in _free_on(ctx, start.date(), exclude)):
        free = [f"{s:%H:%M}-{e:%H:%M}" for s, e in _free_on(ctx, start.date(), exclude)]
        raise ToolError(f"{start_s}-{end:%H:%M} isn't free (it overlaps school, the buffer, "
                        f"another session or the 10-minute gap, or is outside the study "
                        f"window). Free on {start.date()}: {', '.join(free) or 'nothing'}")

    used = sum(_minutes(_parse(r["start_at"], ""), _parse(r["end_at"], ""))
               for r in _planned_on(ctx, start.date(), exclude))
    if used + length > _cap(ctx, start.date()):
        raise ToolError(f"daily cap is {_cap(ctx, start.date())} min and {used} min are "
                        f"already planned on {start.date()}; this adds {length}")

    for b in db.blocks_on(ctx.conn, item["track_id"], start.date()):
        if f"{start:%H:%M}" < b["end_time"] and f"{end:%H:%M}" > b["start_time"]:
            raise ToolError(f"{item['track']} is blocked {b['start_time']}-{b['end_time']} on "
                            f"{b['days']} (Zak: {b['reason'] or 'no reason given'}). Put a "
                            f"different track there, or this one at another time.")

    if item["due_date"] and end.date() >= date.fromisoformat(item["due_date"]):
        raise ToolError(f"'{item['title']}' is due {item['due_date']}, so it must be "
                        f"done by the day before")
    return start, end


def _available_item(ctx: Context, item_id: int) -> dict:
    for it in db.available_items(ctx.conn, ctx.now):
        if it["item_id"] == item_id:
            return it
    item = db.get_item(ctx.conn, item_id)
    if item is None:
        raise ToolError(f"no item {item_id}")
    raise ToolError(f"item {item_id} ('{item['title']}') can't be planned: it's done, "
                    f"waiting on a dependency, or its track is paused")


def _movable_session(ctx: Context, session_id: int) -> dict:
    s = db.get_session(ctx.conn, session_id)
    if s is None or s["status"] != "planned":
        raise ToolError(f"no planned session {session_id}")
    if s["locked"]:
        raise ToolError(f"session {session_id} is locked by Zak; leave it alone")
    if s["start_at"] < ctx.now:
        raise ToolError(f"session {session_id} has already started or finished")
    return s


# ---------------------------------------------------------------- read-only tools

def get_free_slots(ctx: Context, date_from: str, date_to: str) -> dict:
    d0, d1 = _parse_day(date_from, "date_from"), _parse_day(date_to, "date_to")
    d0, d1 = max(d0, ctx.today), min(d1, ctx.horizon_end)
    now = _parse(ctx.now, "now")
    days = []
    d = d0
    while d <= d1:
        used = sum(_minutes(_parse(r["start_at"], ""), _parse(r["end_at"], ""))
                   for r in _planned_on(ctx, d))
        slots = [(max(s, now), e) for s, e in _free_on(ctx, d) if e > now]
        days.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "cap_remaining_minutes": max(0, _cap(ctx, d) - used),
            "slots": [{"start": f"{s:%H:%M}", "end": f"{e:%H:%M}", "minutes": _minutes(s, e)}
                      for s, e in slots if _minutes(s, e) >= ctx.config["session_minutes"]["min"]],
        })
        d += timedelta(days=1)
    return {"days": days}


def get_sessions(ctx: Context, date_from: str, date_to: str) -> dict:
    d0, d1 = _parse_day(date_from, "date_from"), _parse_day(date_to, "date_to")
    rows = db.sessions_between(ctx.conn, d0.isoformat(), (d1 + timedelta(days=1)).isoformat())
    return {"sessions": [{k: r[k] for k in ("session_id", "item_id", "track", "title",
                                             "start_at", "end_at", "brief", "locked")}
                         for r in rows]}


def get_available_items(ctx: Context, track_id: int | None = None) -> dict:
    return {"items": db.available_items(ctx.conn, ctx.now, track_id)}


def get_item_history(ctx: Context, item_id: int) -> dict:
    item = db.get_item(ctx.conn, item_id)
    if item is None:
        raise ToolError(f"no item {item_id}")
    history = [{k: c[k] for k in ("logged_at", "minutes_spent", "outcome", "confidence", "note")}
               for c in db.item_history(ctx.conn, item_id)]
    return {"item": item, "completions": history}


# ---------------------------------------------------------------- autonomous tools

def plan_session(ctx: Context, item_id: int, start: str, end: str, brief: str) -> dict:
    item = _available_item(ctx, item_id)
    if not brief or not brief.strip():
        raise ToolError("brief is required: say what to do in this session")
    _check_block(ctx, item, start, end)
    sid = db.plan_session(ctx.conn, item_id, start, end, brief=brief.strip())
    ctx.log.append({"did": "planned", "session_id": sid, "item": item["title"],
                    "start": start, "end": end})
    return {"ok": True, "session_id": sid}


def move_session(ctx: Context, session_id: int, new_start: str, new_end: str) -> dict:
    s = _movable_session(ctx, session_id)
    item = _available_item(ctx, s["item_id"])
    _check_block(ctx, item, new_start, new_end, exclude=session_id)
    new_id = db.move_session(ctx.conn, session_id, new_start, new_end)
    ctx.log.append({"did": "moved", "item": item["title"], "from": s["start_at"],
                    "to": new_start, "new_session_id": new_id})
    return {"ok": True, "new_session_id": new_id}


def add_item(ctx: Context, track_id: int, title: str, est_minutes: int,
             due_date: str | None = None, after_item_id: int | None = None,
             strand: str | None = None) -> dict:
    if ctx.new_items >= MAX_NEW_ITEMS_PER_RUN:
        raise ToolError(f"you've added {MAX_NEW_ITEMS_PER_RUN} items this run; that's the limit")
    if track_id not in {t["track_id"] for t in db.list_tracks(ctx.conn)}:
        raise ToolError(f"no active track {track_id}")
    if not title or not title.strip():
        raise ToolError("title is required")
    if not 5 <= est_minutes <= 600:
        raise ToolError("est_minutes must be 5-600")
    if due_date is not None:
        _parse_day(due_date, "due_date")
    try:
        item_id = db.add_item_after(ctx.conn, track_id, title.strip(), est_minutes,
                                    after_item_id, due_date, strand)
    except ValueError as e:
        raise ToolError(str(e))
    ctx.new_items += 1
    ctx.log.append({"did": "added item", "item_id": item_id, "title": title.strip()})
    return {"ok": True, "item_id": item_id}


# ---------------------------------------------------------------- proposal tool

def propose(ctx: Context, action: str, target_id: int, reason: str) -> dict:
    if not reason or not reason.strip():
        raise ToolError("reason is required: Zak decides based on it")
    if action == "cancel_session":
        _movable_session(ctx, target_id)
    elif action == "pause_track":
        if target_id not in {t["track_id"] for t in db.list_tracks(ctx.conn)}:
            raise ToolError(f"no active track {target_id}")
    elif action == "mark_item_done":
        item = db.get_item(ctx.conn, target_id)
        if item is None or item["status"] == "done":
            raise ToolError(f"item {target_id} doesn't exist or is already done")
    else:
        raise ToolError("action must be cancel_session, pause_track or mark_item_done")
    try:
        pid = db.create_proposal(ctx.conn, action, target_id, reason.strip())
    except sqlite3.IntegrityError:
        raise ToolError("that exact proposal is already waiting for Zak's answer")
    ctx.log.append({"did": "proposed", "action": action, "target_id": target_id,
                    "reason": reason.strip()})
    return {"ok": True, "proposal_id": pid, "note": "Zak will approve or reject this later"}


# ---------------------------------------------------------------- schemas (what the model sees)

_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_TIME = {"type": "string", "description": "YYYY-MM-DD HH:MM, local UK time"}

SCHEMAS = [
    {
        "name": "get_free_slots",
        "description": "Free time between two dates (inclusive), per day, after school, "
                       "buffers, planned sessions and the gap between sessions. Also shows "
                       "how many minutes of the daily cap are left.",
        "input_schema": {"type": "object", "properties": {
            "date_from": _DATE, "date_to": _DATE}, "required": ["date_from", "date_to"]},
    },
    {
        "name": "get_sessions",
        "description": "Planned sessions between two dates (inclusive), with brief and "
                       "whether Zak locked them. Locked sessions must not be moved or cancelled.",
        "input_schema": {"type": "object", "properties": {
            "date_from": _DATE, "date_to": _DATE}, "required": ["date_from", "date_to"]},
    },
    {
        "name": "get_available_items",
        "description": "Every item that can be worked on now (not done, dependencies done, "
                       "track active), with estimate, due date, minutes already spent and "
                       "minutes already planned ahead. Optionally for one track.",
        "input_schema": {"type": "object", "properties": {
            "track_id": {"type": "integer"}}},
    },
    {
        "name": "get_item_history",
        "description": "Every logged attempt at one item: minutes, outcome, confidence 1-5, "
                       "notes. Use it to judge whether an item needs revisiting and how to "
                       "brief the next session.",
        "input_schema": {"type": "object", "properties": {
            "item_id": {"type": "integer"}}, "required": ["item_id"]},
    },
    {
        "name": "plan_session",
        "description": "Book a study block for one item. Must fit a free slot, the daily cap "
                       "and any due date. A big item can be split over several sessions. "
                       "brief: 1-3 sentences telling Zak exactly what to do in the session.",
        "input_schema": {"type": "object", "properties": {
            "item_id": {"type": "integer"}, "start": _TIME, "end": _TIME,
            "brief": {"type": "string"}},
            "required": ["item_id", "start", "end", "brief"]},
    },
    {
        "name": "move_session",
        "description": "Move an unlocked future session to a new time, keeping its item "
                       "and brief. Same rules as plan_session.",
        "input_schema": {"type": "object", "properties": {
            "session_id": {"type": "integer"}, "new_start": _TIME, "new_end": _TIME},
            "required": ["session_id", "new_start", "new_end"]},
    },
    {
        "name": "add_item",
        "description": "Add a new unit of work to a track, e.g. a revision item for a topic "
                       "with low confidence. after_item_id places it straight after an "
                       "existing item in that track; otherwise it goes at the end. "
                       f"At most {MAX_NEW_ITEMS_PER_RUN} per run.",
        "input_schema": {"type": "object", "properties": {
            "track_id": {"type": "integer"}, "title": {"type": "string"},
            "est_minutes": {"type": "integer"}, "due_date": _DATE,
            "after_item_id": {"type": "integer"},
            "strand": {"type": "string", "description": "Same strand names the track "
                       "already uses, e.g. 'odyssey'. Omit if the track has none."}},
            "required": ["track_id", "title", "est_minutes"]},
    },
    {
        "name": "propose",
        "description": "Ask Zak to approve something you may not do yourself: cancel a "
                       "session without replacing it, pause a track, or mark an item done. "
                       "Nothing happens until he says yes.",
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["cancel_session", "pause_track", "mark_item_done"]},
            "target_id": {"type": "integer",
                          "description": "session_id, track_id or item_id, matching action"},
            "reason": {"type": "string", "description": "One sentence Zak will read"}},
            "required": ["action", "target_id", "reason"]},
    },
    {
        "name": "finish",
        "description": "End the planning run. summary: 2-5 short lines for Zak on what "
                       "you changed and why. Call this exactly once, last.",
        "input_schema": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]},
    },
]

FUNCTIONS = {
    "get_free_slots": get_free_slots,
    "get_sessions": get_sessions,
    "get_available_items": get_available_items,
    "get_item_history": get_item_history,
    "plan_session": plan_session,
    "move_session": move_session,
    "add_item": add_item,
    "propose": propose,
}


def run_tool(ctx: Context, name: str, args: dict) -> dict:
    """Run one tool call from the model. Never raises: every failure becomes an
    {"error": ...} result the model can read and react to."""
    if name == "finish":
        return {"ok": True}          # the loop itself notices finish and stops
    fn = FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return fn(ctx, **args)
    except ToolError as e:
        return {"error": str(e)}
    except TypeError as e:            # wrong or missing argument names
        return {"error": f"bad arguments for {name}: {e}"}
    except sqlite3.IntegrityError as e:
        return {"error": f"database refused: {e}"}
