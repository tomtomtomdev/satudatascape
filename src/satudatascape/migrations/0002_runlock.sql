-- 0002_runlock (S8): the single-row lock that keeps crawls from overlapping.

CREATE TABLE run_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id INTEGER,
    holder TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
