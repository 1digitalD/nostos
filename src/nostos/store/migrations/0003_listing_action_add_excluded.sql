-- Migration 0003: add 'excluded' to listing_action.kind CHECK constraint.
--
-- 'excluded' is a separate action from 'dismiss':
--   - dismiss  = "I saw this listing, not for me" (kept in the archive,
--     surfaced as a marker on the card so I know I rejected it)
--   - excluded = "don't ever show me this again" (auto-hides from the
--     listing view; can be undone)
--
-- Implementation: rebuild the table with the new CHECK constraint.
-- nostos runs at small scale (single user, hundreds-thousands of rows)
-- so a rebuild is cheap. The CREATE INDEX IF NOT EXISTS guards against
-- the table already having been migrated to a state with that index.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

CREATE TABLE listing_action_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('star', 'dismiss', 'excluded', 'contacted', 'note')),
    note TEXT,
    created_at TEXT NOT NULL
);

INSERT INTO listing_action_new (id, listing_id, kind, note, created_at)
SELECT id, listing_id, kind, note, created_at FROM listing_action;

DROP TABLE listing_action;

ALTER TABLE listing_action_new RENAME TO listing_action;

CREATE INDEX IF NOT EXISTS idx_listing_action_listing
    ON listing_action(listing_id, kind);

COMMIT;

PRAGMA foreign_keys = ON;
