CREATE TABLE listing (
    id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    fields_json TEXT NOT NULL CHECK (json_valid(fields_json))
);

CREATE INDEX idx_listing_status ON listing(status);
CREATE INDEX idx_listing_last_seen ON listing(last_seen);

CREATE TABLE observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    value_json TEXT NOT NULL CHECK (json_valid(value_json)),
    origin TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence TEXT,
    observed_at TEXT NOT NULL
);

CREATE INDEX idx_observation_listing_field
    ON observation(listing_id, field);
CREATE INDEX idx_observation_listing_field_origin
    ON observation(listing_id, field, origin);
CREATE INDEX idx_observation_observed_at
    ON observation(observed_at);

CREATE TABLE source_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    content_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX idx_source_record_listing
    ON source_record(listing_id);
CREATE INDEX idx_source_record_source_source_id
    ON source_record(source, source_id);

CREATE TABLE listing_source (
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    PRIMARY KEY (listing_id, source, source_id)
);

CREATE INDEX idx_listing_source_signature
    ON listing_source(signature);

CREATE TABLE score (
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    score REAL NOT NULL,
    breakdown_json TEXT NOT NULL CHECK (json_valid(breakdown_json)),
    computed_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, profile_id)
);

CREATE INDEX idx_score_profile
    ON score(profile_id);

CREATE TABLE user_state (
    listing_id TEXT NOT NULL REFERENCES listing(id),
    profile_id TEXT NOT NULL,
    shortlisted INTEGER NOT NULL DEFAULT 0 CHECK (shortlisted IN (0, 1)),
    excluded INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1)),
    contact_status TEXT,
    notes TEXT,
    viewing_at TEXT,
    viewing_done INTEGER NOT NULL DEFAULT 0 CHECK (viewing_done IN (0, 1)),
    PRIMARY KEY (listing_id, profile_id)
);

CREATE TABLE run (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sources_json TEXT NOT NULL CHECK (json_valid(sources_json)),
    counts_json TEXT NOT NULL CHECK (json_valid(counts_json)),
    notes TEXT
);
