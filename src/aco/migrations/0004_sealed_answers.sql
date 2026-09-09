-- 0004: sealed answers — the only official answer reference (#14).
-- One Sealed Answer per trial (UNIQUE trial_id). Content lives on disk under
-- <data-root>/answers/<digest>/, content-addressed and read-only; this table
-- is the registration that makes it queryable. The receipt_id is the trial's
-- single submissions receipt: accepted -> sealing -> sealed/error.
CREATE TABLE sealed_answers (
    trial_id TEXT PRIMARY KEY REFERENCES trials(id),
    run_id TEXT NOT NULL REFERENCES trial_runs(run_id),
    receipt_id TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL,                      -- sha256 of the canonical manifest
    manifest TEXT NOT NULL,                    -- canonical manifest JSON (files + changes + metadata)
    seal_trigger TEXT NOT NULL CHECK (seal_trigger IN ('submit', 'exit', 'timeout')),
    trigger_at TEXT NOT NULL,                  -- seal entry invoked
    frozen_at TEXT NOT NULL,                   -- container paused, writes suspended
    copied_at TEXT NOT NULL,                   -- snapshot copied out of the container
    published_at TEXT NOT NULL,                -- renamed into the content-addressed location
    registered_at TEXT NOT NULL,               -- this row committed
    status TEXT NOT NULL DEFAULT 'sealed' CHECK (status IN ('sealed', 'anomaly')),
    anomaly TEXT                               -- execution-condition anomaly detail, NULL when sealed
);
