CREATE TABLE research_source_feedback (
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    cache_key TEXT NOT NULL,
    url TEXT NOT NULL,
    excluded_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, cache_key, url)
);
