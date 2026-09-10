-- 0010: widen sealed_answers.seal_trigger for the full termination vocabulary (#16 reopen).
-- ADR 0003 funnels every trial termination through finish_trial: submit, exit,
-- and timeout seal answers; explicit cancel, supervisor loss/crash, pre-agent
-- launch failures, and startup recovery leave exactly-once anomaly receipts.
-- The precise trigger always also lands in lifecycle_events.reason; this
-- constraint keeps the answer rows bounded to known termination families.
-- Rebuild (SQLite cannot alter a CHECK): copy every row verbatim.
CREATE TABLE sealed_answers_new (
    trial_id TEXT PRIMARY KEY REFERENCES trials(id),
    run_id TEXT NOT NULL REFERENCES trial_runs(run_id),
    receipt_id TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL,                      -- sha256 of the canonical manifest
    manifest TEXT NOT NULL,                    -- canonical manifest JSON (files + changes + metadata)
    seal_trigger TEXT NOT NULL CHECK (seal_trigger IN (
        'submit', 'exit', 'timeout',           -- agent-driven answers
        'cancel',                              -- explicit experiment cancel
        'supervisor_lost', 'supervisor_crash', -- system failures
        'contract_invalid', 'unsupported_target', -- pre-agent launch failures
        'recovery')),                          -- restart reconciliation
    trigger_at TEXT NOT NULL,                  -- seal entry invoked
    frozen_at TEXT NOT NULL,                   -- container paused, writes suspended
    copied_at TEXT NOT NULL,                   -- snapshot copied out of the container
    published_at TEXT NOT NULL,                -- renamed into the content-addressed location
    registered_at TEXT NOT NULL,               -- this row committed
    status TEXT NOT NULL DEFAULT 'sealed' CHECK (status IN ('sealed', 'anomaly')),
    anomaly TEXT                               -- execution-condition anomaly detail, NULL when sealed
);
INSERT INTO sealed_answers_new SELECT * FROM sealed_answers;
DROP TABLE sealed_answers;
ALTER TABLE sealed_answers_new RENAME TO sealed_answers;
