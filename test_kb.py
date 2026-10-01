"""Tests for the knowledge base: notes, track blocks, instruction changes, the
review input, and blocks enforced by the planner tools. Run: python -m unittest -v

Seed data, "now" = Tue 2026-09-29 12:00. Item 8 (gradient descent) is track 3
(Coding/ML); item 6 (TMUA past paper) is track 2. Thursday 2026-10-01 is free from
18:15 (school until 18:00 + 15 min buffer) to 22:00.
"""

import sqlite3
import unittest

import availability
import db
import tools

NOW = "2026-09-29 12:00"


class NotesTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)

    def tearDown(self):
        self.conn.close()

    def test_notes_reviewed_only_up_to_what_the_review_saw(self):
        a = db.add_note(self.conn, " too tired for Greek in the mornings ")
        b = db.add_note(self.conn, "TMUA paper 2 questions take me ages")
        db.mark_notes_reviewed(self.conn, a)            # b arrived mid-review
        self.assertEqual([n["note_id"] for n in db.list_notes(self.conn, unreviewed_only=True)], [b])
        self.assertEqual(db.list_notes(self.conn)[0]["text"], "too tired for Greek in the mornings")
        with self.assertRaises(ValueError):
            db.add_note(self.conn, "   ")

    def test_parse_days(self):
        self.assertEqual(db.parse_days("weekend"), ["sat", "sun"])
        self.assertEqual(db.parse_days(["Sunday", "mon"]), ["mon", "sun"])
        self.assertEqual(len(db.parse_days("all")), 7)
        with self.assertRaises(ValueError):
            db.parse_days("someday")


class ChangesTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)

    def tearDown(self):
        self.conn.close()

    def test_validation_refuses_bad_payloads(self):
        bad = [("add_rule", {}), ("set_guidance", {"track_id": 99, "guidance": "x"}),
               ("add_block", {"track_id": 1, "days": "weekend", "start": "12:00", "end": "09:00"}),
               ("remove_rule", {"rule_id": 1}), ("set_weekly_target", {"track_id": 1, "minutes": 5000}),
               ("delete_everything", {})]
        for action, payload in bad:
            with self.assertRaises(ValueError, msg=action):
                db.create_change(self.conn, action, payload, "because")
        with self.assertRaises(ValueError):
            db.create_change(self.conn, "add_rule", {"text": "x"}, "  ")   # no reason

    def test_payload_is_cleaned_and_duplicates_refused(self):
        cid = db.create_change(self.conn, "add_block",
                               {"track_id": "1", "days": "weekend", "start": "9:00",
                                "end": "12:00", "junk": True}, "3 skipped Greek mornings")
        self.assertEqual(db.get_change(self.conn, cid)["payload"],
                         {"track_id": 1, "days": ["sat", "sun"], "start": "09:00", "end": "12:00"})
        with self.assertRaises(sqlite3.IntegrityError):
            db.create_change(self.conn, "add_block",
                             {"track_id": 1, "days": ["sun", "sat"], "start": "09:00", "end": "12:00"},
                             "again")

    def test_approve_each_action(self):
        rid = db.add_rule(self.conn, "No maths on Fridays")
        cases = [
            ("add_rule", {"text": "Greek before CS on the same night"}),
            ("remove_rule", {"rule_id": rid}),
            ("set_guidance", {"track_id": 1, "guidance": "Weekday evenings only."}),
            ("set_weekly_target", {"track_id": 3, "minutes": 120}),
        ]
        for action, payload in cases:
            cid = db.create_change(self.conn, action, payload, "evidence")
            self.assertEqual(db.decide_change(self.conn, cid, True)["status"], "approved")
        self.assertEqual([r["text"] for r in db.list_rules(self.conn)],
                         ["Greek before CS on the same night"])
        tracks = {t["track_id"]: t for t in db.list_tracks(self.conn, active_only=False)}
        self.assertEqual(tracks[1]["guidance"], "Weekday evenings only.")
        self.assertEqual(tracks[3]["weekly_target_minutes"], 120)

    def test_approving_a_block_cancels_clashing_future_sessions_only(self):
        clash = db.plan_session(self.conn, 8, "2026-10-03 10:00", "2026-10-03 11:00")    # Sat
        later = db.plan_session(self.conn, 8, "2026-10-03 14:00", "2026-10-03 15:00")    # Sat pm
        pinned = db.plan_session(self.conn, 8, "2026-10-04 09:30", "2026-10-04 10:30", locked=True)
        cid = db.create_change(self.conn, "add_block",
                               {"track_id": 3, "days": "weekend", "start": "09:00", "end": "12:00"},
                               "skipped every weekend morning")
        ch = db.decide_change(self.conn, cid, True)
        self.assertEqual(ch["sessions_cancelled"], 1)
        self.assertEqual(db.get_session(self.conn, clash)["status"], "cancelled")
        self.assertEqual(db.get_session(self.conn, later)["status"], "planned")
        self.assertEqual(db.get_session(self.conn, pinned)["status"], "planned")
        self.assertEqual(db.list_blocks(self.conn)[0]["reason"], "skipped every weekend morning")

    def test_reject_changes_nothing_and_revalidates_on_approve(self):
        rid = db.add_rule(self.conn, "No maths on Fridays")
        cid = db.create_change(self.conn, "remove_rule", {"rule_id": rid}, "never relevant")
        db.remove_rule(self.conn, rid)                      # I removed it by hand meanwhile
        with self.assertRaises(ValueError):
            db.decide_change(self.conn, cid, True)
        self.assertEqual(db.get_change(self.conn, cid)["status"], "pending")
        db.decide_change(self.conn, cid, False)
        with self.assertRaises(ValueError):
            db.decide_change(self.conn, cid, True)          # already decided

    def test_expire_and_describe(self):
        cid = db.create_change(self.conn, "add_block",
                               {"track_id": 1, "days": "weekend", "start": "09:00", "end": "12:00"}, "x")
        self.assertIn("never plan Ancient Greek 09:00-12:00 on sat, sun",
                      db.describe_change(self.conn, db.get_change(self.conn, cid)))
        self.assertEqual(db.expire_changes(self.conn), 1)
        self.assertEqual(db.pending_changes(self.conn), [])


class SnapshotTest(unittest.TestCase):
    def test_kb_snapshot_has_evidence_and_instructions(self):
        conn = db.connect(":memory:")
        db.init_db(conn, seed=True)
        db.add_note(conn, "too tired for Greek in the mornings")
        snap = db.kb_snapshot(conn, "2026-09-30")
        self.assertEqual(snap["new_notes"][0]["text"], "too tired for Greek in the mornings")
        # Sat 26 Sept 10:00 Greek partial; Sun 27 Sept 14:00 Coding skipped
        greek_weekend = next(p for p in snap["session_patterns_28d"]
                             if p["track"] == "Ancient Greek" and p["day_type"] == "weekend")
        self.assertEqual((greek_weekend["time_of_day"], greek_weekend["partial"]), ("morning", 1))
        skipped = sum(p["skipped"] for p in snap["session_patterns_28d"])
        self.assertEqual(skipped, 2)
        self.assertIn("standing_rules", snap["current_instructions"])
        self.assertTrue(any(c["note"] == "too tired" for c in snap["completion_notes_28d"]))
        conn.close()


class BlockEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)
        db.add_block(self.conn, 3, "weekdays", "18:00", "20:00", "too tired straight after school")
        self.ctx = tools.Context(self.conn, availability.load_config(), NOW)

    def tearDown(self):
        self.conn.close()

    def plan(self, item, start, end):
        return tools.run_tool(self.ctx, "plan_session",
                              {"item_id": item, "start": start, "end": end, "brief": "x"})

    def test_blocked_track_refused_inside_window(self):
        r = self.plan(8, "2026-10-01 19:30", "2026-10-01 20:30")     # overlaps by 30 min
        self.assertIn("blocked 18:00-20:00", r["error"])

    def test_blocked_track_allowed_outside_window(self):
        self.assertTrue(self.plan(8, "2026-10-01 20:00", "2026-10-01 21:00")["ok"])
        self.assertTrue(self.plan(8, "2026-10-03 18:00", "2026-10-03 19:00")["ok"])   # Saturday

    def test_other_tracks_unaffected(self):
        self.assertTrue(self.plan(6, "2026-10-01 18:30", "2026-10-01 19:30")["ok"])

    def test_move_into_block_refused(self):
        sid = self.plan(8, "2026-10-01 20:00", "2026-10-01 21:00")["session_id"]
        r = tools.run_tool(self.ctx, "move_session", {"session_id": sid,
                           "new_start": "2026-10-02 18:30", "new_end": "2026-10-02 19:30"})
        self.assertIn("blocked", r["error"])


if __name__ == "__main__":
    unittest.main()
