"""Tests for schema migrations, proposals, locked sessions and weekly priorities.
Run with:  python -m unittest -v
"""

import sqlite3
import unittest
from pathlib import Path

import db

ROOT = Path(__file__).parent


def schema_of(conn) -> dict:
    """Every table's columns plus every index/trigger name: the 'shape' of a db."""
    shape = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        shape[name] = [(r[1], r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({name})")]
    shape["_objects"] = sorted(tuple(r) for r in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('index', 'trigger') "
        "AND name NOT LIKE 'sqlite_autoindex%'"))
    return shape


class MigrationTest(unittest.TestCase):
    def test_fresh_db_is_latest_version(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        self.assertEqual(db.schema_version(conn), len(db.MIGRATIONS))

    def test_migrated_db_matches_fresh_db_and_keeps_data(self):
        # Old database: version-0 schema + the seed data
        old = sqlite3.connect(":memory:")
        old.executescript((ROOT / "fixtures" / "schema_v0.sql").read_text())
        seed = (ROOT / "seed.sql").read_text()
        old.executescript(seed)
        sessions_before = old.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        self.assertEqual(db.migrate(old), list(range(1, len(db.MIGRATIONS) + 1)))
        self.assertEqual(db.migrate(old), [])   # running again does nothing

        fresh = db.connect(":memory:")
        db.init_db(fresh)
        self.assertEqual(schema_of(old), schema_of(fresh))
        self.assertEqual(old.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], sessions_before)
        self.assertEqual(old.execute("SELECT COUNT(*) FROM sessions WHERE locked = 0").fetchone()[0],
                         sessions_before)


class PlannerTablesTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)

    def tearDown(self):
        self.conn.close()

    def test_brief_and_lock(self):
        sid = db.plan_session(self.conn, 1, "2026-10-01 19:00", "2026-10-01 19:45",
                              brief="Parse lines 1-10, then drill the aorist.", locked=True)
        s = db.get_session(self.conn, sid)
        self.assertEqual(s["brief"], "Parse lines 1-10, then drill the aorist.")
        self.assertEqual(s["locked"], 1)

    def test_locked_session_cannot_be_cancelled_or_moved(self):
        sid = db.plan_session(self.conn, 1, "2026-10-01 19:00", "2026-10-01 19:45", locked=True)
        with self.assertRaises(sqlite3.IntegrityError):
            db.cancel_session(self.conn, sid)
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction(self.conn):
                self.conn.execute("UPDATE sessions SET start_at = '2026-10-01 20:00' "
                                  "WHERE session_id = ?", (sid,))
        db.set_calendar_event_id(self.conn, sid, "evt123")   # sync still allowed
        db.set_session_locked(self.conn, sid, False)
        self.assertEqual(db.cancel_session(self.conn, sid)["status"], "cancelled")

    def test_approve_proposal_runs_action(self):
        pid = db.create_proposal(self.conn, "pause_track", 5, "No reading done in 2 weeks")
        self.assertEqual(len(db.pending_proposals(self.conn)), 1)
        self.assertEqual(db.decide_proposal(self.conn, pid, approve=True)["status"], "approved")
        active = {t["track_id"] for t in db.list_tracks(self.conn)}
        self.assertNotIn(5, active)
        with self.assertRaises(ValueError):
            db.decide_proposal(self.conn, pid, approve=True)   # already decided

    def test_reject_proposal_changes_nothing(self):
        pid = db.create_proposal(self.conn, "mark_item_done", 2, "Logged 85 of 90 minutes")
        db.decide_proposal(self.conn, pid, approve=False)
        self.assertNotEqual(db.get_item(self.conn, 2)["status"], "done")

    def test_duplicate_pending_proposal_rejected(self):
        db.create_proposal(self.conn, "pause_track", 5, "first")
        with self.assertRaises(sqlite3.IntegrityError):
            db.create_proposal(self.conn, "pause_track", 5, "second")

    def test_failed_approval_rolls_back(self):
        sid = db.plan_session(self.conn, 1, "2026-10-01 19:00", "2026-10-01 19:45")
        pid = db.create_proposal(self.conn, "cancel_session", sid, "Clashes with nothing, just surplus")
        db.set_session_locked(self.conn, sid, True)   # I pin it before approving
        with self.assertRaises(sqlite3.IntegrityError):
            db.decide_proposal(self.conn, pid, approve=True)
        self.assertEqual(db.pending_proposals(self.conn)[0]["proposal_id"], pid)  # still pending
        self.assertEqual(db.get_session(self.conn, sid)["status"], "planned")

    def test_passages_and_coverage(self):
        self.conn.executemany(
            "INSERT INTO passages (track_id, strand, ref, position) VALUES (1, 'odyssey', ?, ?)",
            [("Od. 16.201-225", 1), ("Od. 16.226-250", 2), ("Od. 16.251-275", 3)])
        pid = self.conn.execute("SELECT passage_id FROM passages WHERE position = 2").fetchone()[0]
        self.assertEqual(db.mark_covered(self.conn, pid, "2026-09-30"), 2)   # 1 and 2
        cov = db.coverage(self.conn)[0]
        self.assertEqual(cov["covered"], ["Od. 16.201-225", "Od. 16.226-250"])
        self.assertEqual(cov["next_in_class"], "Od. 16.251-275")
        self.assertEqual(db.mark_covered(self.conn, pid), 0)   # already covered

    def test_rules(self):
        rid = db.add_rule(self.conn, "  No maths on Friday evenings ")
        db.add_rule(self.conn, "Greek before CS on the same night")
        self.assertEqual([r["text"] for r in db.list_rules(self.conn)],
                         ["No maths on Friday evenings", "Greek before CS on the same night"])
        db.remove_rule(self.conn, rid)
        self.assertEqual(len(db.list_rules(self.conn)), 1)
        self.assertEqual(len(db.list_rules(self.conn, include_removed=True)), 2)
        with self.assertRaises(ValueError):
            db.remove_rule(self.conn, rid)
        with self.assertRaises(ValueError):
            db.add_rule(self.conn, "   ")

    def test_weekly_priorities(self):
        self.assertEqual(db.week_start("2026-10-04"), "2026-09-28")   # Sunday -> its Monday
        db.set_weekly_priorities(self.conn, "TMUA paper 1; Greek homework", "2026-09-30")
        self.assertEqual(db.get_weekly_priorities(self.conn, "2026-10-02"),
                         "TMUA paper 1; Greek homework")
        db.set_weekly_priorities(self.conn, "Just TMUA", "2026-10-01")   # replaces
        self.assertEqual(db.get_weekly_priorities(self.conn, "2026-09-28"), "Just TMUA")
        self.assertIsNone(db.get_weekly_priorities(self.conn, "2026-10-05"))


if __name__ == "__main__":
    unittest.main()
