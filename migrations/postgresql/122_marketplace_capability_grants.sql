-- Marketplace Layer-1: capability grants (execution & trust model).
--
-- Freedom to publish is not freedom to execute. A Layer-1 (remote) package
-- installs as an INERT connector — enabled=false, no credentials, zero egress.
-- Approval is never "now it's trusted": it activates exactly the narrow,
-- default-deny capability set the admin ticked (a subset of what the package
-- requested), every use is logged, and revocation is instant.
--
-- marketplace_capability_grants: one active grant per (tenant, package).
--   requested = what the manifest declared {"outbound_domains": [...], "scopes": [...]}
--   granted   = the admin-ticked subset (empty until approved)
--   status    = pending | granted | revoked | pending_reapproval (drift, P2)
--   descriptor_sha256 / mapping_version anchor drift detection (P2).
--
-- marketplace_network_log grows a grant_id and drops the workspace NOT NULL:
-- production Layer-1 connector calls log here too, not just sandbox traffic.

CREATE TABLE IF NOT EXISTS marketplace_capability_grants (
    id                UUID PRIMARY KEY,
    tenant_id         VARCHAR(255) NOT NULL,
    package_id        VARCHAR(255) NOT NULL,
    install_id        UUID REFERENCES marketplace_installs(id) ON DELETE SET NULL,
    workspace_id      UUID REFERENCES marketplace_workspaces(id) ON DELETE SET NULL,
    connector_id      UUID REFERENCES connector_registry(id) ON DELETE SET NULL,
    requested         JSONB NOT NULL DEFAULT '{}',
    granted           JSONB NOT NULL DEFAULT '{}',
    status            VARCHAR(32) NOT NULL DEFAULT 'pending',
    descriptor_sha256 VARCHAR(96),   -- bare hex (L1) or sha256:<hex> image digest (L2)
    descriptor_format VARCHAR(32),
    mapping_version   INTEGER NOT NULL DEFAULT 1,
    requested_by      VARCHAR(255) NOT NULL,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by        VARCHAR(255),
    granted_at        TIMESTAMPTZ,
    revoked_by        VARCHAR(255),
    revoked_at        TIMESTAMPTZ,
    note              TEXT
);

CREATE INDEX IF NOT EXISTS ix_mcg_tenant_package
    ON marketplace_capability_grants (tenant_id, package_id);
CREATE INDEX IF NOT EXISTS ix_mcg_status
    ON marketplace_capability_grants (status);

ALTER TABLE marketplace_network_log
    ALTER COLUMN workspace_id DROP NOT NULL;
ALTER TABLE marketplace_network_log
    ADD COLUMN IF NOT EXISTS grant_id UUID REFERENCES marketplace_capability_grants(id) ON DELETE SET NULL;
