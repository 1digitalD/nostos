CREATE TABLE listing_action (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('star', 'dismiss', 'contacted', 'note')),
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_listing_action_listing
    ON listing_action(listing_id, kind);
