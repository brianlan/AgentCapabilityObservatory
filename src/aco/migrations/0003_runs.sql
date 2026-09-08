-- 0003: execution runs — launch intent persisted before side effects (#13).
-- One run per trial in V1. Request-side config (requested_profile) is stored
-- once at launch and never overwritten; observations stay NULL until observed.
CREATE TABLE trial_runs (
    run_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL UNIQUE REFERENCES trials(id),
    status TEXT NOT NULL CHECK (status IN ('launching', 'running', 'finished', 'error')),
    requested_profile TEXT NOT NULL,           -- target config JSON, frozen at launch
    launched_at TEXT NOT NULL,
    supervisor_pid INTEGER,                    -- set at spawn, before the supervisor does anything
    adapter_version TEXT,                      -- observed: ACO adapter version
    harbor_version TEXT,                       -- observed: pinned harbor version
    container_id TEXT,                         -- observed: agent container, discovered via label
    image TEXT,                                -- observed: agent image reference
    phases TEXT,                               -- observed: JSON list of {event, at, ...}
    exit_kind TEXT,                            -- normal | agent_error | timeout | supervisor_lost | unsupported_target | ...
    exit_detail TEXT,                          -- raw diagnostics (exception type/message), no secrets
    log_dir TEXT,                              -- harbor trial logs/artifacts reference
    finished_at TEXT,
    created_at TEXT NOT NULL
);
