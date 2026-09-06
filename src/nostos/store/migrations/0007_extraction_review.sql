CREATE TABLE extraction_review (
    preview_token TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    source_record_id INTEGER NOT NULL REFERENCES source_record(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL CHECK (json_valid(source_snapshot_json)),
    extractor_revision TEXT NOT NULL,
    correction_revision INTEGER NOT NULL,
    supporting_fingerprint TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_fingerprint TEXT NOT NULL,
    machine_listing_json TEXT NOT NULL CHECK (json_valid(machine_listing_json)),
    resolved_listing_json TEXT NOT NULL CHECK (json_valid(resolved_listing_json)),
    changes_json TEXT NOT NULL CHECK (json_valid(changes_json)),
    previous_eligible INTEGER NOT NULL CHECK (previous_eligible IN (0, 1)),
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    score_json TEXT CHECK (score_json IS NULL OR json_valid(score_json)),
    created_at TEXT NOT NULL,
    applied_at TEXT,
    superseded_at TEXT
);

CREATE INDEX idx_extraction_review_listing_created
    ON extraction_review(listing_id, created_at DESC);
CREATE UNIQUE INDEX idx_extraction_review_current
    ON extraction_review(listing_id)
    WHERE applied_at IS NOT NULL AND superseded_at IS NULL;

ALTER TABLE observation
    ADD COLUMN extraction_review_id TEXT REFERENCES extraction_review(preview_token);
CREATE INDEX idx_observation_extraction_review
    ON observation(listing_id, extraction_review_id, field);

CREATE TABLE listing_correction_revision (
    listing_id TEXT PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0
);

INSERT INTO listing_correction_revision(listing_id, revision)
SELECT listing_id, 1
FROM observation
WHERE origin = 'user'
GROUP BY listing_id;

CREATE TRIGGER observation_user_revision_insert
AFTER INSERT ON observation
WHEN NEW.origin = 'user'
BEGIN
    INSERT INTO listing_correction_revision(listing_id, revision)
    VALUES (NEW.listing_id, 1)
    ON CONFLICT(listing_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER observation_user_revision_delete
AFTER DELETE ON observation
WHEN OLD.origin = 'user'
BEGIN
    INSERT INTO listing_correction_revision(listing_id, revision)
    VALUES (OLD.listing_id, 1)
    ON CONFLICT(listing_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER observation_user_revision_update
AFTER UPDATE ON observation
WHEN OLD.origin = 'user' OR NEW.origin = 'user'
BEGIN
    INSERT INTO listing_correction_revision(listing_id, revision)
    VALUES (NEW.listing_id, 1)
    ON CONFLICT(listing_id) DO UPDATE SET revision = revision + 1;
END;
