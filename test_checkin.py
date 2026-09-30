"""Tests for the evening check-in flow (no Telegram needed). Run: python -m unittest -v

Day 2026-09-30: seed session 9 (TMUA past paper, item 6, 18:00-20:30) plus a session
added in setUp, SID_HW (CS Big O worksheet, item 9, 20:40-21:20). Both unlogged.
"""

import unittest

import checkin
import db

DAY = "2026-09-30"


def buttons(screen):
    return [data for row in screen.buttons for _, data in row]


class CheckinTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)
        self.hw = db.plan_session(self.conn, 9, "2026-09-30 20:40", "2026-09-30 21:20")

    def tearDown(self):
        self.conn.close()

    def first(self):
        return checkin.next_screen(self.conn, DAY)

    def test_first_screen_asks_about_first_session(self):
        s = self.first()
        self.assertIn("18:00–20:30", s.text)
        self.assertIn("(2 left)", s.text)
        self.assertEqual(buttons(s), ["o:9:done", "o:9:partial", "o:9:skipped"])

    def test_partial_flow_logs_and_moves_on(self):
        r = checkin.handle(self.conn, "o:9:partial", DAY)
        self.assertIn("m:9:partial:150", buttons(r.screen))         # planned 150 offered
        r = checkin.handle(self.conn, "m:9:partial:75", DAY)
        self.assertEqual(buttons(r.screen), [f"c:9:partial:75:{c}" for c in range(1, 6)])
        r = checkin.handle(self.conn, "c:9:partial:75:3", DAY)
        self.assertIn("Logged", r.screen.text)
        self.assertIn(f"o:{self.hw}:done", buttons(r.screen))       # next session
        self.assertEqual(db.get_item(self.conn, 6)["status"], "in_progress")

    def test_done_asks_if_item_finished(self):
        r = checkin.handle(self.conn, f"c:{self.hw}:done:40:4", DAY)
        self.assertEqual(buttons(r.screen), [f"f:{self.hw}:done:40:4:1", f"f:{self.hw}:done:40:4:0"])
        checkin.handle(self.conn, f"f:{self.hw}:done:40:4:0", DAY)
        self.assertEqual(db.get_item(self.conn, 9)["status"], "in_progress")   # more to do

    def test_done_and_finished_marks_item_done(self):
        checkin.handle(self.conn, f"f:{self.hw}:done:40:4:1", DAY)
        self.assertEqual(db.get_item(self.conn, 9)["status"], "done")

    def test_open_ended_item_never_asks_finished(self):
        db.add_item(self.conn, 1, "Vocab (ongoing)", 3000)
        item = self.conn.execute("SELECT MAX(item_id) FROM items").fetchone()[0]
        sid = db.plan_session(self.conn, item, "2026-09-30 21:30", "2026-09-30 22:00")
        r = checkin.handle(self.conn, f"c:{sid}:done:30:4", DAY)
        self.assertIn("Logged", r.screen.text)
        self.assertNotEqual(db.get_item(self.conn, item)["status"], "done")

    def test_skip_logs_immediately_and_last_one_finishes(self):
        checkin.handle(self.conn, "o:9:skipped", DAY)
        r = checkin.handle(self.conn, f"o:{self.hw}:skipped", DAY)
        self.assertTrue(r.finished)
        self.assertIsNone(checkin.next_screen(self.conn, DAY))

    def test_double_tap_doesnt_double_log(self):
        checkin.handle(self.conn, "o:9:skipped", DAY)
        r = checkin.handle(self.conn, "o:9:skipped", DAY)
        self.assertIn("Already logged", r.screen.text)
        n = self.conn.execute("SELECT COUNT(*) FROM completions WHERE session_id = 9").fetchone()[0]
        self.assertEqual(n, 1)

    def test_button_data_fits_telegram_limit(self):
        data = "f:99999:partial:120:5:1"
        self.assertLessEqual(len(data.encode()), 64)

    def test_proposals(self):
        pid = db.create_proposal(self.conn, "pause_track", 3, "No coding logged for 2 weeks")
        screens = checkin.proposal_screens(self.conn)
        self.assertIn("pause the Coding/ML track", screens[0].text)
        r = checkin.handle(self.conn, f"p:{pid}:1")
        self.assertTrue(r.changed_plan)
        r = checkin.handle(self.conn, f"p:{pid}:1")
        self.assertIn("Couldn't", r.screen.text)       # already decided


if __name__ == "__main__":
    unittest.main()
