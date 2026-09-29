"""Tests for free slots and calendar sync. Run with:  python -m unittest -v

Sync is tested against FakeCalendar, a stand-in with the same method chain as the
real client (svc.events().insert(...).execute()), so no Google account is needed.
"""

import unittest
from datetime import date

import availability
import db
import gcal

CONFIG = availability.load_config()


class FreeSlotsTest(unittest.TestCase):
    def slots(self, d, planned=()):
        return [(s.strftime("%H:%M"), e.strftime("%H:%M"))
                for s, e in availability.free_slots(d, CONFIG, planned)]

    def test_weekday_after_school_plus_buffer(self):
        # Mon 5 Oct: school to 17:00 + 15 min buffer, window 17:30-22:00
        self.assertEqual(self.slots(date(2026, 10, 5)), [("17:30", "22:00")])

    def test_thursday_later(self):
        # Thu: school to 18:00 + buffer -> 18:15
        self.assertEqual(self.slots(date(2026, 10, 1)), [("18:15", "22:00")])

    def test_sunday_split_around_busy(self):
        # Sun: busy 16:00-17:00 padded to 15:45-17:15
        self.assertEqual(self.slots(date(2026, 10, 4)),
                         [("09:00", "15:45"), ("17:15", "21:30")])

    def test_planned_sessions_are_taken(self):
        from datetime import datetime
        d = date(2026, 10, 5)
        planned = [(datetime(2026, 10, 5, 18, 0), datetime(2026, 10, 5, 19, 0))]
        self.assertEqual(self.slots(d, planned), [("17:30", "18:00"), ("19:00", "22:00")])

    def test_short_gaps_dropped(self):
        from datetime import datetime
        d = date(2026, 10, 5)
        planned = [(datetime(2026, 10, 5, 17, 50), datetime(2026, 10, 5, 22, 0))]
        self.assertEqual(self.slots(d, planned), [])   # 20-min gap < 30-min minimum

    def test_one_off_commitment(self):
        cfg = dict(CONFIG, one_off=[{"label": "Party",
                                     "start": "2026-10-03 19:00", "end": "2026-10-03 23:00"}])
        slots = [(s.strftime("%H:%M"), e.strftime("%H:%M"))
                 for s, e in availability.free_slots(date(2026, 10, 3), cfg)]
        self.assertEqual(slots, [("09:00", "18:45")])


class FakeCalendar:
    """Mimics googleapiclient's chained calls and records what happened."""

    def __init__(self):
        self.events_store = {}
        self._next = 0

    def events(self):
        return self

    def insert(self, calendarId, body):
        self._next += 1
        eid = f"evt{self._next}"
        self.events_store[eid] = body
        return _Exec({"id": eid})

    def delete(self, calendarId, eventId):
        self.events_store.pop(eventId, None)
        return _Exec({})


class _Exec:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class SyncTest(unittest.TestCase):
    NOW = "2026-09-29 12:00"

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn, seed=True)
        # seed sessions already have fake event ids; clear them to start from an empty calendar
        self.conn.execute("UPDATE sessions SET calendar_event_id = NULL")
        self.cal = FakeCalendar()

    def sync(self):
        return gcal.sync(self.conn, self.cal, "cal", "Europe/London", now=self.NOW)

    def test_creates_only_future_sessions_and_is_idempotent(self):
        # future sessions: 7, 8 (today, later) and 9 (tomorrow)
        self.assertEqual(self.sync(), {"created": 3, "removed": 0})
        self.assertEqual(self.sync(), {"created": 0, "removed": 0})

    def test_event_body(self):
        self.sync()
        body = next(b for b in self.cal.events_store.values()
                    if b["extendedProperties"]["private"]["session_id"] == "9")
        self.assertEqual(body["summary"], "TMUA maths: TMUA: past paper 1")
        self.assertEqual(body["start"], {"dateTime": "2026-09-30T18:00:00",
                                         "timeZone": "Europe/London"})

    def test_cancel_removes_event(self):
        self.sync()
        db.cancel_session(self.conn, 9)
        self.assertEqual(self.sync(), {"created": 0, "removed": 1})
        self.assertEqual(len(self.cal.events_store), 2)

    def test_replan_is_cancel_plus_new(self):
        self.sync()
        db.cancel_session(self.conn, 9)
        db.plan_session(self.conn, 6, "2026-10-01 18:30", "2026-10-01 21:00")
        self.assertEqual(self.sync(), {"created": 1, "removed": 1})


if __name__ == "__main__":
    unittest.main()
