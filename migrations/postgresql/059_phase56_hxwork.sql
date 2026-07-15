-- P56 HxWork: Kanban + Sprint Board

CREATE TABLE hxwork_boards (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    name            TEXT        NOT NULL,
    description     TEXT,
    case_type_id    UUID        REFERENCES case_types(id) ON DELETE SET NULL,
    -- columns derived from case type stages
    column_config   JSONB       NOT NULL DEFAULT '[]',
    -- [{stage_id, label, wip_limit}]
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_board_tenant    ON hxwork_boards(tenant_id);
CREATE INDEX ix_board_case_type ON hxwork_boards(case_type_id);

CREATE TABLE hxwork_sprints (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id    UUID        NOT NULL REFERENCES hxwork_boards(id) ON DELETE CASCADE,
    tenant_id   TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    goal        TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'planned',
    -- planned | active | completed
    start_date  DATE,
    end_date    DATE,
    velocity    INTEGER,    -- story points completed (calculated on completion)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX ix_sprint_board  ON hxwork_sprints(board_id);
CREATE INDEX ix_sprint_status ON hxwork_sprints(status);

CREATE TABLE hxwork_sprint_cards (
    sprint_id   UUID NOT NULL REFERENCES hxwork_sprints(id) ON DELETE CASCADE,
    case_id     UUID NOT NULL,
    story_points INTEGER DEFAULT 0,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sprint_id, case_id)
);

CREATE INDEX ix_sc_sprint ON hxwork_sprint_cards(sprint_id);
CREATE INDEX ix_sc_case   ON hxwork_sprint_cards(case_id);

CREATE TABLE hxwork_card_relations (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id    UUID        NOT NULL REFERENCES hxwork_boards(id) ON DELETE CASCADE,
    from_case_id UUID       NOT NULL,
    to_case_id  UUID        NOT NULL,
    relation_type VARCHAR(30) NOT NULL DEFAULT 'blocks',
    -- blocks | depends_on | relates_to | duplicates
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_cr_board    ON hxwork_card_relations(board_id);
CREATE INDEX ix_cr_from     ON hxwork_card_relations(from_case_id);
CREATE INDEX ix_cr_to       ON hxwork_card_relations(to_case_id);
