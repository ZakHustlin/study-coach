-- Named queries loaded by db.py. Each starts with a "-- name: <key>" line.
-- :today is passed in from Python as 'YYYY-MM-DD' (local date), not date('now'),
-- because SQLite's 'now' is UTC and it keeps the queries testable.

-- name: completion_rates
-- 1. Completion rate per track over the last 14 days (planned vs done).
--    A planned session counts as done if a completion for it has outcome 'done'.
SELECT t.name AS track,
       COUNT(DISTINCT s.session_id)                                         AS planned,
       COUNT(DISTINCT CASE WHEN c.outcome = 'done' THEN s.session_id END)   AS done,
       ROUND(1.0 * COUNT(DISTINCT CASE WHEN c.outcome = 'done' THEN s.session_id END)
                 / COUNT(DISTINCT s.session_id), 2)                         AS completion_rate
FROM sessions s
JOIN items i        ON i.item_id = s.item_id
JOIN tracks t       ON t.track_id = i.track_id
LEFT JOIN completions c ON c.session_id = s.session_id
WHERE s.status = 'planned'
  AND date(s.start_at) >  date(:today, '-14 days')
  AND date(s.start_at) <= date(:today)
GROUP BY t.track_id;

-- name: estimate_accuracy
-- 2. Estimate accuracy per track (actual ÷ estimated minutes), finished items only.
--    >1 means you underestimate. Includes unplanned completions.
SELECT t.name AS track,
       SUM(spent.minutes)                                       AS actual_minutes,
       SUM(i.est_minutes)                                       AS estimated_minutes,
       ROUND(1.0 * SUM(spent.minutes) / SUM(i.est_minutes), 2)  AS accuracy
FROM items i
JOIN tracks t ON t.track_id = i.track_id
JOIN (SELECT item_id, SUM(minutes_spent) AS minutes
      FROM completions GROUP BY item_id) spent ON spent.item_id = i.item_id
WHERE i.status = 'done'
GROUP BY t.track_id;

-- name: stale_low_confidence
-- 3. Items whose latest confidence is below 3 and not touched in 7 days.
WITH latest AS (
    SELECT item_id, confidence,
           ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY logged_at DESC) AS rn
    FROM completions
    WHERE confidence IS NOT NULL
),
last_touch AS (
    SELECT item_id, MAX(logged_at) AS last_at FROM completions GROUP BY item_id
)
SELECT i.item_id, i.title, l.confidence, lt.last_at
FROM latest l
JOIN items i       ON i.item_id = l.item_id
JOIN last_touch lt ON lt.item_id = l.item_id
WHERE l.rn = 1
  AND l.confidence < 3
  AND date(lt.last_at) <= date(:today, '-7 days');

-- name: next_items
-- 4. Next available item per active track: not done, all dependencies done, lowest position.
WITH available AS (
    SELECT i.*,
           ROW_NUMBER() OVER (PARTITION BY i.track_id ORDER BY i.position, i.item_id) AS rn
    FROM items i
    JOIN tracks t ON t.track_id = i.track_id
    WHERE t.is_active = 1
      AND i.status <> 'done'
      AND NOT EXISTS (
          SELECT 1
          FROM item_dependencies d
          JOIN items dep ON dep.item_id = d.depends_on_id
          WHERE d.item_id = i.item_id
            AND dep.status <> 'done')
)
SELECT t.name AS track, a.item_id, a.title, a.est_minutes, a.due_date, a.status
FROM available a
JOIN tracks t ON t.track_id = a.track_id
WHERE a.rn = 1
ORDER BY t.priority DESC;

-- name: day_summary
-- 5. Everything on a given day for the evening check-in (planned + actual, incl. unplanned).
--    Brackets matter: in SQLite || binds tighter than /.
SELECT substr(s.start_at, 12, 5) AS time,
       i.title,
       'planned ' || ((strftime('%s', s.end_at) - strftime('%s', s.start_at)) / 60) || 'm' AS plan,
       COALESCE(c.outcome || ' ' || c.minutes_spent || 'm', 'not logged')                  AS actual,
       c.confidence,
       c.note
FROM sessions s
JOIN items i ON i.item_id = s.item_id
LEFT JOIN completions c ON c.session_id = s.session_id
WHERE s.status = 'planned' AND date(s.start_at) = date(:today)
UNION ALL
SELECT substr(c.logged_at, 12, 5), i.title, 'unplanned',
       c.outcome || ' ' || c.minutes_spent || 'm', c.confidence, c.note
FROM completions c
JOIN items i ON i.item_id = c.item_id
WHERE c.session_id IS NULL AND date(c.logged_at) = date(:today)
ORDER BY time;
