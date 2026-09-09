-- 0006: lifecycle events — append-only record of every lifecycle change (#16).
-- One row per cancel/resume/pause/adoption/timeout-verdict decision, with the
-- machine-readable reason that decided it. Rows are never updated or deleted:
-- history is explained by appended events, never rewritten.
CREATE TABLE lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    trial_id TEXT,                             -- NULL = experiment-level event
    event TEXT NOT NULL,                       -- cancelled | stopped | restart_paused | resumed | adopted | timeout_verdict
    reason TEXT,                               -- machine-readable termination/verdict reason
    detail TEXT,                               -- JSON context (counts, evidence, timestamps)
    created_at TEXT NOT NULL
);
