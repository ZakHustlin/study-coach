# study-coach

Personal study-coach agent: plans study time across tracks, checks in daily,
and re-plans from what actually got done.

## Files

| File | What it is |
|---|---|
| `schema.sql` | Table definitions (tracks, items, item_dependencies, sessions, completions) |
| `seed.sql` | Fake data for testing, with "today" = 2026-09-29 |
| `queries.sql` | The five analysis queries, each under a `-- name:` header |
| `db.py` | Data layer - all other code reads/writes the database only through this |
| `test_db.py` | Tests for the data layer |

## Commands

```bash
python db.py seed        # rebuild coach.db with fake data
python db.py init        # rebuild coach.db empty (wipes it!)
python db.py snapshot    # print what the planner will see, as JSON
python -m unittest -v    # run the tests
```

`coach.db` is gitignored because it holds personal data.
