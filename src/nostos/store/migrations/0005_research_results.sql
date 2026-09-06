CREATE TABLE IF NOT EXISTS research_run (
    listing_id TEXT PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    fetched_at TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    filtered_stale_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS research_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(listing_id, topic, url)
);

CREATE INDEX IF NOT EXISTS idx_research_result_listing_topic
ON research_result(listing_id, topic, published_at DESC);
