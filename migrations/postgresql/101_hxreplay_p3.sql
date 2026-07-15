-- HxReplay P3: opt-in estimation of indeterminate cases (policy substitution /
-- Monte-Carlo). Estimated results are labelled and never enter hard metrics.

ALTER TABLE replay_runs ADD COLUMN IF NOT EXISTS estimate BOOLEAN NOT NULL DEFAULT FALSE;
