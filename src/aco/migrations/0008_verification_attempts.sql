-- 0008: verification attempts — one row per actual verifier container start (#15 reopened).
-- The verifications row keeps the idempotent request identity and its current
-- state; every actual execution appends an attempt row here. Restart recovery
-- finalizes the interrupted attempt as an infra error and the re-queued
-- execution appends a new attempt: previous attempt times and diagnostics are
-- never overwritten. Contradictory successful verdicts for the same
-- (trial, scorer version) are detected at query time and excluded from
-- official capability scores (results layer), never resolved by "latest wins".
CREATE TABLE verification_attempts (
    id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL REFERENCES verifications(id),
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'error')),
    pass INTEGER CHECK (pass IN (0, 1)),       -- NULL unless succeeded
    submetrics TEXT,                           -- JSON {name: number}, NULL unless succeeded
    error_kind TEXT CHECK (error_kind IN ('verifier_error', 'invalid_output', 'infra_error')),
    error_detail TEXT,
    raw_output_dir TEXT,                       -- per-attempt diagnostic artifact reference
    scorer_digest TEXT,
    image TEXT,
    evidence TEXT,                             -- JSON: runtime container security evidence
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (verification_id, attempt_no)
);
