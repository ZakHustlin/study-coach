"""Free time: when can the planner put study blocks?

free = study window for the day
       - fixed commitments (fixed.json), padded by buffer_minutes either side
       - sessions already planned in the database (optional)
       - gaps shorter than min_slot_minutes are dropped

Pure Python with no Google libraries, so it's easy to test. v1 reads commitments
from fixed.json instead of the Fixed calendar (the 2-month scope cut).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FMT = "%Y-%m-%d %H:%M"

Interval = tuple[datetime, datetime]


def load_config(path: str | Path = ROOT / "fixed.json") -> dict:
    return json.loads(Path(path).read_text())


def _t(hhmm: str) -> time:
    return datetime.strptime(hhmm, "%H:%M").time()


def _subtract(windows: list[Interval], busy: Interval) -> list[Interval]:
    """Remove one busy interval from a list of free windows (interval difference)."""
    b_start, b_end = busy
    out: list[Interval] = []
    for start, end in windows:
        if b_end <= start or b_start >= end:      # no overlap
            out.append((start, end))
            continue
        if start < b_start:                         # free bit before the busy block
            out.append((start, b_start))
        if b_end < end:                             # free bit after it
            out.append((b_end, end))
    return out


def busy_intervals(day: date, config: dict) -> list[Interval]:
    """Fixed commitments on this day (recurring + one-offs), unpadded."""
    name = DAYS[day.weekday()]
    busy = [(datetime.combine(day, _t(f["start"])), datetime.combine(day, _t(f["end"])))
            for f in config.get("fixed", []) if name in f["days"]]
    for o in config.get("one_off", []):
        s, e = datetime.strptime(o["start"], FMT), datetime.strptime(o["end"], FMT)
        if s.date() <= day <= e.date():
            busy.append((s, e))
    return busy


def free_slots(day: date, config: dict, planned: list[Interval] = ()) -> list[Interval]:
    """Free windows on one day, as (start, end) datetimes."""
    kind = "weekend" if day.weekday() >= 5 else "weekday"
    w_start, w_end = config["study_window"][kind]
    windows = [(datetime.combine(day, _t(w_start)), datetime.combine(day, _t(w_end)))]

    pad = timedelta(minutes=config.get("buffer_minutes", 0))
    for s, e in busy_intervals(day, config):
        windows = _subtract(windows, (s - pad, e + pad))
    for s, e in planned:
        windows = _subtract(windows, (s, e))

    min_len = timedelta(minutes=config.get("min_slot_minutes", 0))
    return [(s, e) for s, e in windows if e - s >= min_len]


def free_slots_range(start: date, days: int, config: dict,
                     planned: list[dict] = ()) -> list[dict]:
    """Free slots for several days, as dicts ready for JSON / the LLM.

    planned: session rows (e.g. from db.sessions_between) to treat as taken.
    """
    taken = [(datetime.strptime(p["start_at"], FMT), datetime.strptime(p["end_at"], FMT))
             for p in planned]
    out = []
    for i in range(days):
        day = start + timedelta(days=i)
        for s, e in free_slots(day, config, [t for t in taken if t[0].date() == day]):
            out.append({"start": s.strftime(FMT), "end": e.strftime(FMT),
                        "minutes": int((e - s).total_seconds() // 60)})
    return out
