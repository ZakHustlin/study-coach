"""Tests for the planner loop, using a fake model that follows a script, so no API
key or money is needed. Run with:  python -m unittest -v
"""

import unittest
from types import SimpleNamespace as NS

import availability
import db
import planner
import tools

NOW = "2026-09-29 12:00"


def text(t):
    return NS(type="text", text=t)


def call(name, i, **args):
    return NS(type="tool_use", id=f"call_{i}", name=name, input=args)


class FakeClient:
    """Stands in for anthropic.Anthropic(). Returns scripted replies in order and
    records every request, so tests can check what the model was sent."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot: the loop keeps appending
        content = self.replies.pop(0)
        stop = "max_tokens" if content == "MAX" else (
            "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn")
        if content == "MAX":
            content = [NS(type="thinking", thinking="Let me consider every track...")]
        return NS(content=content, stop_reason=stop,
                  usage=NS(input_tokens=1000, output_tokens=100,
                                            cache_creation_input_tokens=0,
                                            cache_read_input_tokens=0))


class PlannerLoopTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)
        self.ctx = tools.Context(self.conn, availability.load_config(), NOW)

    def tearDown(self):
        self.conn.close()

    def test_error_is_fed_back_then_model_recovers(self):
        client = FakeClient([
            [text("Plan gradient descent Thursday."),
             call("plan_session", 1, item_id=8, start="2026-10-01 18:00",
                  end="2026-10-01 19:00", brief="Derive, then code it.")],   # clashes with school
            [call("plan_session", 2, item_id=8, start="2026-10-01 18:30",
                  end="2026-10-01 19:30", brief="Derive, then code it.")],
            [call("finish", 3, summary="Added gradient descent on Thursday.")],
        ])
        result = planner.run(self.ctx, client)
        self.assertTrue(result.finished)
        self.assertEqual(result.steps, 3)
        self.assertEqual(len(result.changes), 1)
        # the failed call's error went back to the model as an error tool_result
        first_results = client.requests[1]["messages"][-1]["content"]
        self.assertTrue(first_results[0]["is_error"])
        self.assertIn("isn't free", first_results[0]["content"])

    def test_first_message_holds_state(self):
        client = FakeClient([[call("finish", 1, summary="Nothing to change.")]])
        planner.run(self.ctx, client)
        first = client.requests[0]["messages"][0]["content"]
        for key in ("plan_until", "tracks_this_week", "free_time", "available_items"):
            self.assertIn(key, first)
        self.assertIn("cache_control", client.requests[0]["tools"][-1])

    def test_work_done_before_finish_in_same_reply(self):
        client = FakeClient([[
            call("finish", 1, summary="done"),
            call("add_item", 2, track_id=1, title="Aorist revision", est_minutes=30),
        ]])
        result = planner.run(self.ctx, client)
        self.assertEqual(len(result.changes), 1)

    def test_nudged_once_then_stops(self):
        client = FakeClient([[text("All good.")], [text("Really, all good.")]])
        result = planner.run(self.ctx, client)
        self.assertFalse(result.finished)
        self.assertEqual(len(client.requests), 2)

    def test_max_tokens_stops_and_says_why(self):
        client = FakeClient(["MAX"])
        result = planner.run(self.ctx, client)
        self.assertFalse(result.finished)
        self.assertIn("max_tokens", result.summary)
        self.assertEqual(result.trace[0]["blocks"], ["thinking"])

    def test_step_limit(self):
        loop = [[call("get_sessions", i, date_from="2026-09-29", date_to="2026-10-04")]
                for i in range(planner.MAX_STEPS)]
        result = planner.run(self.ctx, FakeClient(loop))
        self.assertFalse(result.finished)
        self.assertEqual(result.steps, planner.MAX_STEPS)

    def test_cost_estimate(self):
        haiku = planner.PROVIDERS["anthropic"]["prices"]
        r = planner.RunResult("", [], 1, {"input": 1_000_000, "output": 100_000}, prices=haiku)
        self.assertAlmostEqual(r.cost_usd, 1.5)

    def test_providers_complete(self):
        for name, p in planner.PROVIDERS.items():
            self.assertEqual(set(p), {"base_url", "key_env", "model", "prices"}, name)

    def test_dry_run_copy_is_separate(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "coach.db"
            real = db.connect(path)
            db.init_db(real, seed=True)
            real.close()
            copy = planner.copy_db(path)
            db.plan_session(copy, 8, "2026-10-01 18:30", "2026-10-01 19:30")
            real = db.connect(path)
            self.assertEqual(real.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 9)
            real.close()


if __name__ == "__main__":
    unittest.main()
