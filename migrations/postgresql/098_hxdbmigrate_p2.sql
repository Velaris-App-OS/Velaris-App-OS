-- HxDBMigrate P2 (Semantic & Compliance Discovery): sensitive-column count on an analysis.
-- The full semantic/compliance/mapping detail lives in the analysis `report` JSONB; this
-- top-level count is for fast list views ("3 PII columns").

ALTER TABLE hxdbmigrate_analyses ADD COLUMN IF NOT EXISTS pii_count INTEGER;
