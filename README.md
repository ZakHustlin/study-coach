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
| `checkin.py` | Evening check-in flow (screens + button handling), chat-app independent |
| `bot.py` | Telegram bot: 22:00 check-in, commands, re-plan + calendar sync |
| `test_checkin.py` | Check-in tests (no Telegram needed) |
| `test_calendar.py` | Tests for free slots and sync (uses a fake calendar) |
| `reviewer.py` | Weekly review: Claude reads the knowledge base and suggests instruction changes |
| `test_kb.py`, `test_reviewer.py` | Knowledge base, track blocks and review tests |
| `deploy/` | systemd units, backup timer, server setup and update scripts |
| `DEPLOY.md` | How to put it on a VPS |
| `.env.example` | Every setting `.env` can hold |

## Knowledge base

Two loops at two speeds:

- **Nightly planner** (fast): books sessions, follows the instructions: standing
  rules, track guidance, **track blocks** (hard: "never Greek on weekend mornings",
  enforced in `tools.py`) and weekly targets.
- **Weekly review** (slow, Sunday 18:00): Claude reads the evidence (your `/note`s,
  how sessions actually went by track and time of day, targets vs minutes, its own past
  suggestions and your answers) and suggests changes to those instructions. Each one
  arrives in Telegram with Approve / Reject; nothing changes until you approve.

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
python db.py note "too tired for Greek in the mornings"   # evidence for the weekly review
python db.py notes / kb  # your notes / everything the review reads (paste into Claude)
python db.py blocks      # hard blocks; block add 2 weekend 09:00-12:00 "reason" / block remove 1
python db.py changes     # review suggestions; approve-change 3 / reject-change 3
python db.py backup FILE # safe copy of coach.db while the bot is running
python -m unittest -v    # run the tests

pip install -r requirements.txt
python gcal.py auth      # one-off Google sign-in
python gcal.py setup     # one-off: create the Study (agent) calendar
python gcal.py sync      # push planned/cancelled sessions to Google Calendar
python gcal.py free      # free slots for the next 7 days

# Planner: needs DEEPSEEK_API_KEY=... in .env (or PLANNER_PROVIDER=anthropic + ANTHROPIC_API_KEY)
python planner.py --dry-run   # plan on a copy of coach.db, change nothing
python planner.py             # plan for real, then: python gcal.py sync

# Telegram bot: needs TELEGRAM_BOT_TOKEN in .env; send /start from your phone first
python bot.py                 # keeps running; check-in at 22:00, /help for commands

# Weekly review: ANTHROPIC_API_KEY in .env (or REVIEWER_PROVIDER=deepseek)
python reviewer.py --dry-run  # review a copy, change nothing
python reviewer.py            # real run; approve suggestions with /changes
```

`coach.db` is gitignored because it holds personal data.
