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

ROOT = Path(__file__).parent
DEFAULT_DB = ROOT / "coach.db"


# ---------------------------------------------------------------- connection

def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced (SQLite has them OFF by default,
    per connection) and rows accessible by column name."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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


def _today(today: str | None) -> str:
    return today or date.today().isoformat()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


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

def plan_session(conn, item_id: int, start_at: str, end_at: str) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sessions (item_id, start_at, end_at) VALUES (?, ?, ?)",
            (item_id, start_at, end_at))
    return cur.lastrowid


def set_calendar_event_id(conn, session_id: int, event_id: str) -> None:
    """Called by the calendar sync once the block exists in Google Calendar."""
    with transaction(conn):
        conn.execute("UPDATE sessions SET calendar_event_id = ? WHERE session_id = ?",
                     (event_id, session_id))


def cancel_session(conn, session_id: int) -> dict:
    """Mark a session cancelled (a re-plan removed it). Kept, not deleted, but it no
    longer counts as 'planned' in completion rates. Returns the row so the caller
    can delete the matching calendar event."""
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
    }


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description="Study coach database")
    p.add_argument("command", choices=["init", "seed", "snapshot"],
                   help="init: empty db | seed: db with fake data | snapshot: print planner view")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--today", help="YYYY-MM-DD (default: real today)")
    args = p.parse_args()

    conn = connect(args.db)
    if args.command in ("init", "seed"):
        init_db(conn, seed=args.command == "seed")
        print(f"Rebuilt {args.db}" + (" with fake data" if args.command == "seed" else ""))
    else:
        print(json.dumps(planner_snapshot(conn, args.today), indent=2))
