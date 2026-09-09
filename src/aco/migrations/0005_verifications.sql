-- 0005: verifications — append-only scoring records (#15).
-- One row per scoring execution of a trial's Sealed Answer by one scorer
-- version. Rows are never overwritten or deleted: re-evaluation appends.
-- pass is 0/1 only when status='succeeded' (a valid verdict, including a
-- failing one); scoring errors keep pass NULL and are never capability
-- failures. Idempotent retries address the same row via
-- UNIQUE(trial_id, idempotency_key).
CREATE TABLE verifications (
    id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES trials(id),
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,              -- sha256 of canonical request payload
    scorer_version_id TEXT NOT NULL REFERENCES versions(id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'error')),
    pass INTEGER CHECK (pass IN (0, 1)),       -- NULL unless succeeded
    submetrics TEXT,                           -- JSON {name: number}, NULL unless succeeded
    error_kind TEXT CHECK (error_kind IN ('verifier_error', 'invalid_output', 'infra_error')),
    error_detail TEXT,
    raw_output_dir TEXT,                       -- diagnostic artifact reference (verifier's own output)
    scorer_digest TEXT,                        -- observed verifier bundle digest
    image TEXT,                                -- observed container image reference
    evidence TEXT,                             -- JSON: runtime container security evidence
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (trial_id, idempotency_key)
);
