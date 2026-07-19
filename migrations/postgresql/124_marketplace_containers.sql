-- Marketplace Layer-2: publisher code in a hardened container on Velaris infra.
--
-- One row per provisioned app container. The image is DIGEST-PINNED (the tag
-- is display-only); the container starts only when the capability grant is
-- granted, runs with the full sandbox hardening posture (non-root, read-only
-- fs, cap-drop, seccomp, egress-DROP + granted domains only), receives ZERO
-- database credentials — the scoped broker is the only data path — and stops
-- the instant the grant is revoked. Provenance (registry, digest, pulled_at)
-- is recorded here.

CREATE TABLE IF NOT EXISTS marketplace_containers (
    id            UUID PRIMARY KEY,
    tenant_id     VARCHAR(255) NOT NULL,
    package_id    VARCHAR(255) NOT NULL,
    grant_id      UUID REFERENCES marketplace_capability_grants(id) ON DELETE SET NULL,
    install_id    UUID REFERENCES marketplace_installs(id) ON DELETE SET NULL,
    image         VARCHAR(512) NOT NULL,       -- registry reference (display)
    image_digest  VARCHAR(96)  NOT NULL,       -- sha256:... (the real identity)
    registry      VARCHAR(255),
    container_id  VARCHAR(128),                -- docker id when provisioned
    status        VARCHAR(32) NOT NULL DEFAULT 'declared',  -- declared|provisioned|running|stopped|destroyed|failed
    port          INTEGER,
    signature_verified BOOLEAN NOT NULL DEFAULT false,
    pulled_at     TIMESTAMPTZ,
    started_at    TIMESTAMPTZ,
    stopped_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    error         TEXT
);

CREATE INDEX IF NOT EXISTS ix_mc_tenant_package
    ON marketplace_containers (tenant_id, package_id);
CREATE INDEX IF NOT EXISTS ix_mc_grant
    ON marketplace_containers (grant_id);
