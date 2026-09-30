# study-coach

Personal study-coach agent: plans study time across tracks, checks in daily,
and re-plans from what actually got done.

## Files

| File | What it is |
|---|---|
| `schema.sql` | Current table definitions, for building a fresh database |
| `fixtures/schema_v0.sql` | Frozen old schema, only for testing migrations |
| `test_migrations.py` | Tests for migrations, proposals, locked sessions, weekly priorities |
| `seed.sql` | Fake data for testing, with "today" = 2026-09-29 |
| `queries.sql` | The five analysis queries, each under a `-- name:` header |
| `db.py` | Data layer - all other code reads/writes the database only through this |
| `test_db.py` | Tests for the data layer |
| `fixed.json` | Fixed commitments and study windows (edit this when your week changes) |
| `availability.py` | Works out free slots from `fixed.json` and planned sessions |
| `gcal.py` | Google Calendar: sign-in, the Study (agent) calendar, sync |
| `tools.py` | The planner's tools: what the LLM can see and do, and the hard rules it can't break |
| `test_tools.py` | Tests for every planner rule |
| `planner.py` | The evening re-plan: the LLM tool loop |
| `test_planner.py` | Loop tests with a scripted fake model (no API key needed) |
| `test_calendar.py` | Tests for free slots and sync (uses a fake calendar) |

## Commands

```bash
python db.py seed        # rebuild coach.db with fake data
python db.py init        # rebuild coach.db empty (wipes it!)
python db.py snapshot    # print what the planner will see, as JSON
python db.py version     # schema version (coach.db migrates itself on connect)
python db.py proposals   # pending planner proposals
python db.py approve 3   # approve proposal #3 (reject 3 to reject)
python db.py priorities "TMUA paper 2, Greek hw"   # set this week's priorities
python db.py items       # everything you can work on now, with ids
python db.py rule add "No maths on Friday evenings"   # standing instruction to the planner
python db.py rule list / rule remove 2
python db.py add-homework "CS homework 3" 60 2026-10-15
python db.py add-item "Greek" "Odyssey 16.1-25: translate + notes" 30 --strand odyssey
python -m unittest -v    # run the tests

pip install -r requirements.txt
python gcal.py auth      # one-off Google sign-in
python gcal.py setup     # one-off: create the Study (agent) calendar
python gcal.py sync      # push planned/cancelled sessions to Google Calendar
python gcal.py free      # free slots for the next 7 days

# Planner: needs DEEPSEEK_API_KEY=... in .env (or PLANNER_PROVIDER=anthropic + ANTHROPIC_API_KEY)
python planner.py --dry-run   # plan on a copy of coach.db, change nothing
python planner.py             # plan for real, then: python gcal.py sync
```

`coach.db` is gitignored because it holds personal data.
