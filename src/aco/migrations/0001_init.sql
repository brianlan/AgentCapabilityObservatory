-- 0001: initial schema for versioned registry, experiments, and trials.
CREATE TABLE versions (
    id TEXT PRIMARY KEY,                       -- content digest
    kind TEXT NOT NULL CHECK (kind IN ('task', 'suite', 'config', 'scorer')),
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    content TEXT NOT NULL,                     -- normalized JSON
    assets TEXT NOT NULL DEFAULT '[]',         -- normalized JSON [{name, digest}]
    created_at TEXT NOT NULL,
    UNIQUE (kind, name, version)
);

CREATE TABLE experiments (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'planned',
    requested TEXT NOT NULL,                   -- normalized request JSON
    created_at TEXT NOT NULL
);

CREATE TABLE trials (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    task_version_id TEXT NOT NULL REFERENCES versions(id),
    config_version_id TEXT NOT NULL REFERENCES versions(id),
    repetition INTEGER NOT NULL CHECK (repetition >= 1),
    plan_order INTEGER NOT NULL CHECK (plan_order >= 1),
    status TEXT NOT NULL DEFAULT 'planned',
    requested TEXT NOT NULL,                   -- plan snapshot: task + config content
    runtime_observation TEXT,                  -- NULL until actually observed
    UNIQUE (experiment_id, task_version_id, config_version_id, repetition)
);
