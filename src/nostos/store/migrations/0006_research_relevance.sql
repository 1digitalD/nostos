ALTER TABLE research_run ADD COLUMN cache_key TEXT NOT NULL DEFAULT '';
ALTER TABLE research_run ADD COLUMN filtered_irrelevant_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE research_result ADD COLUMN match_reason TEXT NOT NULL DEFAULT '';
