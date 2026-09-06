CREATE TABLE detail_refresh_job (
    id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    requested_source_record_id INTEGER NOT NULL REFERENCES source_record(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    requested_content_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    claim_token TEXT,
    claim_expires_at TEXT,
    requested_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    extractor_revision TEXT NOT NULL,
    outcome TEXT,
    error TEXT,
    message TEXT NOT NULL,
    result_source_record_id INTEGER REFERENCES source_record(id)
);

CREATE INDEX idx_detail_refresh_job_claim
    ON detail_refresh_job(state, requested_at, id);
CREATE INDEX idx_detail_refresh_job_listing
    ON detail_refresh_job(listing_id, requested_at DESC);
CREATE UNIQUE INDEX idx_detail_refresh_job_active
    ON detail_refresh_job(listing_id)
    WHERE state IN ('queued', 'running');
