-- HxNexus Operator (MCP) P3 hardening: tenant-scope proposals.
-- A pending proposal's summary embeds argument values, so listing proposals
-- globally would disclose one tenant's activity to another. Capture the
-- proposer's tenant so review/confirm/reject can be filtered to that tenant.
-- (Confirmation still re-authorizes as the confirmer via HxGuard; this closes
-- the pre-confirm listing/tamper surface.)

ALTER TABLE mcp_action_proposals ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(255);
CREATE INDEX IF NOT EXISTS ix_mcp_prop_tenant ON mcp_action_proposals (tenant_id);
