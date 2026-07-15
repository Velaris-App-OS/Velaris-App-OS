-- HxEvolve P1 (Optimization Insights): every proposal the system ever generated,
-- with its gate/guardrail outcome, evidence, and (P2) the branch it was staged to.
-- The insight store is HxEvolve's ONLY write surface — production config changes
-- go through a human-approved HxBranch merge, never through HxEvolve.

CREATE TABLE IF NOT EXISTS hxevolve_insights (
    id             UUID PRIMARY KEY,
    tenant_id      VARCHAR(255),
    case_type_id   UUID         NOT NULL,
    signal         JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- the mining trigger
    proposal       JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- gated mutation payload
    proposal_kind  VARCHAR(32),         -- rule_adjust | rule_add | sla_duration | routing | reorder
    evidence       JSONB,               -- replay summary or descriptive stats
    evidence_kind  VARCHAR(16),         -- counterfactual | descriptive
    replay_run_id  UUID,
    rationale      TEXT,
    status         VARCHAR(32)  NOT NULL DEFAULT 'surfaced',
    -- surfaced | discarded_gate | discarded_guardrail | insufficient_evidence
    -- | staged | dismissed
    branch_id      UUID,                -- P2: the HxBranch PR this insight opened
    staged_rule_id UUID,                -- P2: rule kinds — the disabled rule created
    created_by     VARCHAR(255),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hxevolve_ins_ct      ON hxevolve_insights (case_type_id);
CREATE INDEX IF NOT EXISTS ix_hxevolve_ins_tenant  ON hxevolve_insights (tenant_id);
CREATE INDEX IF NOT EXISTS ix_hxevolve_ins_status  ON hxevolve_insights (status);
CREATE INDEX IF NOT EXISTS ix_hxevolve_ins_created ON hxevolve_insights (created_at);
