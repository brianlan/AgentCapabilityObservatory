-- 0013: allow content-addressed skill versions in the registry (#39).
-- SQLite cannot alter a CHECK constraint in place: rebuild the table.
-- Rows (ids) are copied unchanged, so trials/verifications FK references
-- stay valid; the PRAGMAs that make the swap safe live in db.migrate,
-- outside the BEGIN/COMMIT wrapper it adds around this script.
CREATE TABLE versions_new (
    id TEXT PRIMARY KEY,                       -- content digest
    kind TEXT NOT NULL CHECK (kind IN ('task', 'suite', 'config', 'scorer', 'skill')),
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    content TEXT NOT NULL,                     -- normalized JSON
    assets TEXT NOT NULL DEFAULT '[]',         -- normalized JSON [{name, digest}]
    created_at TEXT NOT NULL,
    UNIQUE (kind, name, version)
);

INSERT INTO versions_new (id, kind, name, version, content, assets, created_at)
    SELECT id, kind, name, version, content, assets, created_at FROM versions;

DROP TABLE versions;
ALTER TABLE versions_new RENAME TO versions;
