-- 0002: session tokens, claim timing, and idempotent submission intents (#12).
ALTER TABLE trials ADD COLUMN opened_at TEXT;                -- first claim time, never reset
ALTER TABLE trials ADD COLUMN session_token_digest TEXT;     -- sha256 of bearer token, never plaintext
ALTER TABLE trials ADD COLUMN session_token_expires_at TEXT;

CREATE TABLE submissions (
    trial_id TEXT PRIMARY KEY REFERENCES trials(id),         -- one end-intent per trial
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,                            -- canonical body digest
    receipt_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'accepted'
        CHECK (status IN ('accepted', 'sealing', 'sealed', 'error')),
    answer TEXT NOT NULL,                                    -- normalized answer payload for #14 sealing
    created_at TEXT NOT NULL
);
