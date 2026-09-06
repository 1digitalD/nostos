CREATE TABLE profile_revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_path TEXT NOT NULL,
    revision TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_profile_revision_path ON profile_revision(profile_path, id);
CREATE TABLE hunt_progress (
    listing_id TEXT PRIMARY KEY REFERENCES listing(id),
    stage TEXT NOT NULL DEFAULT 'spotted',
    viewing_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE route_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX idx_source_record_latest ON source_record(listing_id, id DESC);
