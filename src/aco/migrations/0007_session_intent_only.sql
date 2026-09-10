-- 0007 (#12 reopen): submit is intent-only.
-- The official answer is the workspace snapshot sealed by the supervisor
-- (#14), never a body supplied by the untrusted session, so the answer
-- payload leaves the submissions table.
CREATE TABLE submissions_new (
    trial_id TEXT PRIMARY KEY REFERENCES trials(id),         -- one end-intent per trial
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,                            -- canonical body digest
    receipt_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'accepted'
        CHECK (status IN ('accepted', 'sealing', 'sealed', 'error')),
    created_at TEXT NOT NULL
);
INSERT INTO submissions_new (trial_id, idempotency_key, request_digest, receipt_id, status, created_at)
    SELECT trial_id, idempotency_key, request_digest, receipt_id, status, created_at FROM submissions;
DROP TABLE submissions;
ALTER TABLE submissions_new RENAME TO submissions;
