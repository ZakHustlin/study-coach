"""Tests for the weekly review loop and the review-suggestion buttons, using the
scripted fake model from test_planner (no API key needed). Run: python -m unittest -v
"""

import json
import unittest

import checkin
import db
import reviewer
from test_planner import FakeClient, call, text

NOW = "2026-10-04 18:00"      # a Sunday


class ReviewerTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)
        self.note = db.add_note(self.conn, "too tired for Greek in the mornings")

    def tearDown(self):
        self.conn.close()

    def test_note_becomes_a_suggestion_then_a_block(self):
        client = FakeClient([
            [call("propose_change", 1, action="add_block",
                  payload={"track_id": 1, "days": ["weekend"], "start": "09:00", "end": "12:00"},
                  reason="You said you're too tired for Greek in the mornings.")],
            [call("finish", 2, summary="Suggested moving Greek out of weekend mornings.")],
        ])
        result = reviewer.run(self.conn, client, "m", NOW)
        self.assertTrue(result.finished)
        self.assertEqual(len(result.change_ids), 1)
        # the model saw the note and the auto-logged patterns
        first = client.requests[0]["messages"][0]["content"]
        self.assertIn("too tired for Greek in the mornings", first)
        self.assertIn("session_patterns_28d", first)
        # note is now reviewed; review saved
        self.assertEqual(db.list_notes(self.conn, unreviewed_only=True), [])
        self.assertIn("weekend mornings", db.last_review(self.conn)["summary"])
        # approve through the same button path Telegram uses
        screen = checkin.change_screens(self.conn)[0]
        self.assertIn("never plan Ancient Greek 09:00-12:00 on sat, sun", screen.text)
        approve = screen.buttons[0][0][1]
        r = checkin.handle(self.conn, approve)
        self.assertTrue(r.screen.text.startswith("Approved"))
        self.assertEqual(db.list_blocks(self.conn)[0]["track_id"], 1)

    def test_bad_suggestion_is_fed_back_and_nothing_saved(self):
        client = FakeClient([
            [call("propose_change", 1, action="set_weekly_target",
                  payload={"track_id": 42, "minutes": 60}, reason="x")],
            [call("finish", 2, summary="Nothing clear this week.")],
        ])
        result = reviewer.run(self.conn, client, "m", NOW)
        self.assertEqual(result.change_ids, [])
        err = client.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(err["is_error"])
        self.assertIn("no track 42", err["content"])

    def test_unfinished_review_leaves_notes_unreviewed(self):
        client = FakeClient([[text("Hmm.")], [text("Still thinking.")]])
        result = reviewer.run(self.conn, client, "m", NOW)
        self.assertFalse(result.finished)
        self.assertEqual(len(client.requests), 2)            # nudged once, then gave up
        self.assertEqual(len(db.list_notes(self.conn, unreviewed_only=True)), 1)

    def test_new_review_expires_old_suggestions_and_sees_decisions(self):
        old = db.create_change(self.conn, "add_rule", {"text": "Greek first"}, "old")
        rejected = db.create_change(self.conn, "set_weekly_target",
                                    {"track_id": 3, "minutes": 60}, "cut coding")
        db.decide_change(self.conn, rejected, False)
        client = FakeClient([[call("finish", 1, summary="ok")]])
        reviewer.run(self.conn, client, "m", NOW)
        self.assertEqual(db.get_change(self.conn, old)["status"], "expired")
        snap = json.loads(client.requests[0]["messages"][0]["content"].split("\n", 1)[1])
        self.assertEqual(snap["past_decisions"][0]["status"], "rejected")

    def test_suggestion_limit(self):
        replies = [[call("propose_change", i, action="add_rule", payload={"text": f"rule {i}"},
                         reason="r")] for i in range(reviewer.MAX_CHANGES_PER_REVIEW + 1)]
        replies.append([call("finish", 99, summary="done")])
        result = reviewer.run(self.conn, FakeClient(replies), "m", NOW)
        self.assertEqual(len(result.change_ids), reviewer.MAX_CHANGES_PER_REVIEW)

    def test_reject_button(self):
        cid = db.create_change(self.conn, "add_rule", {"text": "Greek first"}, "because")
        r = checkin.handle(self.conn, f"k:{cid}:0")
        self.assertTrue(r.screen.text.startswith("Rejected"))
        self.assertEqual(db.list_rules(self.conn), [])
        self.assertIn("Couldn't", checkin.handle(self.conn, f"k:{cid}:1").screen.text)

    def test_reviewers_use_known_providers(self):
        import planner
        for name in reviewer.REVIEWERS:
            self.assertIn(name, planner.PROVIDERS)


if __name__ == "__main__":
    unittest.main()
