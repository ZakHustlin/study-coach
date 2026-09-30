"""Data layer for the study coach.

Every other part of the app (Telegram bot, calendar sync, LLM planner) talks to the
database only through these functions, never with raw SQL. That keeps the SQL in one
place: if the schema changes, only this file and the .sql files change.

Conventions
- Dates are 'YYYY-MM-DD', datetimes 'YYYY-MM-DD HH:MM', both in local (UK) time.
- Durations are in minutes.
- Functions return plain dicts / lists of dicts so callers (and the LLM prompt) can
  serialise them straight to JSON.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DEFAULT_DB = ROOT / "coach.db"


# ---------------------------------------------------------------- connection

def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced (SQLite has them OFF by default,
    per connection) and rows accessible by column name. Brings an older database
    up to the current schema, so no caller can run against a stale one."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


# ---------------------------------------------------------------- migrations
#
# schema.sql always describes the *current* schema, for building a fresh database.
# MIGRATIONS turns an *existing* database into that same shape without losing rows.
# PRAGMA user_version (an integer SQLite stores in the file header) records how many
# migrations a database has had. Rule: every schema change = edit schema.sql AND
# append one migration here AND bump the user_version line at the end of schema.sql.
# Never edit a migration once it's been run on the real coach.db - add a new one.

MIGRATIONS: list[str] = [
    # 1: planner support - lesson briefs, locked sessions, proposals, weekly priorities
    """
    ALTER TABLE sessions ADD COLUMN brief TEXT;
    ALTER TABLE sessions ADD COLUMN locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1));

    CREATE TABLE proposals (
        proposal_id INTEGER PRIMARY KEY,
        created_at  TEXT    NOT NULL,
        action      TEXT    NOT NULL
                    CHECK (action IN ('cancel_session', 'pause_track', 'mark_item_done')),
        target_id   INTEGER NOT NULL,
        reason      TEXT    NOT NULL,
        status      TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
        decided_at  TEXT
    );

    CREATE TABLE weekly_priorities (
        week_start TEXT PRIMARY KEY,
        priorities TEXT NOT NULL,
        set_at     TEXT NOT NULL
    );

    CREATE UNIQUE INDEX idx_proposals_one_pending
        ON proposals(action, target_id) WHERE status = 'pending';

    CREATE TRIGGER sessions_locked_guard
    BEFORE UPDATE OF item_id, start_at, end_at, status ON sessions
    WHEN OLD.locked = 1 AND NEW.locked = 1
    BEGIN
        SELECT RAISE(ABORT, 'session is locked');
    END;
    """,
]


def schema_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn) -> list[int]:
    """Apply any migrations this database hasn't had yet. Returns the numbers applied.
    An empty database (no tables yet) is left alone: init_db builds it at the
    current version straight from schema.sql."""
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'").fetchone()
    if not has_tables:
        return []
    applied = []
    for number in range(schema_version(conn) + 1, len(MIGRATIONS) + 1):
        # Each migration runs as one transaction together with its version bump, so a
        # failure halfway leaves the database exactly as it was before (atomicity).
        conn.executescript(f"BEGIN;\n{MIGRATIONS[number - 1]}\nPRAGMA user_version = {number};\nCOMMIT;")
        applied.append(number)
    conn.execute("PRAGMA foreign_keys = ON")  # executescript can reset it
    return applied


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Commit if the block succeeds, roll back if it raises - so a failure halfway
    through a multi-step write never leaves half the rows behind."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(conn: sqlite3.Connection, seed: bool = False) -> None:
    """Drop and recreate every table. seed=True loads the fake test data too."""
    conn.executescript((ROOT / "schema.sql").read_text())
    if seed:
        conn.executescript((ROOT / "seed.sql").read_text())
    conn.execute("PRAGMA foreign_keys = ON")  # executescript can reset it


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# All times in the app are UK local time. Servers (Codespaces, the VPS) run on UTC,
# so plain datetime.now() would be an hour behind during British Summer Time.
LOCAL_TZ = ZoneInfo("Europe/London")


def local_now() -> datetime:
    """Current UK time as a naive datetime, matching how times are stored."""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def _today(today: str | None) -> str:
    return today or local_now().date().isoformat()


def _now() -> str:
    return local_now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- named queries

def _load_queries() -> dict[str, str]:
    """Split queries.sql on '-- name: <key>' headers."""
    queries: dict[str, str] = {}
    name = None
    buf: list[str] = []
    for line in (ROOT / "queries.sql").read_text().splitlines():
        if line.startswith("-- name:"):
            if name:
                queries[name] = "\n".join(buf).strip()
            name, buf = line.split(":", 1)[1].strip(), []
        elif name:
            buf.append(line)
    if name:
        queries[name] = "\n".join(buf).strip()
    return queries


QUERIES = _load_queries()


def completion_rates(conn, today: str | None = None) -> list[dict]:
    """Test query 1: planned vs done per track, last 14 days."""
    return _rows(conn.execute(QUERIES["completion_rates"], {"today": _today(today)}))


def estimate_accuracy(conn) -> list[dict]:
    """Test query 2: actual ÷ estimated minutes per track (finished items)."""
    return _rows(conn.execute(QUERIES["estimate_accuracy"]))


def stale_low_confidence(conn, today: str | None = None) -> list[dict]:
    """Test query 3: latest confidence < 3 and untouched for 7+ days."""
    return _rows(conn.execute(QUERIES["stale_low_confidence"], {"today": _today(today)}))


def next_items(conn) -> list[dict]:
    """Test query 4: next available item per active track, highest priority first."""
    return _rows(conn.execute(QUERIES["next_items"]))


def day_summary(conn, day: str | None = None) -> list[dict]:
    """Test query 5: everything planned and done on a day, for the evening check-in."""
    return _rows(conn.execute(QUERIES["day_summary"], {"today": _today(day)}))


# ---------------------------------------------------------------- tracks

def add_track(conn, name: str, priority: int, weekly_target_minutes: int = 0,
              end_date: str | None = None) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO tracks (name, priority, weekly_target_minutes, end_date) "
            "VALUES (?, ?, ?, ?)",
            (name, priority, weekly_target_minutes, end_date))
    return cur.lastrowid


def list_tracks(conn, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM tracks"
    if active_only:
        sql += " WHERE is_active = 1"
    return _rows(conn.execute(sql + " ORDER BY priority DESC, name"))


def set_track_active(conn, track_id: int, active: bool) -> None:
    with transaction(conn):
        conn.execute("UPDATE tracks SET is_active = ? WHERE track_id = ?",
                     (int(active), track_id))


# ---------------------------------------------------------------- items

def add_item(conn, track_id: int, title: str, est_minutes: int,
             position: int | None = None, due_date: str | None = None) -> int:
    """Add an item. If position is omitted it goes to the end of its track."""
    with transaction(conn):
        if position is None:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM items WHERE track_id = ?",
                (track_id,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO items (track_id, title, est_minutes, position, due_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (track_id, title, est_minutes, position, due_date))
    return cur.lastrowid


def add_item_after(conn, track_id: int, title: str, est_minutes: int,
                   after_item_id: int | None = None, due_date: str | None = None) -> int:
    """Add an item straight after another in the same track, shifting the later
    items down one place. Without after_item_id it goes to the end."""
    if after_item_id is None:
        return add_item(conn, track_id, title, est_minutes, due_date=due_date)
    with transaction(conn):
        after = conn.execute("SELECT track_id, position FROM items WHERE item_id = ?",
                             (after_item_id,)).fetchone()
        if after is None or after["track_id"] != track_id:
            raise ValueError(f"item {after_item_id} is not in track {track_id}")
        conn.execute("UPDATE items SET position = position + 1 "
                     "WHERE track_id = ? AND position > ?", (track_id, after["position"]))
        cur = conn.execute(
            "INSERT INTO items (track_id, title, est_minutes, position, due_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (track_id, title, est_minutes, after["position"] + 1, due_date))
    return cur.lastrowid


def available_items(conn, now: str | None = None, track_id: int | None = None) -> list[dict]:
    """Every item that can be worked on now (not just the first per track, unlike
    next_items): not done, dependencies done, track active. Includes how much time
    has gone into it and how much is already planned ahead, so the planner can
    decide whether to split it across sessions."""
    sql = """
        SELECT i.item_id, t.track_id, t.name AS track, i.title, i.est_minutes,
               i.due_date, i.status, i.position,
               (SELECT COALESCE(SUM(c.minutes_spent), 0) FROM completions c
                 WHERE c.item_id = i.item_id) AS minutes_spent,
               (SELECT COALESCE(SUM((julianday(s.end_at) - julianday(s.start_at)) * 1440), 0)
                  FROM sessions s
                 WHERE s.item_id = i.item_id AND s.status = 'planned' AND s.start_at >= :now
               ) AS minutes_planned_ahead
        FROM items i
        JOIN tracks t ON t.track_id = i.track_id
        WHERE t.is_active = 1
          AND i.status <> 'done'
          AND NOT EXISTS (
              SELECT 1 FROM item_dependencies d
              JOIN items dep ON dep.item_id = d.depends_on_id
              WHERE d.item_id = i.item_id AND dep.status <> 'done')
    """
    params: dict = {"now": now or _now()}
    if track_id is not None:
        sql += " AND i.track_id = :track_id"
        params["track_id"] = track_id
    rows = _rows(conn.execute(sql + " ORDER BY t.priority DESC, i.position, i.item_id", params))
    for r in rows:
        r["minutes_planned_ahead"] = round(r["minutes_planned_ahead"])
    return rows


def get_item(conn, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_items(conn, track_id: int | None = None, include_done: bool = False) -> list[dict]:
    sql = "SELECT * FROM items WHERE 1 = 1"
    params: list = []
    if track_id is not None:
        sql += " AND track_id = ?"
        params.append(track_id)
    if not include_done:
        sql += " AND status <> 'done'"
    return _rows(conn.execute(sql + " ORDER BY track_id, position", params))


def set_item_status(conn, item_id: int, status: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE items SET status = ? WHERE item_id = ?", (status, item_id))


def add_dependency(conn, item_id: int, depends_on_id: int) -> None:
    """item_id can't start until depends_on_id is done.

    The CHECK constraint only blocks A -> A. A longer cycle (A -> B -> A) would make
    both items permanently unavailable, so walk the existing dependency graph from
    depends_on_id and refuse if it already leads back to item_id.
    """
    cycle = conn.execute(
        """
        WITH RECURSIVE reach(id) AS (
            SELECT depends_on_id FROM item_dependencies WHERE item_id = :start
            UNION
            SELECT d.depends_on_id FROM item_dependencies d JOIN reach r ON d.item_id = r.id
        )
        SELECT 1 FROM reach WHERE id = :target
        """,
        {"start": depends_on_id, "target": item_id}).fetchone()
    if cycle:
        raise ValueError(f"Dependency {item_id} -> {depends_on_id} would create a cycle")
    with transaction(conn):
        # Not INSERT OR IGNORE: that also silently ignores CHECK failures.
        # ON CONFLICT only skips the duplicate-pair case.
        conn.execute("INSERT INTO item_dependencies VALUES (?, ?) "
                     "ON CONFLICT (item_id, depends_on_id) DO NOTHING",
                     (item_id, depends_on_id))


# ---------------------------------------------------------------- sessions

def plan_session(conn, item_id: int, start_at: str, end_at: str,
                 brief: str | None = None, locked: bool = False) -> int:
    """brief: the lesson prompt for the calendar event. locked: pinned by me, so
    the planner may not move or cancel it."""
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sessions (item_id, start_at, end_at, brief, locked) VALUES (?, ?, ?, ?, ?)",
            (item_id, start_at, end_at, brief, int(locked)))
    return cur.lastrowid


def move_session(conn, session_id: int, start_at: str, end_at: str) -> int:
    """Re-plan a session at a new time: cancel the old row and create a new one with
    the same item, brief and lock, in ONE transaction. Returns the new session_id.
    (Cancel + new rather than editing the times keeps the history and fits the
    one-way calendar sync, which deletes the old event and creates the new one.)"""
    with transaction(conn):
        old = conn.execute("SELECT * FROM sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
        if old is None or old["status"] != "planned":
            raise ValueError(f"session {session_id} is not a planned session")
        conn.execute("UPDATE sessions SET status = 'cancelled' WHERE session_id = ?",
                     (session_id,))   # the locked trigger fires here if it's locked
        cur = conn.execute(
            "INSERT INTO sessions (item_id, start_at, end_at, brief, locked) VALUES (?, ?, ?, ?, ?)",
            (old["item_id"], start_at, end_at, old["brief"], old["locked"]))
    return cur.lastrowid


def get_session(conn, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def set_session_locked(conn, session_id: int, locked: bool) -> None:
    with transaction(conn):
        conn.execute("UPDATE sessions SET locked = ? WHERE session_id = ?",
                     (int(locked), session_id))


def set_calendar_event_id(conn, session_id: int, event_id: str) -> None:
    """Called by the calendar sync once the block exists in Google Calendar."""
    with transaction(conn):
        conn.execute("UPDATE sessions SET calendar_event_id = ? WHERE session_id = ?",
                     (event_id, session_id))


def cancel_session(conn, session_id: int) -> dict:
    """Mark a session cancelled (a re-plan removed it). Kept, not deleted, but it no
    longer counts as 'planned' in completion rates. Returns the row so the caller
    can delete the matching calendar event. Raises sqlite3.IntegrityError if the
    session is locked - unlock it first."""
    with transaction(conn):
        conn.execute("UPDATE sessions SET status = 'cancelled' WHERE session_id = ?",
                     (session_id,))
    return dict(conn.execute("SELECT * FROM sessions WHERE session_id = ?",
                             (session_id,)).fetchone())


def sessions_between(conn, start: str, end: str, include_cancelled: bool = False) -> list[dict]:
    """Planned sessions in [start, end), joined with item and track names."""
    sql = """
        SELECT s.*, i.title, t.name AS track
        FROM sessions s
        JOIN items i  ON i.item_id = s.item_id
        JOIN tracks t ON t.track_id = i.track_id
        WHERE s.start_at >= ? AND s.start_at < ?
    """
    if not include_cancelled:
        sql += " AND s.status = 'planned'"
    return _rows(conn.execute(sql + " ORDER BY s.start_at", (start, end)))


def unlogged_sessions(conn, day: str | None = None) -> list[dict]:
    """Today's planned sessions with no completion yet - what the check-in asks about."""
    return _rows(conn.execute(
        """
        SELECT s.session_id, s.item_id, s.start_at, s.end_at, i.title
        FROM sessions s
        JOIN items i ON i.item_id = s.item_id
        WHERE s.status = 'planned'
          AND date(s.start_at) = date(?)
          AND NOT EXISTS (SELECT 1 FROM completions c WHERE c.session_id = s.session_id)
        ORDER BY s.start_at
        """, (_today(day),)))


# ---------------------------------------------------------------- calendar sync

def sessions_to_create(conn, now: str | None = None) -> list[dict]:
    """Planned sessions not yet in Google Calendar and not already over."""
    return _rows(conn.execute(
        """
        SELECT s.*, i.title, t.name AS track
        FROM sessions s
        JOIN items i  ON i.item_id = s.item_id
        JOIN tracks t ON t.track_id = i.track_id
        WHERE s.status = 'planned' AND s.calendar_event_id IS NULL AND s.end_at > ?
        ORDER BY s.start_at
        """, (now or _now(),)))


def sessions_to_remove(conn) -> list[dict]:
    """Cancelled sessions whose calendar event still exists."""
    return _rows(conn.execute(
        "SELECT * FROM sessions WHERE status = 'cancelled' AND calendar_event_id IS NOT NULL"))


def clear_calendar_event_id(conn, session_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE sessions SET calendar_event_id = NULL WHERE session_id = ?",
                     (session_id,))


# ---------------------------------------------------------------- completions

def log_completion(conn, item_id: int, outcome: str, minutes_spent: int = 0,
                   confidence: int | None = None, note: str | None = None,
                   session_id: int | None = None, logged_at: str | None = None,
                   mark_item_done: bool | None = None) -> int:
    """Record what actually happened, and update the item's status to match.

    outcome: 'done' | 'partial' | 'skipped'. session_id=None means unplanned study.
    Item status: 'done' -> item done, 'partial' -> in_progress (unless already done),
    'skipped' -> unchanged. Pass mark_item_done to override, e.g. a session went
    'done' but the item (a long text) still has more to go.
    """
    if session_id is not None:
        row = conn.execute("SELECT item_id FROM sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"No session {session_id}")
        if row["item_id"] != item_id:
            raise ValueError(f"Session {session_id} is for item {row['item_id']}, not {item_id}")
    if outcome == "skipped":
        minutes_spent, confidence = 0, None

    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO completions (item_id, session_id, logged_at, minutes_spent, "
            "outcome, confidence, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, session_id, logged_at or _now(), minutes_spent, outcome,
             confidence, note))

        done = mark_item_done if mark_item_done is not None else outcome == "done"
        if done:
            conn.execute("UPDATE items SET status = 'done' WHERE item_id = ?", (item_id,))
        elif outcome != "skipped":
            conn.execute("UPDATE items SET status = 'in_progress' "
                         "WHERE item_id = ? AND status <> 'done'", (item_id,))
    return cur.lastrowid


def item_history(conn, item_id: int) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM completions WHERE item_id = ? ORDER BY logged_at", (item_id,)))


# ---------------------------------------------------------------- proposals

PROPOSAL_ACTIONS = {
    # action -> SQL run when I approve it
    "cancel_session": "UPDATE sessions SET status = 'cancelled' WHERE session_id = ?",
    "pause_track":    "UPDATE tracks SET is_active = 0 WHERE track_id = ?",
    "mark_item_done": "UPDATE items SET status = 'done' WHERE item_id = ?",
}


def create_proposal(conn, action: str, target_id: int, reason: str) -> int:
    """The planner asks for something it may not do alone. Raises
    sqlite3.IntegrityError if the same proposal is already pending."""
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO proposals (created_at, action, target_id, reason) VALUES (?, ?, ?, ?)",
            (_now(), action, target_id, reason))
    return cur.lastrowid


def pending_proposals(conn) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM proposals WHERE status = 'pending' ORDER BY created_at, proposal_id"))


def decide_proposal(conn, proposal_id: int, approve: bool) -> dict:
    """Approve (carry out the action) or reject. The status change and the action
    happen in one transaction: if the action fails (e.g. the session is now
    locked), neither happens. Returns the updated proposal."""
    p = conn.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    if p is None:
        raise ValueError(f"no proposal {proposal_id}")
    if p["status"] != "pending":
        raise ValueError(f"proposal {proposal_id} is already {p['status']}")
    with transaction(conn):
        if approve:
            cur = conn.execute(PROPOSAL_ACTIONS[p["action"]], (p["target_id"],))
            if cur.rowcount != 1:
                raise ValueError(f"{p['action']}: target {p['target_id']} not found")
        conn.execute("UPDATE proposals SET status = ?, decided_at = ? WHERE proposal_id = ?",
                     ("approved" if approve else "rejected", _now(), proposal_id))
    return dict(conn.execute("SELECT * FROM proposals WHERE proposal_id = ?",
                             (proposal_id,)).fetchone())


def expire_proposals(conn, created_before: str) -> int:
    """Pending proposals I never answered go stale once the plan they were made
    for has moved on. Returns how many were expired."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE proposals SET status = 'expired', decided_at = ? "
            "WHERE status = 'pending' AND created_at < ?", (_now(), created_before))
    return cur.rowcount


# ---------------------------------------------------------------- weekly priorities

def week_start(day: str | None = None) -> str:
    """The Monday of the week containing day."""
    d = date.fromisoformat(_today(day))
    return date.fromordinal(d.toordinal() - d.weekday()).isoformat()


def set_weekly_priorities(conn, priorities: str, day: str | None = None) -> None:
    """Store my answer for the week containing day. Answering again replaces it."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO weekly_priorities (week_start, priorities, set_at) VALUES (?, ?, ?) "
            "ON CONFLICT (week_start) DO UPDATE SET priorities = excluded.priorities, "
            "set_at = excluded.set_at",
            (week_start(day), priorities, _now()))


def get_weekly_priorities(conn, day: str | None = None) -> str | None:
    row = conn.execute("SELECT priorities FROM weekly_priorities WHERE week_start = ?",
                       (week_start(day),)).fetchone()
    return row["priorities"] if row else None


def week_progress(conn, day: str | None = None) -> list[dict]:
    """Per active track, this week (Mon-Sun): target, minutes logged so far and
    minutes planned (not cancelled). Lets the planner see who's behind."""
    start = week_start(day)
    end = date.fromisoformat(start).toordinal() + 7
    end = date.fromordinal(end).isoformat()
    return _rows(conn.execute(
        """
        SELECT t.track_id, t.name AS track, t.priority, t.weekly_target_minutes,
               (SELECT COALESCE(SUM(c.minutes_spent), 0)
                  FROM completions c JOIN items i ON i.item_id = c.item_id
                 WHERE i.track_id = t.track_id
                   AND c.logged_at >= :start AND c.logged_at < :end) AS minutes_logged,
               (SELECT COALESCE(CAST(ROUND(SUM((julianday(s.end_at) - julianday(s.start_at)) * 1440)) AS INTEGER), 0)
                  FROM sessions s JOIN items i ON i.item_id = s.item_id
                 WHERE i.track_id = t.track_id AND s.status = 'planned'
                   AND s.start_at >= :start AND s.start_at < :end) AS minutes_planned
        FROM tracks t
        WHERE t.is_active = 1
        ORDER BY t.priority DESC, t.track_id
        """, {"start": start, "end": end}))


# ---------------------------------------------------------------- planner snapshot

def planner_snapshot(conn, today: str | None = None) -> dict:
    """Everything the LLM planner needs in one dict (step 4 serialises this to JSON)."""
    today = _today(today)
    return {
        "today": today,
        "tracks": list_tracks(conn),
        "next_items": next_items(conn),
        "completion_rates_14d": completion_rates(conn, today),
        "estimate_accuracy": estimate_accuracy(conn),
        "needs_review": stale_low_confidence(conn, today),
        "today_summary": day_summary(conn, today),
        "weekly_priorities": get_weekly_priorities(conn, today),
        "pending_proposals": pending_proposals(conn),
    }


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description="Study coach database")
    p.add_argument("command",
                   choices=["init", "seed", "snapshot", "version",
                            "proposals", "approve", "reject", "priorities"],
                   help="init: empty db | seed: fake data | snapshot: planner view | "
                        "version: schema version | proposals: list pending | "
                        "approve/reject ID | priorities [TEXT]: show or set this week's")
    p.add_argument("arg", nargs="?", help="proposal id, or priorities text")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--today", help="YYYY-MM-DD (default: real today)")
    args = p.parse_args()

    conn = connect(args.db)   # migrates automatically if needed
    if args.command in ("init", "seed"):
        init_db(conn, seed=args.command == "seed")
        print(f"Rebuilt {args.db}" + (" with fake data" if args.command == "seed" else ""))
    elif args.command == "version":
        print(f"schema version {schema_version(conn)} (latest {len(MIGRATIONS)})")
    elif args.command == "proposals":
        for pr in pending_proposals(conn):
            print(f"#{pr['proposal_id']}  {pr['action']}({pr['target_id']})  {pr['reason']}")
    elif args.command in ("approve", "reject"):
        pr = decide_proposal(conn, int(args.arg), approve=args.command == "approve")
        print(f"#{pr['proposal_id']} {pr['status']}"
              + ("  - run `python gcal.py sync`" if pr["action"] == "cancel_session"
                 and pr["status"] == "approved" else ""))
    elif args.command == "priorities":
        if args.arg:
            set_weekly_priorities(conn, args.arg, args.today)
        print(get_weekly_priorities(conn, args.today) or "(none set this week)")
    else:
        print(json.dumps(planner_snapshot(conn, args.today), indent=2))
