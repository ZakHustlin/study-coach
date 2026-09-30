"""Tests for the data layer. Run with:  python -m unittest -v

Each test gets a fresh in-memory database loaded with seed.sql, with "today"
fixed at 2026-09-29 so the date-based queries give the same answers every time.
"""

import sqlite3
import unittest

import db

TODAY = "2026-09-29"


class DataLayerTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)

    def tearDown(self):
        self.conn.close()

    # --- the five test queries from the project context

    def test_1_completion_rates(self):
        rates = {r["track"]: (r["planned"], r["done"]) for r in db.completion_rates(self.conn, TODAY)}
        self.assertEqual(rates["Ancient Greek"], (3, 1))
        self.assertEqual(rates["TMUA maths"], (2, 2))
        self.assertEqual(rates["Schoolwork"], (1, 0))

    def test_2_estimate_accuracy(self):
        acc = {r["track"]: r["accuracy"] for r in db.estimate_accuracy(self.conn)}
        self.assertEqual(acc["TMUA maths"], 1.33)   # 200 actual / 150 estimated

    def test_3_stale_low_confidence(self):
        titles = [r["title"] for r in db.stale_low_confidence(self.conn, TODAY)]
        # sequences & series is also confidence 2, but was touched today -> excluded
        self.assertEqual(titles, ["Greek: aorist tense"])

    def test_4_next_items(self):
        nxt = {r["track"]: r["title"] for r in db.next_items(self.conn)}
        self.assertEqual(nxt["TMUA maths"], "TMUA: past paper 1")        # both deps done
        self.assertEqual(nxt["Ancient Greek"], "Greek: Xenophon passage 1")  # passage 2 blocked
        self.assertNotIn("Reading", nxt)                                  # paused track

    def test_5_day_summary(self):
        rows = db.day_summary(self.conn, TODAY)
        self.assertEqual([r["plan"] for r in rows], ["unplanned", "planned 60m", "planned 40m"])

    # --- writes

    def test_log_completion_updates_item_status(self):
        item = db.add_item(self.conn, 1, "Greek: middle voice", 45)
        db.log_completion(self.conn, item, "partial", 20, confidence=2)
        self.assertEqual(db.get_item(self.conn, item)["status"], "in_progress")
        db.log_completion(self.conn, item, "done", 30, confidence=4)
        self.assertEqual(db.get_item(self.conn, item)["status"], "done")

    def test_skipped_clears_minutes_and_confidence(self):
        cid = db.log_completion(self.conn, 3, "skipped", 50, confidence=5)
        row = self.conn.execute("SELECT * FROM completions WHERE completion_id = ?", (cid,)).fetchone()
        self.assertEqual((row["minutes_spent"], row["confidence"]), (0, None))

    def test_completion_must_match_session_item(self):
        with self.assertRaises(ValueError):
            db.log_completion(self.conn, 3, "done", 30, session_id=1)  # session 1 is item 4

    def test_dependency_cycle_rejected(self):
        # seed has 3 -> 2; adding 2 -> 3 would make both unreachable
        with self.assertRaises(ValueError):
            db.add_dependency(self.conn, 2, 3)

    def test_self_dependency_rejected_by_schema(self):
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_dependency(self.conn, 3, 3)

    def test_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_item(self.conn, 999, "orphan", 10)

    def test_cancelled_session_not_counted(self):
        db.cancel_session(self.conn, 8)  # the skipped homework session today
        rates = {r["track"] for r in db.completion_rates(self.conn, TODAY)}
        self.assertNotIn("Schoolwork", rates)

    def test_add_item_appends_position(self):
        item = db.add_item(self.conn, 2, "TMUA: past paper 2", 150)
        self.assertEqual(db.get_item(self.conn, item)["position"], 4)

    def test_unlogged_sessions(self):
        s = db.plan_session(self.conn, 3, "2026-09-29 20:00", "2026-09-29 21:00")
        ids = [r["session_id"] for r in db.unlogged_sessions(self.conn, TODAY)]
        self.assertEqual(ids, [s])

    def test_week_progress_splits_logged_and_ahead(self):
        rows = {r["track"]: r for r in db.week_progress(self.conn, "2026-09-30", "2026-09-30 14:00")}
        self.assertEqual(rows["TMUA maths"]["minutes_planned_ahead"], 150)   # tonight's paper
        self.assertEqual(rows["Ancient Greek"]["minutes_planned_ahead"], 0)  # Tuesday's is past
        self.assertEqual(rows["Ancient Greek"]["minutes_logged"], 50)

    def test_planner_snapshot_has_everything(self):
        snap = db.planner_snapshot(self.conn, TODAY)
        self.assertEqual(set(snap), {"today", "tracks", "next_items", "completion_rates_14d",
                                     "estimate_accuracy", "needs_review", "today_summary",
                                     "weekly_priorities", "pending_proposals"})


if __name__ == "__main__":
    unittest.main()
