CREATE TABLE floorplan_analysis (
    listing_id TEXT PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    gallery_hash TEXT NOT NULL,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    latest_result_json TEXT NOT NULL CHECK (json_valid(latest_result_json)),
    analyzed_at TEXT NOT NULL
);

CREATE TABLE floorplan_decision (
    listing_id TEXT PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    gallery_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('confirmed', 'rejected')),
    selected_source_hash TEXT,
    note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 2000),
    decided_at TEXT NOT NULL,
    CHECK (decision != 'confirmed' OR selected_source_hash IS NOT NULL)
);
