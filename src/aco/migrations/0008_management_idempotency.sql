-- 0008 (#35): server-authoritative idempotent Experiment creation.
-- One row per (management principal, Idempotency-Key). The canonical request
-- digest — body only, never credential material — decides replay vs 409; the
-- experiment_id ties the key to the first-created plan. Registered in the
-- same transaction as the experiments/trials rows, so a key can never point
-- at a half-created or missing plan.
CREATE TABLE management_idempotency (
    principal TEXT NOT NULL,                                 -- sha256 of the management bearer, never the token itself
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,                            -- canonical body digest
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    created_at TEXT NOT NULL,
    UNIQUE (principal, idempotency_key)
);
