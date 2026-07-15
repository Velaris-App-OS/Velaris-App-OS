-- P57 HxCanvas: Visual Whiteboard

CREATE TABLE hxcanvas_boards (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    description TEXT,
    case_id     UUID        REFERENCES case_instances(id) ON DELETE SET NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_hxcb_tenant  ON hxcanvas_boards(tenant_id);
CREATE INDEX ix_hxcb_case    ON hxcanvas_boards(case_id);

CREATE TABLE hxcanvas_items (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id   UUID        NOT NULL REFERENCES hxcanvas_boards(id) ON DELETE CASCADE,
    tenant_id  TEXT        NOT NULL,
    type       VARCHAR(30) NOT NULL,
    -- sticky_note | shape | text | connector | freehand | graph_node_embed
    x          FLOAT       NOT NULL DEFAULT 0,
    y          FLOAT       NOT NULL DEFAULT 0,
    width      FLOAT       NOT NULL DEFAULT 120,
    height     FLOAT       NOT NULL DEFAULT 60,
    data       JSONB       NOT NULL DEFAULT '{}',
    z_index    INTEGER     NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_hxci_board   ON hxcanvas_items(board_id);
CREATE INDEX ix_hxci_tenant  ON hxcanvas_items(tenant_id);
CREATE INDEX ix_hxci_type    ON hxcanvas_items(type);
