-- Study Coach schema (SQLite)
-- Rebuild: python -c "import sqlite3; c=sqlite3.connect('coach.db'); c.executescript(open('schema.sql').read())"
-- All durations in minutes. All dates/times as ISO text: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (local time).

PRAGMA foreign_keys = ON;

-- Drop children before parents
DROP TABLE IF EXISTS instruction_changes;
DROP TABLE IF EXISTS reviews;
DROP TABLE IF EXISTS track_blocks;
DROP TABLE IF EXISTS notes;
DROP TABLE IF EXISTS rules;
DROP TABLE IF EXISTS passages;
DROP TABLE IF EXISTS proposals;
DROP TABLE IF EXISTS weekly_priorities;
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
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)), -- pause without deleting history
    guidance              TEXT                                                 -- free-text instructions the planner follows for this track
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
    strand      TEXT,                              -- sub-area within a track, e.g. 'herodotus'; NULL if none
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
    brief             TEXT,                        -- lesson prompt from the planner, shown in the calendar event
    locked            INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),  -- 1 = pinned by me, planner can't touch
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

-- Actions the planner wants but may not do alone. It proposes; I approve or reject.
-- Every current action targets exactly one row, so target_id is a plain integer
-- rather than a JSON blob: easy to validate and to query.
CREATE TABLE proposals (
    proposal_id INTEGER PRIMARY KEY,
    created_at  TEXT    NOT NULL,
    action      TEXT    NOT NULL
                CHECK (action IN ('cancel_session', 'pause_track', 'mark_item_done')),
    target_id   INTEGER NOT NULL,                  -- session_id / track_id / item_id, depending on action
    reason      TEXT    NOT NULL,                  -- the planner's justification, shown to me
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_at  TEXT                               -- NULL while pending
);

-- My answer to "what matters this week?", read by every re-plan that week.
CREATE TABLE weekly_priorities (
    week_start TEXT PRIMARY KEY,                   -- the Monday, 'YYYY-MM-DD'
    priorities TEXT NOT NULL,
    set_at     TEXT NOT NULL
);

-- Set-text syllabus: how far class has got. Not work items: the planner never books
-- these, it only reads which are covered so revision tests stay within them.
CREATE TABLE passages (
    passage_id INTEGER PRIMARY KEY,
    track_id   INTEGER NOT NULL,
    strand     TEXT    NOT NULL,                   -- 'odyssey', 'herodotus'
    ref        TEXT    NOT NULL,                   -- e.g. 'Od. 16.201-225'
    position   INTEGER NOT NULL,                   -- order within the strand
    covered_on TEXT,                               -- date translated in class; NULL = not yet
    UNIQUE (strand, ref),
    FOREIGN KEY (track_id) REFERENCES tracks(track_id)
);

-- My standing instructions to the planner ("no maths on Friday evenings").
-- Soft rules: the model follows them. Hard limits (times, caps) live in fixed.json.
-- Only I write these; the planner has no tool to change them.
CREATE TABLE rules (
    rule_id    INTEGER PRIMARY KEY,
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))  -- removed = 0, kept for history
);

-- ---------------------------------------------------------------- knowledge base
-- Things I tell the coach about myself ("too tired for Greek in the mornings").
-- Raw evidence for the weekly review. The planner never reads these: a note only
-- changes the plan once the review turns it into a rule/block and I approve it.
CREATE TABLE notes (
    note_id     INTEGER PRIMARY KEY,
    created_at  TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    reviewed_at TEXT                               -- NULL until a weekly review has read it
);

-- Hard rule: never plan this track inside this window on these days.
-- Enforced in tools.py like the daily cap, so no prompt wording gets past it.
CREATE TABLE track_blocks (
    block_id   INTEGER PRIMARY KEY,
    track_id   INTEGER NOT NULL,
    days       TEXT    NOT NULL,                   -- 'sat,sun' (mon..sun, comma-separated)
    start_time TEXT    NOT NULL,                   -- 'HH:MM'
    end_time   TEXT    NOT NULL,
    reason     TEXT,
    created_at TEXT    NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (end_time > start_time),
    FOREIGN KEY (track_id) REFERENCES tracks(track_id)
);

-- One row per weekly review run.
CREATE TABLE reviews (
    review_id  INTEGER PRIMARY KEY,
    created_at TEXT    NOT NULL,
    summary    TEXT    NOT NULL,
    finished   INTEGER NOT NULL CHECK (finished IN (0, 1)),
    cost_usd   REAL
);

-- Changes to the planner's INSTRUCTIONS (rules, guidance, blocks, targets) that a
-- review suggests. Kept apart from `proposals`, which change the PLAN: payloads vary
-- by action, so here it's JSON, validated in db.py before insert and again on approve.
CREATE TABLE instruction_changes (
    change_id  INTEGER PRIMARY KEY,
    review_id  INTEGER,
    created_at TEXT    NOT NULL,
    action     TEXT    NOT NULL CHECK (action IN ('add_rule', 'remove_rule', 'set_guidance',
                                                  'add_block', 'remove_block', 'set_weekly_target')),
    payload    TEXT    NOT NULL,                   -- canonical JSON (sorted keys)
    reason     TEXT    NOT NULL,                   -- the evidence, shown to me
    status     TEXT    NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_at TEXT,
    FOREIGN KEY (review_id) REFERENCES reviews(review_id)
);

CREATE INDEX idx_items_track        ON items(track_id);
CREATE INDEX idx_sessions_start     ON sessions(start_at);
CREATE INDEX idx_completions_item   ON completions(item_id, logged_at);
-- Stops the nightly planner piling up the same proposal every evening
CREATE UNIQUE INDEX idx_proposals_one_pending
    ON proposals(action, target_id) WHERE status = 'pending';
CREATE UNIQUE INDEX idx_changes_one_pending
    ON instruction_changes(action, payload) WHERE status = 'pending';

-- Defence in depth: the planner's tools already refuse locked sessions, but this
-- makes the database itself refuse to move, re-item or cancel one. To change a
-- locked session, unlock it first (a separate UPDATE). Calendar sync can still
-- set calendar_event_id because that column isn't checked.
CREATE TRIGGER sessions_locked_guard
BEFORE UPDATE OF item_id, start_at, end_at, status ON sessions
WHEN OLD.locked = 1 AND NEW.locked = 1
BEGIN
    SELECT RAISE(ABORT, 'session is locked');
END;

-- Schema version: must equal len(db.MIGRATIONS). Bump both together.
PRAGMA user_version = 5;
