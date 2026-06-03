-- P49 HxConnect: Identity, KYC & E-Sign

CREATE TABLE identity_verifications (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    provider        VARCHAR(50) NOT NULL DEFAULT 'onfido',
    check_id        VARCHAR(255),            -- Onfido check ID (not raw PII)
    applicant_id    VARCHAR(255),            -- Onfido applicant ID
    sdk_token       TEXT,                    -- short-lived token for hosted flow
    verification_url TEXT,                  -- link sent to customer
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | in_progress | complete | withdrawn
    result          VARCHAR(50),             -- clear | consider | unidentified
    result_hash     TEXT,                    -- SHA-256 of raw result for audit (not raw PII)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX ix_iv_case    ON identity_verifications(case_id);
CREATE INDEX ix_iv_check   ON identity_verifications(check_id);
CREATE INDEX ix_iv_status  ON identity_verifications(status);

CREATE TABLE esign_requests (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    provider        VARCHAR(50) NOT NULL DEFAULT 'docusign',
    envelope_id     VARCHAR(255),            -- DocuSign envelope ID
    signing_url     TEXT,                    -- hosted signing link for customer
    document_name   TEXT,
    signer_email    TEXT,
    signer_name     TEXT,
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | sent | delivered | completed | declined | voided
    signed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_esign_case     ON esign_requests(case_id);
CREATE INDEX ix_esign_envelope ON esign_requests(envelope_id);
CREATE INDEX ix_esign_status   ON esign_requests(status);
