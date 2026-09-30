"""Tests for the planner's tools and their hard rules. Run with:  python -m unittest -v

Seed data, "now" = Tue 2026-09-29 12:00, so the planning horizon ends Sun 2026-10-04.
Already planned: Tue 18:00-19:00 and 19:15-19:55, Wed 18:00-20:30 (150 min).
Available items: 2 (Greek passage 1), 6 (TMUA past paper), 8 (gradient descent),
9 (CS homework, due Thu 2026-10-01). Item 3 waits on item 2; item 10's track is paused.
"""

import unittest

import availability
import db
import tools

NOW = "2026-09-29 12:00"


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)
        self.ctx = tools.Context(self.conn, availability.load_config(), NOW)

    def tearDown(self):
        self.conn.close()

    def call(self, name, **args):
        return tools.run_tool(self.ctx, name, args)

    def assertError(self, result, fragment):
        self.assertIn("error", result, result)
        self.assertIn(fragment, result["error"])

    # --- planning rules

    def test_plan_valid_session(self):
        r = self.call("plan_session", item_id=8, start="2026-10-01 18:30",
                      end="2026-10-01 19:30", brief="Derive the update rule, then code it.")
        self.assertTrue(r["ok"])
        self.assertEqual(db.get_session(self.conn, r["session_id"])["brief"],
                         "Derive the update rule, then code it.")

    def test_cannot_overlap_school_or_buffer(self):
        # Thursday school ends 18:00, +15 min buffer
        self.assertError(self.call("plan_session", item_id=8, start="2026-10-01 18:00",
                                   end="2026-10-01 19:00", brief="x"), "isn't free")

    def test_gap_between_sessions(self):
        self.assertError(self.call("plan_session", item_id=8, start="2026-09-29 19:55",
                                   end="2026-09-29 20:30", brief="x"), "isn't free")
        self.assertTrue(self.call("plan_session", item_id=8, start="2026-09-29 20:05",
                                  end="2026-09-29 20:45", brief="x")["ok"])

    def test_daily_cap(self):
        # Wednesday already has 150 of 180 minutes
        self.assertError(self.call("plan_session", item_id=8, start="2026-09-30 20:40",
                                   end="2026-09-30 21:20", brief="x"), "daily cap")

    def test_due_date(self):
        self.assertError(self.call("plan_session", item_id=9, start="2026-10-01 18:30",
                                   end="2026-10-01 19:10", brief="x"), "due 2026-10-01")
        self.assertTrue(self.call("plan_session", item_id=9, start="2026-09-29 20:10",
                                  end="2026-09-29 20:50", brief="x")["ok"])

    def test_time_rules(self):
        self.assertError(self.call("plan_session", item_id=8, start="2026-09-28 18:00",
                                   end="2026-09-28 19:00", brief="x"), "past")
        self.assertError(self.call("plan_session", item_id=8, start="2026-10-05 10:00",
                                   end="2026-10-05 11:00", brief="x"), "up to 2026-10-04")
        self.assertError(self.call("plan_session", item_id=6, start="2026-10-03 10:00",
                                   end="2026-10-03 13:20", brief="x"), "Split")

    def test_item_must_be_available(self):
        self.assertError(self.call("plan_session", item_id=3, start="2026-10-03 10:00",
                                   end="2026-10-03 11:00", brief="x"), "can't be planned")
        self.assertError(self.call("plan_session", item_id=10, start="2026-10-03 10:00",
                                   end="2026-10-03 11:00", brief="x"), "can't be planned")

    def test_big_item_can_be_split(self):
        for day in ("2026-10-03", "2026-10-04"):
            self.assertTrue(self.call("plan_session", item_id=6, start=f"{day} 10:00",
                                      end=f"{day} 11:15", brief="Half the paper")["ok"])
        item = next(i for i in db.available_items(self.conn, NOW) if i["item_id"] == 6)
        self.assertEqual(item["minutes_planned_ahead"], 150 + 75 + 75)

    # --- moving and locking

    def test_move_session(self):
        r = self.call("move_session", session_id=9, new_start="2026-10-03 10:00",
                      new_end="2026-10-03 12:30")
        self.assertTrue(r["ok"])
        self.assertEqual(db.get_session(self.conn, 9)["status"], "cancelled")
        self.assertEqual(db.get_session(self.conn, r["new_session_id"])["item_id"], 6)

    def test_move_can_overlap_its_own_old_slot(self):
        r = self.call("move_session", session_id=9, new_start="2026-09-30 18:30",
                      new_end="2026-09-30 21:00")
        self.assertTrue(r["ok"], r)

    def test_locked_session_untouchable(self):
        db.set_session_locked(self.conn, 9, True)
        self.assertError(self.call("move_session", session_id=9, new_start="2026-10-03 10:00",
                                   new_end="2026-10-03 12:30"), "locked")
        self.assertError(self.call("propose", action="cancel_session", target_id=9,
                                   reason="x"), "locked")

    # --- items and proposals

    def test_add_item_after(self):
        r = self.call("add_item", track_id=1, title="Greek: aorist revision",
                      est_minutes=30, after_item_id=1)
        pos = {i["item_id"]: i["position"] for i in db.list_items(self.conn, 1, include_done=True)}
        self.assertEqual(pos[r["item_id"]], 2)
        self.assertEqual(pos[2], 3)       # later items shifted down
        self.assertError(self.call("add_item", track_id=5, title="x", est_minutes=30), "no active track")

    def test_propose(self):
        self.assertTrue(self.call("propose", action="mark_item_done", target_id=2,
                                  reason="55 of 60 min done at confidence 3")["ok"])
        self.assertError(self.call("propose", action="mark_item_done", target_id=2,
                                   reason="again"), "already waiting")
        self.assertError(self.call("propose", action="delete_everything", target_id=1,
                                   reason="x"), "action must be")

    # --- reading and plumbing

    def test_free_slots_show_cap(self):
        days = {d["date"]: d for d in self.call("get_free_slots", date_from="2026-09-30",
                                                date_to="2026-09-30")["days"]}
        self.assertEqual(days["2026-09-30"]["cap_remaining_minutes"], 30)
        self.assertEqual(days["2026-09-30"]["slots"], [{"start": "20:40", "end": "22:00", "minutes": 80}])

    def test_sunday_plans_next_week(self):
        ctx = tools.Context(self.conn, availability.load_config(), "2026-10-04 20:00")
        self.assertEqual(ctx.horizon_end.isoformat(), "2026-10-11")

    def test_bad_arguments_become_errors(self):
        self.assertError(self.call("plan_session", item_id=8), "bad arguments")
        self.assertError(self.call("drop_table"), "unknown tool")

    def test_every_tool_has_a_schema(self):
        self.assertEqual({s["name"] for s in tools.SCHEMAS},
                         set(tools.FUNCTIONS) | {"finish"})


if __name__ == "__main__":
    unittest.main()
