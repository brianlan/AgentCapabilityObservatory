-- 0014: verifiable supervisor process identity (#16 reopen).
-- The manager records /proc/<pid>/stat field 22 (process start time, in
-- clock ticks) the moment Popen returns. Adoption, cancel, and recovery
-- then re-read that field: an equal tick proves the process at <pid> is
-- still the one we launched — a reused PID has a different start time.
-- NULL (legacy rows, or a supervisor that exited before the read) means
-- "identity unverifiable": never adopt, never signal.
ALTER TABLE trial_runs ADD COLUMN supervisor_start TEXT;
