-- Fake data for testing. Run AFTER schema.sql. Dates assume "today" = 2026-09-29.
PRAGMA foreign_keys = ON;

INSERT INTO tracks (track_id, name, priority, weekly_target_minutes, end_date, is_active) VALUES
    (1, 'Ancient Greek', 4, 180, NULL,         1),
    (2, 'TMUA maths',    5, 240, '2026-10-21', 1),
    (3, 'Coding/ML',     3, 180, NULL,         1),
    (4, 'Schoolwork',    4, 120, NULL,         1),
    (5, 'Reading',       2,  90, NULL,         0);   -- paused

INSERT INTO items (item_id, track_id, title, est_minutes, position, due_date, status) VALUES
    (1,  1, 'Greek: aorist tense',            45, 1, NULL,         'done'),
    (2,  1, 'Greek: Xenophon passage 1',      60, 2, NULL,         'in_progress'),
    (3,  1, 'Greek: Xenophon passage 2',      60, 3, NULL,         'not_started'),
    (4,  2, 'TMUA: logic & proof',            90, 1, NULL,         'done'),
    (5,  2, 'TMUA: sequences & series',       60, 2, NULL,         'done'),
    (6,  2, 'TMUA: past paper 1',            150, 3, NULL,         'not_started'),
    (7,  3, 'ML: linear regression notes',    60, 1, NULL,         'done'),
    (8,  3, 'ML: gradient descent from scratch', 90, 2, NULL,      'not_started'),
    (9,  4, 'CS homework: Big O worksheet',   40, 1, '2026-10-01', 'not_started'),
    (10, 5, 'Reading: Meno',                 120, 1, NULL,         'not_started');

-- Passage 2 needs passage 1; past paper needs BOTH TMUA topics; gradient descent needs regression
INSERT INTO item_dependencies (item_id, depends_on_id) VALUES
    (3, 2),
    (6, 4),
    (6, 5),
    (8, 7);

INSERT INTO sessions (session_id, item_id, start_at, end_at, calendar_event_id, status) VALUES
    (1, 4, '2026-09-20 10:00', '2026-09-20 11:30', 'evt_a1', 'planned'),
    (2, 1, '2026-09-21 18:00', '2026-09-21 18:45', 'evt_a2', 'planned'),
    (3, 7, '2026-09-22 18:00', '2026-09-22 19:00', 'evt_a3', 'planned'),
    (4, 5, '2026-09-24 19:00', '2026-09-24 20:00', 'evt_a4', 'planned'),
    (5, 2, '2026-09-26 10:00', '2026-09-26 11:00', 'evt_a5', 'planned'),
    (6, 8, '2026-09-27 14:00', '2026-09-27 15:30', 'evt_a6', 'planned'),
    (7, 2, '2026-09-29 18:00', '2026-09-29 19:00', 'evt_a7', 'planned'),
    (8, 9, '2026-09-29 19:15', '2026-09-29 19:55', 'evt_a8', 'planned'),
    (9, 6, '2026-09-30 18:00', '2026-09-30 20:30', 'evt_a9', 'planned');

INSERT INTO completions (item_id, session_id, logged_at, minutes_spent, outcome, confidence, note) VALUES
    (4, 1,    '2026-09-20 21:00', 110, 'done',    4, 'proof by contradiction clicked'),
    (1, 2,    '2026-09-21 21:00',  40, 'done',    2, 'still shaky on irregulars'),
    (7, 3,    '2026-09-22 21:00',  75, 'done',    3, NULL),
    (5, 4,    '2026-09-24 21:00',  70, 'done',    2, 'geometric series sums'),
    (2, 5,    '2026-09-26 21:00',  35, 'partial', 3, 'got halfway'),
    (8, 6,    '2026-09-27 21:00',   0, 'skipped', NULL, 'went out'),
    (2, 7,    '2026-09-29 21:00',  50, 'partial', 3, 'nearly done'),
    (9, 8,    '2026-09-29 21:00',   0, 'skipped', NULL, 'too tired'),
    (5, NULL, '2026-09-29 16:00',  20, 'partial', 3, 'unplanned revision on the bus');
