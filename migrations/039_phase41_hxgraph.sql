-- P41: HxGraph — Helix Native Knowledge Graph
-- Replaces graphify with a live, semantic, HxNexus-controlled knowledge graph.
--
-- graph_nodes: every platform concept (case types, stages, steps, forms, modules,
--              endpoints, connectors, concepts) as a typed node with embeddings.
-- graph_edges: directed relationships between nodes (structural + semantic).

BEGIN;

CREATE TABLE graph_nodes (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    node_type       VARCHAR(50) NOT NULL,   -- case_type|stage|step|form|field|module|endpoint|concept|pattern|access_group|connector
    name            VARCHAR(500) NOT NULL,  -- stable identifier (e.g. "case_type:insurance_claim")
    label           VARCHAR(500) NOT NULL,  -- human-readable display name
    source          VARCHAR(20) NOT NULL DEFAULT 'db',  -- db|code|hxstream|hxnexus
    properties      JSONB       NOT NULL DEFAULT '{}',  -- type-specific metadata
    summary         TEXT,                              -- HxNexus-generated 1-2 line description
    embedding       JSONB,                             -- float array for semantic similarity (numpy cosine)
    community_id    INTEGER,                           -- assigned by community detection
    tenant_id       VARCHAR(255),
    last_synced_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE graph_edges (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node_id    UUID        NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    to_node_id      UUID        NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    edge_type       VARCHAR(50) NOT NULL,   -- has_stage|has_step|uses_form|served_by|depends_on|triggers|similar_to|documented_by|observed_pattern
    weight          FLOAT       NOT NULL DEFAULT 1.0,  -- cosine score for similar_to; 1.0 for structural
    properties      JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for graph traversal
CREATE INDEX ix_graph_nodes_type         ON graph_nodes(node_type);
CREATE INDEX ix_graph_nodes_name         ON graph_nodes(name);
CREATE INDEX ix_graph_nodes_community    ON graph_nodes(community_id);
CREATE INDEX ix_graph_nodes_tenant       ON graph_nodes(tenant_id);
CREATE INDEX ix_graph_nodes_source       ON graph_nodes(source);
CREATE INDEX ix_graph_edges_from         ON graph_edges(from_node_id);
CREATE INDEX ix_graph_edges_to           ON graph_edges(to_node_id);
CREATE INDEX ix_graph_edges_type         ON graph_edges(edge_type);

-- Prevent duplicate structural edges
CREATE UNIQUE INDEX ix_graph_edges_unique
    ON graph_edges(from_node_id, to_node_id, edge_type);

COMMIT;
