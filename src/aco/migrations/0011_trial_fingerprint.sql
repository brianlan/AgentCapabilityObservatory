-- 0011: trial fingerprint (#36) — content fingerprint over the normalized
-- TargetProfile, computed at experiment expansion. NULL for rows created
-- before this migration; the config version content is immutable, so the
-- lazy backfill on read is deterministic.
ALTER TABLE trials ADD COLUMN fingerprint TEXT;
