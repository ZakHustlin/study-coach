-- Frozen copy of schema.sql at version 0 (before migration 1). Used only by
-- test_migrations.py to check that migrating an old database gives the same
-- schema as building a new one. Never edit.
-- Study Coach schema (SQLite)
-- Rebuild: python -c "import sqlite3; c=sqlite3.connect('coach.db'); c.executescript(open('schema.sql').read())"
-- All durations in minutes. All dates/times as ISO text: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (local time).

PRAGMA foreign_keys = ON;

-- Drop children before parents
DROP TABLE IF EXISTS completions;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS item_dependencies;
DROP TABLE IF EXISTS items;
DROP TABLE IF EXISTS tracks;

-- A study area: Greek, maths, coding/ML, schoolwork, reading
CREATE TABLE tracks (
    track_id              INTEGER PRIMARY KEY,
    name                  TEXT    NOT NULL UNIQUE,
    priority              INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),   -- 5 = most important; ties allowed
    weekly_target_minutes INTEGER NOT NULL DEFAULT 0 CHECK (weekly_target_minutes >= 0),
    end_date              TEXT,                                                -- NULL = ongoing (e.g. TMUA exam date)
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)) -- pause without deleting history
);

-- A unit of work within a track: text section, maths topic, lecture, homework
CREATE TABLE items (
    item_id     INTEGER PRIMARY KEY,
    track_id    INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    est_minutes INTEGER NOT NULL CHECK (est_minutes > 0),
    position    INTEGER NOT NULL DEFAULT 0,        -- natural order within the track (e.g. chapter order)
    due_date    TEXT,                              -- NULL unless it has a deadline (homework)
    status      TEXT    NOT NULL DEFAULT 'not_started'
                CHECK (status IN ('not_started', 'in_progress', 'done')),
    FOREIGN KEY (track_id) REFERENCES tracks(track_id)
);

-- Many-to-many: item_id cannot start until depends_on_id is done
CREATE TABLE item_dependencies (
    item_id       INTEGER NOT NULL,
    depends_on_id INTEGER NOT NULL,
    PRIMARY KEY (item_id, depends_on_id),
    CHECK (item_id <> depends_on_id),
    FOREIGN KEY (item_id)       REFERENCES items(item_id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on_id) REFERENCES items(item_id) ON DELETE CASCADE
);

-- A planned block: one session = one item
CREATE TABLE sessions (
    session_id        INTEGER PRIMARY KEY,
    item_id           INTEGER NOT NULL,
    start_at          TEXT    NOT NULL,
    end_at            TEXT    NOT NULL,
    calendar_event_id TEXT    UNIQUE,              -- NULL until written to Google Calendar
    status            TEXT    NOT NULL DEFAULT 'planned'
                      CHECK (status IN ('planned', 'cancelled')),  -- cancelled = removed by re-plan, not counted
    CHECK (end_at > start_at),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- What actually happened. session_id NULL = unplanned study.
CREATE TABLE completions (
    completion_id INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL,
    session_id    INTEGER,
    logged_at     TEXT    NOT NULL,
    minutes_spent INTEGER NOT NULL DEFAULT 0 CHECK (minutes_spent >= 0),
    outcome       TEXT    NOT NULL CHECK (outcome IN ('done', 'partial', 'skipped')),
    confidence    INTEGER CHECK (confidence BETWEEN 1 AND 5),   -- NULL if skipped
    note          TEXT,
    FOREIGN KEY (item_id)    REFERENCES items(item_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX idx_items_track        ON items(track_id);
CREATE INDEX idx_sessions_start     ON sessions(start_at);
CREATE INDEX idx_completions_item   ON completions(item_id, logged_at);
