"""HxDBManager — AI-Powered Database Operations (P67)

Phases:
  1 — Schema browser, read-only table viewer, SQL editor, query history
  2 — Table CRUD, CSV/JSON export
  3 — AI SQL assistant (HxNexus), EXPLAIN visualiser, AI optimiser
  4 — Index advisor, slow query log (pg_stat_statements)

Safety rules (non-negotiable):
  - Every query logged to db_manager_query_log
  - /execute rejects DDL — DDL goes through /ddl with confirm=true
  - Hard 30s query timeout
  - 10k row read limit (unlockable per-query with confirmation)
  - Connects as the case_service app user — no superuser ops
"""
from __future__ import annotations

import asyncio
import bcrypt
import collections
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.introspection import get_introspector
from case_service.db.models import (
    DbManagerQueryLogModel, DmlBeforeImageModel, HelixUserModel, RevokedSessionModel,
)
from case_service.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/hxdbmanager", tags=["hxdbmanager"])

_QUERY_TIMEOUT_MS = 30_000
_ROW_LIMIT        = 10_000
_HISTORY_LIMIT    = 500

_DDL_KEYWORDS = {"create", "drop", "alter", "truncate", "vacuum", "cluster", "reindex", "comment"}
_DML_KEYWORDS = {"insert", "update", "delete", "merge", "upsert"}

# ── Per-user rate limit on /execute: max 20 calls per 60s ────────────────────
_EXECUTE_RATE: dict[str, collections.deque] = {}
_EXECUTE_RATE_LOCK: dict[str, "asyncio.Lock"] = {}
_EXECUTE_RATE_LIMIT   = 20   # max calls
_EXECUTE_RATE_WINDOW  = 60   # seconds

# Service accounts that bypass rate limits and breach detection.
# HxSync runs automated queries at volume — user-facing limits must not apply.
_SERVICE_USER_IDS = {"hxsync-service", "hxdeploy-service", "system"}


def _is_service_account(user: AuthenticatedUser) -> bool:
    # Match the "service" role LITERALLY, not via has_role(): has_role() returns
    # True for any role when the user is admin, and /execute is admin-only — so
    # has_role("service") would classify every admin as a service account and
    # silently disable the per-user rate limit (DoS protection) for everyone.
    return user.user_id in _SERVICE_USER_IDS or "service" in (user.roles or [])

# ── Abuse patterns blocked before any DB round-trip ──────────────────────────
_ABUSE_PATTERNS = [
    (re.compile(r'\bpg_sleep\s*\(', re.I),                       "pg_sleep() is not permitted"),
    (re.compile(r'\bpg_terminate_backend\s*\(', re.I),           "pg_terminate_backend() is not permitted"),
    (re.compile(r'\bpg_cancel_backend\s*\(', re.I),              "pg_cancel_backend() is not permitted"),
    (re.compile(r'\bpg_read_file\s*\(', re.I),                   "pg_read_file() is not permitted"),
    (re.compile(r'\bpg_ls_dir\s*\(', re.I),                      "pg_ls_dir() is not permitted"),
    (re.compile(r'\bpg_stat_file\s*\(', re.I),                   "pg_stat_file() is not permitted"),
    (re.compile(r'\bpg_write_file\s*\(', re.I),                  "pg_write_file() is not permitted"),
    (re.compile(r'\blo_import\s*\(', re.I),                      "lo_import() is not permitted"),
    (re.compile(r'\blo_export\s*\(', re.I),                      "lo_export() is not permitted"),
    (re.compile(r'\bdblink\s*\(', re.I),                         "dblink() is not permitted (SSRF risk)"),
    (re.compile(r'\bcopy\s+\w+\s+(?:to|from)\b', re.I),          "COPY TO/FROM is not permitted"),
    (re.compile(r'\bgenerate_series\s*\([^,]+,\s*(\d{7,})', re.I), "generate_series upper bound too large (max 999,999)"),
    (re.compile(r'\bWITH\s+RECURSIVE\b(?!.*\bLIMIT\b)', re.I | re.S), "WITH RECURSIVE requires a LIMIT clause"),
    # MySQL equivalents of the above abuse vectors (DoS / file access / SSRF):
    (re.compile(r'\bsleep\s*\(', re.I),                          "sleep() is not permitted"),
    (re.compile(r'\bbenchmark\s*\(', re.I),                      "benchmark() is not permitted"),
    (re.compile(r'\bload_file\s*\(', re.I),                      "load_file() is not permitted"),
    (re.compile(r'\binto\s+(?:outfile|dumpfile)\b', re.I),       "INTO OUTFILE/DUMPFILE is not permitted"),
    (re.compile(r'\bload\s+data\b', re.I),                       "LOAD DATA is not permitted"),
]

# ── Protected tables — DML writes to these are blocked regardless of role ─────
_PROTECTED_WRITE_TABLES = {
    "db_manager_query_log",   # audit log must stay append-only
    "helix_users",            # user credentials and MFA secrets
    "connector_configs",      # encrypted API keys
    "encryption_keys",        # AES key material
    "portal_customers",       # customer PII and OTP data
    "marketplace_sources",    # source API tokens
    "schema_migrations",      # Velaris migration tracking — only the pull/startup process writes this
    "security_events",        # security audit log — immutable by design
    "security_incidents",     # same
    "scheduled_releases",     # release management — ops concern only
}

# ── Table-level access control ────────────────────────────────────────────────

# Never shown to anyone — internal Keycloak infrastructure and DB migration
# tooling. These have no business purpose for any Velaris operator.
_HIDDEN_TABLES: set[str] = {
    "realm", "realm_attribute", "realm_default_groups", "realm_enabled_event_types",
    "realm_events_listeners", "realm_localizations", "realm_required_credential",
    "realm_smtp_config", "realm_supported_locales",
    "client", "client_attributes", "client_auth_flow_bindings", "client_initial_access",
    "client_node_registrations", "client_scope", "client_scope_attributes",
    "client_scope_client", "client_scope_role_mapping",
    "credential", "fed_user_attribute", "fed_user_consent", "fed_user_consent_cl_scope",
    "fed_user_credential", "fed_user_group_membership", "fed_user_required_action",
    "fed_user_role_mapping", "federated_identity", "federated_user",
    "keycloak_group", "keycloak_role",
    "protocol_mapper", "protocol_mapper_config",
    "scope_mapping", "scope_policy",
    "resource_attribute", "resource_policy", "resource_scope", "resource_server",
    "resource_server_perm_ticket", "resource_server_policy", "resource_server_resource",
    "resource_server_scope", "resource_uris",
    "offline_client_session", "offline_user_session",
    "user_attribute", "user_consent", "user_consent_client_scope", "user_entity",
    "user_federation_config", "user_federation_mapper", "user_federation_mapper_config",
    "user_federation_provider", "user_group_membership", "user_required_action",
    "user_role_mapping", "username_login_failure",
    "group_attribute", "group_role_mapping",
    "role_attribute", "composite_role",
    "authentication_execution", "authentication_flow", "authenticator_config",
    "authenticator_config_entry",
    "identity_provider", "identity_provider_config", "identity_provider_mapper",
    "idp_mapper_config",
    "event_entity", "admin_event_entity",
    "broker_link", "web_origins", "redirect_uris",
    "associated_policy", "policy_config",
    "default_client_scope", "required_action_config", "required_action_provider",
    "org", "org_domain",
    "databasechangelog", "databasechangeloglock", "migration_model",
}

# Superadmin-only — auth credentials, security audit, encryption, release ops.
# Admins cannot read these — prevents self-serving audit trail or release manipulation.
_SUPERADMIN_ONLY_TABLES: set[str] = {
    # Auth & credentials
    "helix_users", "auth_otp", "revoked_sessions", "revoked_token",
    "sso_providers", "helix_settings", "system_config",
    # Security audit — immutable, superadmin eyes only
    "security_events", "security_incidents", "security_rules", "shield_events",
    "db_manager_query_log",   # admins must not read/clear their own query audit
    # System internals
    "dml_before_image", "trace_events", "telemetry_events",
    # Release & marketplace ops
    "scheduled_releases", "marketplace_sources",
    # Migration tracking — readable by superadmin, no DML for anyone
    "schema_migrations",
}

# Developer (designer role) — case management, integration, and dev tooling only.
# Deliberately excludes PII, credentials, audit, billing, and ops tables.
_DEV_TABLES: set[str] = {
    "case_types", "case_instances", "case_type_stages", "case_type_steps",
    "form_definitions", "case_step_completions", "case_assignments",
    "case_event_log", "case_sla_instances", "case_relationships",
    "case_audit_log", "case_type_migrations", "case_type_notification_overrides",
    "escalation_trees", "business_calendars", "rule_definitions", "work_queues",
    "integration_calls", "dead_letter_queue",
    "outbound_connector_rules", "data_models",
    "graph_nodes", "graph_edges",
    "hxnexus_conversations", "hxnexus_messages", "hxnexus_document_chunks",
    "hxwork_boards", "hxwork_stories", "hxwork_sprints", "hxwork_sprint_cards",
    "hxwork_story_relations", "hxwork_card_relations",
    "artifact_branches", "branch_reviews", "branch_audit_events", "component_commits",
    "migration_projects", "migration_scans", "migration_pipeline_runs",
    "migration_tasks", "bpm_concepts", "import_jobs", "artifact_analyses",
    "app_packages", "app_deployments",
    "process_definitions", "process_instances", "process_case_bindings",
    "process_task_log", "pipeline_stage_events",
    "helix_processes", "helix_process_instances", "helix_process_instances",
    "documents", "document_versions", "generated_docs",
    "field_population_audit", "intake_events",
}


def _is_superadmin(user: AuthenticatedUser) -> bool:
    """True only for superadmin role. AuthenticatedUser has no property for this yet."""
    return user.has_privilege("*", "*") or "superadmin" in (user.roles or [])


def _table_allowed(user: AuthenticatedUser, table_name: str) -> bool:
    """Return True if this user may read this table."""
    if table_name in _HIDDEN_TABLES:
        return False  # nobody sees Keycloak internals via DB Manager
    if table_name in _SUPERADMIN_ONLY_TABLES:
        return _is_superadmin(user)
    if user.is_admin or _is_superadmin(user):
        return True   # admin sees everything except hidden + superadmin-only
    if user.is_designer or user.has_role("developer"):
        return table_name in _DEV_TABLES
    return False


_VALID_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _require_table_access(user: AuthenticatedUser, table_name: str) -> None:
    # A name that isn't a valid SQL identifier can never be a real table — reject it
    # as 404 BEFORE any DB query. Without this, e.g. a NUL byte reaches the
    # information_schema lookup and Postgres raises an encoding error (500), not 404.
    if not _VALID_TABLE_NAME.match(table_name or "") or len(table_name) > 63:
        raise HTTPException(404, f"Table '{table_name}' not found")
    if not _table_allowed(user, table_name):
        if table_name in _HIDDEN_TABLES:
            raise HTTPException(404, f"Table '{table_name}' not found")
        if table_name in _SUPERADMIN_ONLY_TABLES:
            raise HTTPException(403, f"Table '{table_name}' requires superadmin access")
        raise HTTPException(403, f"Table '{table_name}' is not accessible for your role")


# ── Credential masking — Tier 1 always hidden, no permission overrides it ─────

_TIER1_COLUMN_NAMES = {
    "password", "password_hash", "secret", "private_key", "signing_key",
    "otp_code", "mfa_secret", "totp_secret", "token", "api_key", "api_secret",
    "client_secret", "access_token", "refresh_token", "encryption_key",
}
_TIER1_SUFFIXES = ("_hash", "_secret", "_token", "_key", "_enc", "_cipher")

_BCRYPT_RE   = re.compile(r'^\$2[aby]\$\d{2}\$')
_JWT_RE      = re.compile(r'^eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$')
_SHA256_RE   = re.compile(r'^[a-f0-9]{64}$', re.I)

_MASKED_SENTINEL = "••••••••"

# Tier 2 — masked by default, visible only to holders of db_manager.view_sensitive
# who have completed re-authentication ("DBView", see _has_active_elevation).
_TIER2_COLUMN_NAMES = {
    "account_number", "card_number", "sort_code", "bank_ref", "iban",
    "ssn", "tax_id", "passport",
}
_TIER2_SUFFIXES = ("_encrypted", "_cipher")

# PAN: 13-19 digits (with optional spaces/dashes), IBAN: 2 letters + 2 digits + up to 30
# alphanumerics, UK sort code: NN-NN-NN.
_PAN_RE        = re.compile(r'^(?:\d[ -]?){12,18}\d$')
_IBAN_RE       = re.compile(r'^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$')
_SORT_CODE_RE  = re.compile(r'^\d{2}-\d{2}-\d{2}$')


def _is_tier1_col(col: str) -> bool:
    c = col.lower()
    return c in _TIER1_COLUMN_NAMES or any(c.endswith(s) for s in _TIER1_SUFFIXES)


def _is_tier1_val(val: Any) -> bool:
    if not isinstance(val, str) or len(val) < 20:
        return False
    return bool(
        _BCRYPT_RE.match(val) or _JWT_RE.match(val) or _SHA256_RE.match(val)
    )


def _is_tier2_col(col: str) -> bool:
    c = col.lower()
    return c in _TIER2_COLUMN_NAMES or any(c.endswith(s) for s in _TIER2_SUFFIXES)


def _is_tier2_val(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    v = val.strip()
    return bool(_PAN_RE.match(v) or _IBAN_RE.match(v.upper()) or _SORT_CODE_RE.match(v))


def _mask_rows(
    rows: list[dict],
    always: bool = False,
    exclude: bool = False,
    user_has_dbview: bool = False,
) -> tuple[list[dict], bool, bool]:
    """Mask or exclude sensitive columns.

    Returns (result_rows, had_sensitive_cols, revealed_tier2):
      had_sensitive_cols — True if any Tier 1 or Tier 2 column/value was found
                           (regardless of whether it ended up masked or shown).
      revealed_tier2     — True if Tier 2 data was actually shown unmasked
                           (i.e. user_has_dbview was honoured) — used to fire
                           the DBVIEW_TIER2_REVEAL audit event precisely.

    Tier 1 (credentials, secrets, hashes) is always masked — no permission overrides it.
    Tier 2 (account numbers, IBANs, card numbers, etc.) is masked unless the caller
    passes user_has_dbview=True, meaning the user holds db_manager.view_sensitive
    AND has an active re-auth elevation (see _has_active_elevation).

    always=True  — bypasses the feature flag (used for exports).
    exclude=True — drops the column entirely instead of replacing with ••••••••.
                   Used for file exports so neither the value nor the column name
                   appears in the downloaded file. Tier 2 is always excluded too —
                   DBView only applies to in-browser viewing, never to exports.
    """
    from case_service.api.routers.releases import is_feature_enabled
    if not rows or (not always and not is_feature_enabled("hxdbmanager_security")):
        return rows, False, False
    had_sensitive = False
    revealed_tier2 = False
    out = []
    for row in rows:
        result: dict[str, Any] = {}
        for col, val in row.items():
            if _is_tier1_col(col) or _is_tier1_val(val):
                had_sensitive = True
                result[col] = "[REDACTED]" if exclude else _MASKED_SENTINEL
            elif _is_tier2_col(col) or _is_tier2_val(val):
                had_sensitive = True
                if exclude:
                    result[col] = "[REDACTED]"
                elif user_has_dbview:
                    result[col] = val
                    revealed_tier2 = True
                else:
                    prefix = col[:4] if len(col) >= 4 else col
                    result[col] = f"[MASKED — {prefix}••••]"
            else:
                result[col] = val
        out.append(result)
    return out, had_sensitive, revealed_tier2


# ── Breach detection — in-memory tracking, auto-disable on CRITICAL ───────────

_BREACH_EVENTS: dict[str, collections.deque] = {}  # user_id → deque[(ts, sev)]
_BREACH_LOCK:   dict[str, asyncio.Lock]       = {}

_BREACH_HIGH_WINDOW_S    = 600  # 10 minutes
_BREACH_HIGH_FOR_CRITICAL = 3   # 3 HIGH events in window → CRITICAL
_BREACH_REJECT_WINDOW_S  = 60   # 1 minute
_BREACH_REJECT_FOR_CRITICAL = 5 # 5 rejections in window → CRITICAL

# ── DBView re-auth elevation ("sudo mode" for Tier 2 data) ───────────────────
# Re-proving the user's own password unlocks Tier 2 visibility for 15 minutes —
# mirrors `sudo` on Linux rather than issuing a new login token.
_DBVIEW_ELEVATIONS: dict[str, float] = {}   # user_id → expiry (monotonic seconds)
_DBVIEW_ELEVATION_LOCK = asyncio.Lock()
_DBVIEW_ELEVATION_TTL_S = 15 * 60   # 15 minutes

# DML preview cache (TTL 5 minutes)
_DML_PREVIEWS: dict[str, dict] = {}
_DML_PREVIEW_TTL = 300.0

# Regex to extract table name and WHERE clause from DELETE/UPDATE for before-image.
# The identifier may be unquoted, "double-quoted" (Postgres) or `backtick-quoted` (MySQL).
_DELETE_RE = re.compile(
    r'\bDELETE\s+FROM\s+["`]?(\w+)["`]?\s*(?:WHERE\s+(.*?))?(?:RETURNING|;|$)',
    re.I | re.S,
)
_UPDATE_RE = re.compile(
    r'\bUPDATE\s+["`]?(\w+)["`]?\s+SET\s+.+?\bWHERE\s+(.*?)(?:RETURNING|;|$)',
    re.I | re.S,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _classify(sql: str) -> str:
    first = sql.strip().split()[0].lower() if sql.strip() else ""
    if first in _DDL_KEYWORDS:     return "ddl"
    if first in _DML_KEYWORDS:     return "dml"
    if first in {"select", "with", "explain", "show", "table"}: return "select"
    return "unknown"


def _check_abuse(sql: str) -> str | None:
    """Return an error message if the SQL matches a known abuse pattern, else None."""
    for pattern, msg in _ABUSE_PATTERNS:
        if pattern.search(sql):
            return msg
    return None


def _check_protected_write(sql: str) -> str | None:
    """Return an error if a DML statement targets a protected table, else None."""
    sql_lower = sql.lower()
    for table in _PROTECTED_WRITE_TABLES:
        if re.search(rf'\b{re.escape(table)}\b', sql_lower):
            return f"Direct writes to '{table}' are not permitted via DB Manager"
    return None


async def _check_execute_rate(user_id: str) -> bool:
    """Return True if user is within rate limit, False if exceeded. Thread-safe under asyncio."""
    if user_id not in _EXECUTE_RATE_LOCK:
        _EXECUTE_RATE_LOCK[user_id] = asyncio.Lock()
    async with _EXECUTE_RATE_LOCK[user_id]:
        now = time.monotonic()
        if user_id not in _EXECUTE_RATE:
            _EXECUTE_RATE[user_id] = collections.deque()
        dq = _EXECUTE_RATE[user_id]
        while dq and now - dq[0] > _EXECUTE_RATE_WINDOW:
            dq.popleft()
        if len(dq) >= _EXECUTE_RATE_LIMIT:
            return False
        dq.append(now)
        return True


def _wrap_select_with_limit(sql: str, limit: int) -> str:
    """Wrap a SELECT in a subquery so the DB enforces the row limit server-side.

    This prevents generate_series(1, 1_000_000) from materialising all rows
    before we truncate client-side.
    """
    stripped = sql.strip().rstrip(";")
    return f"SELECT * FROM ({stripped}) AS _velaris_q LIMIT {limit}"


def _hash_query(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


async def _log_query(
    session: AsyncSession,
    tenant_id: str,
    user_id: str,
    sql: str,
    status: str,
    duration_ms: int | None = None,
    rows_affected: int | None = None,
    error_detail: str | None = None,
) -> None:
    entry = DbManagerQueryLogModel(
        tenant_id     = tenant_id,
        user_id       = user_id,
        query_text    = sql,
        query_hash    = _hash_query(sql),
        status        = status,
        duration_ms   = duration_ms,
        rows_affected = rows_affected,
        error_detail  = error_detail,
    )
    session.add(entry)
    try:
        await session.commit()
    except Exception:
        await session.rollback()


def _require_admin(user: AuthenticatedUser):
    """SQL editor, DDL, DML — admin/superadmin only."""
    from case_service.api.routers.releases import is_feature_enabled
    if not is_feature_enabled("hxdbmanager"):
        raise HTTPException(404, "HxDB Manager is not available on this instance")
    if not (user.is_admin or _is_superadmin(user)):
        raise HTTPException(403, "DB Manager SQL editor requires admin role")


def _require_db_viewer(user: AuthenticatedUser):
    """Schema browser + Table viewer — admin, superadmin, designer, or developer."""
    from case_service.api.routers.releases import is_feature_enabled
    if not is_feature_enabled("hxdbmanager"):
        raise HTTPException(404, "HxDB Manager is not available on this instance")
    if not (user.is_admin or user.is_designer or user.has_role("developer") or _is_superadmin(user)):
        raise HTTPException(403, "DB Manager requires admin or developer role")


def _require_write(user: AuthenticatedUser):
    """DDL and sensitive-data expose — superadmin only."""
    if not _is_superadmin(user):
        raise HTTPException(403, "DB Manager write access requires superadmin role")


def _has_active_elevation(user_id: str) -> bool:
    expiry = _DBVIEW_ELEVATIONS.get(user_id)
    return expiry is not None and time.monotonic() < expiry


def _user_has_dbview(user: AuthenticatedUser) -> bool:
    """True only when the user holds db_manager.view_sensitive AND has an active
    re-auth elevation. Superadmins use the separate, broader `expose` flow
    (_require_write) — DBView exists to grant Tier 2 visibility to non-superadmin
    admins without handing them the keys to Tier 1 / DDL as well."""
    if not user.has_privilege("db_manager", "view_sensitive"):
        return False
    return _has_active_elevation(user.user_id)


async def _log_dbview_reveal(session: AsyncSession, user: AuthenticatedUser, where: str) -> None:
    """Audit-log a Tier 2 reveal via DBView. Sanctioned activity (privilege +
    re-auth already verified) — logged for the trail, but kept out of the
    breach-event counter that _fire_breach_event feeds (that would punish
    legitimate, granted use of the feature)."""
    from case_service.enterprise.security_events import log_security_event
    await log_security_event(
        session,
        event_type="DBMGR_DBVIEW_TIER2_REVEAL",
        severity="info",
        user_id=user.user_id,
        resource_type="hxdbmanager",
        action="dbview_reveal",
        outcome="success",
        details={"detail": f"Tier 2 sensitive data viewed via DBView — {where}"},
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()


async def _fire_breach_event(
    session: AsyncSession,
    user: AuthenticatedUser,
    severity: str,
    event_type: str,
    detail: str,
    sql: str,
) -> str:
    """Record a breach event, auto-disable account on CRITICAL threshold.

    Returns the effective severity ("CRITICAL" if threshold crossed, else original).
    No-op when hxdbmanager_security_enabled is False.
    """
    from case_service.api.routers.releases import is_feature_enabled
    if not is_feature_enabled("hxdbmanager_security"):
        return severity
    if _is_service_account(user):
        return severity

    from case_service.enterprise.security_events import log_security_event

    user_id = user.user_id
    if user_id not in _BREACH_LOCK:
        _BREACH_LOCK[user_id] = asyncio.Lock()

    effective_severity = severity
    triggered_critical = False

    async with _BREACH_LOCK[user_id]:
        now = time.monotonic()
        if user_id not in _BREACH_EVENTS:
            _BREACH_EVENTS[user_id] = collections.deque()
        dq = _BREACH_EVENTS[user_id]
        # Purge events outside the longest window
        cutoff = now - max(_BREACH_HIGH_WINDOW_S, _BREACH_REJECT_WINDOW_S)
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        dq.append((now, severity))

        recent_1m  = sum(1 for ts, _   in dq if now - ts < _BREACH_REJECT_WINDOW_S)
        recent_high = sum(1 for ts, sev in dq if sev == "HIGH" and now - ts < _BREACH_HIGH_WINDOW_S)

        if recent_1m >= _BREACH_REJECT_FOR_CRITICAL or recent_high >= _BREACH_HIGH_FOR_CRITICAL:
            effective_severity = "CRITICAL"
            triggered_critical = True

    # Always log the security event
    await log_security_event(
        session,
        event_type=f"DBMGR_{event_type}",
        severity=effective_severity.lower(),
        user_id=user_id,
        resource_type="hxdbmanager",
        action="sql_execute",
        outcome="denied",
        details={"detail": detail, "sql_preview": sql[:200]},
    )

    if triggered_critical:
        await _disable_account(session, user, event_type)

    try:
        await session.commit()
    except Exception:
        await session.rollback()

    return effective_severity


async def _disable_account(
    session: AsyncSession,
    user: AuthenticatedUser,
    reason: str,
) -> None:
    """Suspend the account: sets is_active=False in both helix_users AND user_directory.

    Both tables must be updated so that:
    - Auth middleware blocks the login (helix_users.is_active)
    - User Directory shows the user as Inactive and the Reinstate button appears
    When an admin reinstates via User Directory PATCH, _sync_auth_active() in
    user_directory.py mirrors is_active=True back into helix_users automatically.
    """
    from case_service.db.models import HelixUserModel, UserDirectoryModel

    try:
        hu = await session.get(HelixUserModel, uuid.UUID(user.user_id))
        if hu and hu.is_active:
            hu.is_active = False
            # Also mark inactive in user_directory so the UI reflects the suspension
            ud = (await session.execute(
                select(UserDirectoryModel).where(
                    UserDirectoryModel.user_id == hu.username
                )
            )).scalar_one_or_none()
            if ud:
                ud.is_active = False
            await session.flush()
    except Exception:
        log.error("Failed to disable account %s", user.user_id, exc_info=True)
        return

    # Revoke the current JWT immediately.
    # expires_at is read from the token's own `exp` claim so the revoked_sessions
    # cleanup cron removes the row at the same time the token would have expired
    # naturally — not before, not a hardcoded arbitrary window.
    # The token itself is NOT deleted; only this specific JWT is blocked.
    # Once an admin re-enables the account (is_active=True), the user can log in
    # fresh and receive a new JWT that is not in revoked_sessions.
    if user.token:
        token_hash = hashlib.sha256(user.token.encode()).hexdigest()
        from datetime import timedelta
        import base64, json as _json

        token_expires_at = datetime.now(timezone.utc) + timedelta(days=7)  # safe default
        try:
            # Decode payload without signature verification — we only need the exp claim
            payload_b64 = user.token.split(".")[1]
            # Pad to a valid base64 length
            padding = 4 - len(payload_b64) % 4
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
            if "exp" in payload:
                token_expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        except Exception:
            pass  # fallback to 7-day default if decode fails

        rev = RevokedSessionModel(
            token_hash=token_hash,
            user_id=user.user_id,
            reason=f"AUTO_DISABLE:{reason}",
            revoked_by="system:breach_detection",
            expires_at=token_expires_at,
        )
        try:
            session.add(rev)
            await session.flush()
        except Exception:
            pass  # duplicate token_hash — already revoked

    await _send_breach_email(session, user, reason)
    log.warning("Account auto-disabled for breach: user=%s reason=%s", user.user_id, reason)


async def _send_breach_email(
    session: AsyncSession,
    user: AuthenticatedUser,
    reason: str,
) -> None:
    """Send suspension email to the affected user via the tenant's default SMTP account."""
    try:
        from case_service.db.models import EmailAccountModel, HelixUserModel
        from case_service.mail import EmailService

        account = (await session.execute(
            select(EmailAccountModel).where(
                EmailAccountModel.is_default_outbound.is_(True),
                EmailAccountModel.is_active.is_(True),
            ).limit(1)
        )).scalar_one_or_none()

        if not account:
            return

        hu = await session.get(HelixUserModel, uuid.UUID(user.user_id))
        if not hu or not hu.email:
            return

        svc = EmailService()
        await svc.send(
            session,
            case_id=None,
            account=account,
            to_addresses=[hu.email],
            subject="Your Velaris account has been automatically suspended",
            body_text=(
                f"Hi {hu.display_name or hu.username},\n\n"
                "Your Velaris account has been automatically suspended due to a security "
                f"policy violation: {reason}.\n\n"
                "Your password and MFA settings have NOT been changed.\n\n"
                "To restore access, contact your administrator.\n\n"
                "— Velaris Security System"
            ),
        )
    except Exception:
        log.warning("Could not send breach suspension email to user %s", user.user_id, exc_info=True)


def _extract_table_and_where(sql: str) -> tuple[str | None, str | None]:
    """Best-effort extraction of (table_name, where_clause) from DELETE/UPDATE SQL."""
    m = _DELETE_RE.search(sql)
    if m:
        return m.group(1), (m.group(2) or "").strip() or None
    m = _UPDATE_RE.search(sql)
    if m:
        return m.group(1), (m.group(2) or "").strip() or None
    return None, None


async def _capture_before_image(
    session: AsyncSession,
    sql: str,
    kind: str,
    tenant_id: str,
    user_id: str,
) -> str:
    """Snapshot old rows before a DML statement runs. Stores in dml_before_image.

    No-op when hxdbmanager_security_enabled is False — skips entirely so the
    dml_before_image table (migration 079) does not need to exist yet.
    """
    from case_service.api.routers.releases import is_feature_enabled
    if not is_feature_enabled("hxdbmanager_security"):
        return "disabled"

    table_hint, where_clause = _extract_table_and_where(sql)
    insp = get_introspector(session)
    old_rows: list[dict] = []
    capture_method = "partial"

    # RETURNING is Postgres-only (MySQL 8 has no UPDATE/DELETE … RETURNING); on MySQL
    # the savepoint+RETURNING path is skipped and we fall through to the pre-SELECT.
    if kind == "delete" and table_hint and insp.name == "postgresql":
        # Run DELETE inside a savepoint and capture RETURNING * before rolling back
        try:
            sp = await session.begin_nested()
            del_sql = sql.rstrip(";")
            if "returning" not in del_sql.lower():
                del_sql += " RETURNING *"
            result = await session.execute(text(del_sql))
            if result.returns_rows:
                old_rows = [dict(r) for r in result.mappings().all()]
            await sp.rollback()
            capture_method = "returning"
        except Exception:
            old_rows = []
            capture_method = "partial"

    elif kind in ("update", "delete") and table_hint and where_clause:
        try:
            pre_sql = (f'SELECT * FROM {insp.quote_ident(table_hint)} '
                       f'WHERE {where_clause} LIMIT 500')
            result = await session.execute(text(pre_sql))
            old_rows = [dict(r) for r in result.mappings().all()]
            capture_method = "pre_select"
        except Exception:
            old_rows = []
            capture_method = "partial"

    entry = DmlBeforeImageModel(
        tenant_id=tenant_id,
        user_id=user_id,
        operation=kind.upper(),
        table_hint=table_hint,
        original_sql=sql[:10_000],
        old_rows=old_rows if old_rows else None,
        row_count=len(old_rows),
        capture_method=capture_method,
    )
    session.add(entry)
    await session.flush()
    return capture_method


# ── Phase 1: Schema browser ───────────────────────────────────────────────────

@router.get("/schema")
async def list_schema(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return all tables visible to this user with column counts and row estimates."""
    _require_db_viewer(current_user)
    rows = await get_introspector(session).list_tables(session)
    # Filter to only tables this user's role may see
    return {"tables": [r for r in rows if _table_allowed(current_user, r["table_name"])]}


@router.get("/schema/{table_name}")
async def get_table_schema(
    table_name: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return columns, indexes, foreign keys and stats for a table."""
    _require_db_viewer(current_user)
    _require_table_access(current_user, table_name)

    insp = get_introspector(session)
    # Validate table exists (prevent injection via table_name in subsequent queries)
    if not await insp.table_exists(session, table_name):
        raise HTTPException(404, f"Table '{table_name}' not found")

    return {
        "table":   table_name,
        "columns": await insp.columns(session, table_name),
        "indexes": await insp.indexes(session, table_name),
        "foreign_keys": await insp.foreign_keys(session, table_name),
        "stats":   await insp.table_stats(session, table_name),
    }


# ── DBView re-auth ("sudo mode" — unlocks Tier 2 data for 15 minutes) ────────

class ReauthBody(BaseModel):
    password: str


@router.post("/reauth")
async def dbview_reauth(
    body: ReauthBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Re-prove identity to unlock Tier 2 (account numbers, IBANs, etc.) for 15
    minutes — like `sudo` on Linux. Requires the db_manager.view_sensitive
    privilege; verifies the caller's own current password.
    """
    from case_service.api.routers.releases import is_feature_enabled
    if not is_feature_enabled("hxdbmanager"):
        raise HTTPException(404, "HxDB Manager is not available on this instance")
    if not current_user.has_privilege("db_manager", "view_sensitive"):
        raise HTTPException(403, "DBView requires the 'DB Manager / View masked account data' privilege")

    hu = await session.get(HelixUserModel, uuid.UUID(current_user.user_id))
    if not hu or not hu.password_hash:
        raise HTTPException(401, "Password re-authentication is not available for this account")
    try:
        ok = bcrypt.checkpw(body.password.encode(), hu.password_hash.encode())
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(401, "Incorrect password")

    expiry = time.monotonic() + _DBVIEW_ELEVATION_TTL_S
    async with _DBVIEW_ELEVATION_LOCK:
        _DBVIEW_ELEVATIONS[current_user.user_id] = expiry

    from case_service.enterprise.security_events import log_security_event
    await log_security_event(
        session,
        event_type="DBMGR_DBVIEW_REAUTH",
        severity="info",
        user_id=current_user.user_id,
        resource_type="hxdbmanager",
        action="reauth",
        outcome="success",
        details={"detail": "DBView elevation granted", "expires_in_seconds": _DBVIEW_ELEVATION_TTL_S},
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()

    return {"elevated": True, "expires_in_seconds": _DBVIEW_ELEVATION_TTL_S}


@router.get("/reauth/status")
async def dbview_reauth_status(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Return whether the caller currently holds an active DBView elevation,
    and how many seconds remain before it expires."""
    has_privilege = current_user.has_privilege("db_manager", "view_sensitive")
    expiry = _DBVIEW_ELEVATIONS.get(current_user.user_id)
    remaining = max(0, int(expiry - time.monotonic())) if expiry else 0
    return {
        "has_dbview_privilege": has_privilege,
        "elevated": has_privilege and remaining > 0,
        "expires_in_seconds": remaining,
    }


# ── Phase 1: Table viewer ─────────────────────────────────────────────────────

@router.get("/tables/{table_name}/rows")
async def get_table_rows(
    table_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    sort_col: Optional[str] = None,
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    expose: bool = Query(False),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Paginated read-only table viewer.

    expose=true: superadmin only — returns unmasked sensitive values for
    in-browser inspection. This flag is intentionally not available on the
    export endpoint; sensitive data can never be written to a downloadable file.
    """
    _require_db_viewer(current_user)
    _require_table_access(current_user, table_name)
    if expose:
        _require_write(current_user)  # superadmin only

    insp = get_introspector(session)
    # Validate table
    if not await insp.table_exists(session, table_name):
        raise HTTPException(404, f"Table '{table_name}' not found")

    # Validate sort column if provided
    order_clause = ""
    if sort_col:
        valid = await insp.column_names(session, table_name)
        if sort_col not in valid:
            raise HTTPException(400, f"Unknown column: {sort_col}")
        order_clause = f' ORDER BY {insp.quote_ident(sort_col)} {sort_dir.upper()}'

    qtable = insp.quote_ident(table_name)
    offset = (page - 1) * page_size
    sql = f'SELECT * FROM {qtable}{order_clause} LIMIT :lim OFFSET :off'

    t0 = time.monotonic()
    result = await session.execute(text(sql), {"lim": page_size, "off": offset})
    duration_ms = int((time.monotonic() - t0) * 1000)

    raw_rows = [dict(r) for r in result.mappings().all()]
    total = await session.execute(text(f'SELECT COUNT(*) FROM {qtable}'))
    _tid = str(getattr(current_user, "tenant_id", "system"))

    await _log_query(session, _tid, current_user.user_id, sql, "success", duration_ms, len(raw_rows))

    if expose:
        # Superadmin in-browser view only — log as a sensitive access event
        await _fire_breach_event(session, current_user, "MEDIUM",
                                 "SENSITIVE_EXPOSE_VIEW",
                                 f"Sensitive columns exposed for table {table_name}", sql)
        return {
            "table":               table_name,
            "page":                page,
            "page_size":           page_size,
            "total":               total.scalar_one(),
            "rows":                raw_rows,
            "sensitive_cols_masked": False,
            "expose_mode":         True,
        }

    dbview = _user_has_dbview(current_user)
    masked_rows, had_sensitive, revealed_tier2 = _mask_rows(raw_rows, user_has_dbview=dbview)
    if revealed_tier2:
        await _log_dbview_reveal(session, current_user, f"table viewer — {table_name}")
    return {
        "table":          table_name,
        "page":           page,
        "page_size":      page_size,
        "total":          total.scalar_one(),
        "rows":           masked_rows,
        "sensitive_cols_masked": had_sensitive,
        "expose_mode":    False,
        "dbview_active":  dbview,
    }


# ── Phase 1: SQL Editor ───────────────────────────────────────────────────────

class ExecuteBody(BaseModel):
    sql:      str
    row_limit: Optional[int] = 1000
    confirmed_unlimited: bool = False


@router.post("/execute")
async def execute_query(
    body: ExecuteBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run a SELECT/DML query. DDL is rejected — use /ddl endpoint."""
    _require_admin(current_user)

    # ── Per-user rate limit (asyncio-safe) ───────────────────────────────────
    # Service accounts (HxSync, HxDeploy) are exempt — they run at volume and
    # are trusted internal callers, not human admins.
    if not _is_service_account(current_user) and not await _check_execute_rate(current_user.user_id):
        raise HTTPException(429, f"Rate limit exceeded: max {_EXECUTE_RATE_LIMIT} queries per {_EXECUTE_RATE_WINDOW}s per user.")

    sql = body.sql.strip()
    if not sql:
        raise HTTPException(400, "Empty query")

    if len(sql) > 50_000:
        raise HTTPException(413, "SQL body too large (max 50KB)")

    _tid = str(getattr(current_user, "tenant_id", "system"))

    # ── Abuse pattern check ───────────────────────────────────────────────────
    abuse_msg = _check_abuse(sql)
    if abuse_msg:
        await _log_query(session, _tid, current_user.user_id, sql, "rejected", error_detail=abuse_msg)
        await _fire_breach_event(session, current_user, "HIGH", "ABUSE_PATTERN_BLOCKED", abuse_msg, sql)
        raise HTTPException(400, f"Query blocked: {abuse_msg}")

    kind = _classify(sql)

    # Allowlist: only positively-identified select/dml may proceed.
    if kind == "ddl":
        await _log_query(session, _tid, current_user.user_id, sql, "rejected")
        await _fire_breach_event(session, current_user, "MEDIUM", "DDL_VIA_EXECUTE", "DDL in /execute", sql)
        raise HTTPException(400, "DDL statements are not allowed via /execute. Use /ddl with confirm=true.")
    if kind == "unknown":
        await _log_query(session, _tid, current_user.user_id, sql, "rejected",
                         error_detail="Unrecognised statement type")
        await _fire_breach_event(session, current_user, "HIGH", "UNKNOWN_STATEMENT", "Unrecognised SQL type", sql)
        raise HTTPException(400, "Unrecognised statement type. Only SELECT and DML are permitted via /execute.")

    # DML requires write permission (superadmin) and may not target protected tables.
    if kind == "dml":
        _require_write(current_user)
        protected_msg = _check_protected_write(sql)
        if protected_msg:
            await _log_query(session, _tid, current_user.user_id, sql, "rejected", error_detail=protected_msg)
            await _fire_breach_event(session, current_user, "HIGH", "PROTECTED_TABLE_WRITE", protected_msg, sql)
            raise HTTPException(403, protected_msg)

    row_limit = min(body.row_limit or 1000, _ROW_LIMIT if not body.confirmed_unlimited else _ROW_LIMIT)

    # For SELECT queries, wrap with server-side LIMIT so the DB never
    # materialises more rows than the limit (prevents generate_series DoS).
    exec_sql = _wrap_select_with_limit(sql, row_limit) if kind == "select" else sql

    # Capture before-image for DML so admin can audit/rollback via dml_before_image
    if kind == "dml":
        try:
            await _capture_before_image(session, sql, kind, _tid, current_user.user_id)
        except Exception:
            log.warning("Before-image capture failed — proceeding with DML", exc_info=True)

    t0 = time.monotonic()
    status = "success"
    error_detail = None
    rows: list[dict] = []
    rows_affected = None

    insp = get_introspector(session)
    try:
        await insp.set_statement_timeout(session, _QUERY_TIMEOUT_MS)
        result = await session.execute(text(exec_sql))
        duration_ms = int((time.monotonic() - t0) * 1000)

        if result.returns_rows:
            rows = [dict(r) for r in result.mappings().all()]
            rows_affected = len(rows)
        else:
            rows_affected = result.rowcount
            await session.commit()

    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        status = "timeout" if "canceling statement" in str(exc).lower() else "error"
        error_detail = str(exc)
        await session.rollback()
    finally:
        # MySQL's session-scoped timeout would otherwise leak onto the next caller of
        # this pooled connection; reset is a no-op on Postgres (SET LOCAL auto-reverts).
        try:
            await insp.reset_statement_timeout(session)
        except Exception:
            pass

    await _log_query(session, _tid, current_user.user_id, sql, status,
                     duration_ms, rows_affected, error_detail)

    if status != "success":
        raise HTTPException(400, error_detail or "Query failed")

    dbview = _user_has_dbview(current_user)
    masked_rows, had_sensitive, revealed_tier2 = _mask_rows(rows, user_has_dbview=dbview)
    if revealed_tier2:
        await _log_dbview_reveal(session, current_user, "/execute query result")
    return {
        "status":              status,
        "rows":                masked_rows,
        "rows_returned":       len(masked_rows),
        "rows_affected":       rows_affected,
        "duration_ms":         duration_ms,
        "truncated":           len(rows) == row_limit,
        "sensitive_cols_masked": had_sensitive,
        "dbview_active":       dbview,
    }


# ── Phase 2: DDL endpoint ─────────────────────────────────────────────────────

class DdlBody(BaseModel):
    sql:     str
    confirm: bool = False
    reason:  Optional[str] = None


@router.post("/ddl")
async def execute_ddl(
    body: DdlBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run DDL (CREATE INDEX, ALTER TABLE, etc.) with explicit confirmation."""
    _require_write(current_user)

    if not body.confirm:
        raise HTTPException(400, "DDL requires confirm=true and a reason.")
    if not body.reason or len(body.reason.strip()) < 5:
        raise HTTPException(400, "A reason is required for DDL operations.")

    sql = body.sql.strip()
    kind = _classify(sql)
    if kind not in ("ddl",):
        raise HTTPException(400, "Only DDL statements are accepted at this endpoint.")

    t0 = time.monotonic()
    status = "success"
    error_detail = None
    try:
        await session.execute(text(sql))
        await session.commit()
    except Exception as exc:
        status = "error"
        error_detail = str(exc)
        await session.rollback()

    duration_ms = int((time.monotonic() - t0) * 1000)
    await _log_query(session, str(getattr(current_user, "tenant_id", "system")),
                     current_user.user_id, f"[DDL reason: {body.reason}]\n{sql}",
                     status, duration_ms, None, error_detail)

    if status != "success":
        raise HTTPException(400, error_detail)
    return {"ok": True, "duration_ms": duration_ms}


# ── Phase 2: Export ───────────────────────────────────────────────────────────

@router.get("/tables/{table_name}/export")
async def export_table(
    table_name: str,
    fmt: str = Query("csv", pattern="^(csv|json)$"),
    limit: int = Query(1000, ge=1, le=_ROW_LIMIT),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Export table rows as CSV or JSON. Sensitive columns are always [REDACTED]."""
    _require_db_viewer(current_user)
    _require_table_access(current_user, table_name)
    from fastapi.responses import StreamingResponse
    import io, csv, json as _json

    insp = get_introspector(session)
    if not await insp.table_exists(session, table_name):
        raise HTTPException(404, f"Table '{table_name}' not found")

    result = await session.execute(
        text(f'SELECT * FROM {insp.quote_ident(table_name)} LIMIT :lim'), {"lim": limit})
    raw_rows = [dict(r) for r in result.mappings().all()]
    # always=True + exclude=True: sensitive columns are completely removed from
    # exports — column name and value both absent from the downloaded file.
    masked_rows, _, _ = _mask_rows(raw_rows, always=True, exclude=True)

    await _log_query(session, str(getattr(current_user, "tenant_id", "system")),
                     current_user.user_id, f"EXPORT {table_name} AS {fmt}", "success", None, len(masked_rows))

    if fmt == "json":
        content = _json.dumps(masked_rows, default=str)
        return StreamingResponse(io.StringIO(content), media_type="application/json",
                                 headers={"Content-Disposition": f'attachment; filename="{table_name}.json"'})

    output = io.StringIO()
    if masked_rows:
        writer = csv.DictWriter(output, fieldnames=masked_rows[0].keys())
        writer.writeheader()
        writer.writerows(masked_rows)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{table_name}.csv"'})


# ── Phase 1: Query history ────────────────────────────────────────────────────

@router.get("/history")
async def get_query_history(
    limit: int = Query(50, ge=1, le=_HISTORY_LIMIT),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(current_user)
    rows = (await session.execute(
        select(DbManagerQueryLogModel)
        .where(DbManagerQueryLogModel.user_id == current_user.user_id)
        .order_by(DbManagerQueryLogModel.ran_at.desc())
        .limit(limit)
    )).scalars().all()

    return {"history": [
        {
            "id":           str(r.id),
            "query_text":   r.query_text[:500],
            "status":       r.status,
            "duration_ms":  r.duration_ms,
            "rows_affected": r.rows_affected,
            "ran_at":       r.ran_at.isoformat(),
        }
        for r in rows
    ]}


# ── Phase 3: EXPLAIN ─────────────────────────────────────────────────────────

class ExplainBody(BaseModel):
    sql: str


@router.post("/explain")
async def explain_query(
    body: ExplainBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run EXPLAIN (FORMAT JSON) and return the query plan. Never executes the query."""
    _require_admin(current_user)
    sql = body.sql.strip()
    # EXPLAIN without ANALYZE: pure planning, zero execution — safe for any role.
    # Accepting DML here would cause EXPLAIN ANALYZE to actually execute the write,
    # so we restrict to SELECT and let the planner show estimated costs only.
    if _classify(sql) != "select":
        raise HTTPException(400, "EXPLAIN only supports SELECT queries")
    try:
        plan = await get_introspector(session).explain_json(session, sql)
        return {"plan": plan}
    except Exception as exc:
        raise HTTPException(400, str(exc))


# ── Phase 3: AI SQL assistant ─────────────────────────────────────────────────

class AiSqlBody(BaseModel):
    question: str


@router.post("/ai/sql")
async def ai_generate_sql(
    body: AiSqlBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Natural language → SQL via HxNexus. Schema context is injected automatically."""
    _require_admin(current_user)

    from case_service.hxnexus.factory import get_llm_backend, check_ai_available
    if not await check_ai_available():
        raise HTTPException(503, "HxNexus AI is not available")

    # Build schema context
    insp = get_introspector(session)
    schema_lines = [f"  {r['table_name']}({r['cols']})" for r in await insp.schema_summary(session)]
    schema_ctx = "Database schema:\n" + "\n".join(schema_lines)

    dialect_name = "MySQL" if insp.name == "mysql" else "PostgreSQL"
    system = (
        f"You are a {dialect_name} expert. Given the database schema below and a user question, "
        f"generate a correct, safe, read-only SQL SELECT query for {dialect_name}. "
        "Return ONLY a JSON object with keys: sql (string), explanation (string), estimated_rows (string). "
        "Never generate DDL or DML — SELECT only.\n\n" + schema_ctx
    )

    llm = get_llm_backend()
    import json as _json, re as _re
    try:
        raw = await llm.complete(body.question, system=system, temperature=0.1)
    except Exception as exc:
        log.warning("HxNexus AI call failed: %s", exc)
        raise HTTPException(503, "HxNexus did not respond. Check that your AI backend (Ollama/Anthropic) is running and configured.")

    if not raw or not raw.strip():
        raise HTTPException(502, "HxNexus returned an empty response. The AI model may be overloaded or misconfigured.")

    # Strip markdown code fences properly (strip() removes characters, not substrings)
    cleaned = _re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=_re.IGNORECASE)
    cleaned = _re.sub(r'\s*```$', '', cleaned).strip()

    try:
        result = _json.loads(cleaned)
    except _json.JSONDecodeError:
        log.warning("AI SQL: could not parse JSON from response: %s", cleaned[:200])
        raise HTTPException(502, f"HxNexus returned a response that could not be parsed as JSON. Raw response (first 200 chars): {cleaned[:200]}")

    if not result.get("sql"):
        raise HTTPException(502, "HxNexus did not return a SQL query. Try rephrasing your question.")

    return {
        "sql":            result.get("sql", ""),
        "explanation":    result.get("explanation", "No explanation provided."),
        "estimated_rows": result.get("estimated_rows", "unknown"),
    }


@router.post("/ai/optimise")
async def ai_optimise_query(
    body: ExplainBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run EXPLAIN then ask HxNexus for optimisation suggestions."""
    _require_admin(current_user)

    from case_service.hxnexus.factory import get_llm_backend, check_ai_available
    if not await check_ai_available():
        raise HTTPException(503, "HxNexus AI is not available")

    sql = body.sql.strip()
    if _classify(sql) != "select":
        raise HTTPException(400, "AI optimiser only supports SELECT queries")
    insp = get_introspector(session)
    try:
        plan = await insp.explain_json(session, sql)
    except Exception as exc:
        raise HTTPException(400, f"EXPLAIN failed: {exc}")

    import json as _json, re as _re
    _dialect_name = "MySQL" if insp.name == "mysql" else "PostgreSQL"
    system = (
        f"You are a {_dialect_name} query optimisation expert. "
        "Analyse the EXPLAIN plan and return a JSON object with: "
        "summary (string), suggestions (array of {issue, recommendation, ddl_fix (optional)}). "
        "Be specific — reference actual table names, column names, and costs from the plan."
    )
    llm = get_llm_backend()
    try:
        raw = await llm.complete(_json.dumps(plan, default=str), system=system, temperature=0.2)
    except Exception as exc:
        log.warning("HxNexus AI optimise call failed: %s", exc)
        raise HTTPException(503, "HxNexus did not respond. Check that your AI backend is running.")

    if not raw or not raw.strip():
        raise HTTPException(502, "HxNexus returned an empty response.")

    cleaned = _re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=_re.IGNORECASE)
    cleaned = _re.sub(r'\s*```$', '', cleaned).strip()

    try:
        return _json.loads(cleaned)
    except _json.JSONDecodeError:
        log.warning("AI optimise: could not parse JSON: %s", cleaned[:200])
        raise HTTPException(502, f"HxNexus returned a response that could not be parsed. Raw: {cleaned[:200]}")


# ── Phase 4: Slow queries ─────────────────────────────────────────────────────

@router.get("/slow-queries")
async def get_slow_queries(
    limit: int = Query(20, ge=1, le=100),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Top N slowest queries (Postgres pg_stat_statements; unavailable on MySQL)."""
    _require_admin(current_user)
    return await get_introspector(session).slow_queries(session, limit)


@router.get("/index-advisor")
async def get_index_advice(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """AI index recommendations based on pg_stat_statements slow queries."""
    _require_admin(current_user)

    from case_service.hxnexus.factory import get_llm_backend, check_ai_available
    if not await check_ai_available():
        raise HTTPException(503, "HxNexus AI is not available")

    insp = get_introspector(session)
    stats = await insp.slow_queries(session, 20)
    if not stats.get("available"):
        raise HTTPException(503, stats.get("message")
                            or "Slow-query statistics are not available on this backend.")
    slow = stats["queries"]

    import json as _json
    _dialect_name = "MySQL" if insp.name == "mysql" else "PostgreSQL"
    system = (
        f"You are a {_dialect_name} indexing expert. Given the top 20 slowest queries, "
        "recommend indexes that would have the greatest impact. "
        "Return JSON: {recommendations: [{table, column, index_type, ddl, reason, affected_queries}]}"
    )
    import re as _re
    llm = get_llm_backend()
    try:
        raw = await llm.complete(_json.dumps(slow, default=str), system=system, temperature=0.2)
    except Exception as exc:
        log.warning("HxNexus index advisor call failed: %s", exc)
        raise HTTPException(503, "HxNexus did not respond. Check that your AI backend is running.")

    if not raw or not raw.strip():
        raise HTTPException(502, "HxNexus returned an empty response.")

    cleaned = _re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=_re.IGNORECASE)
    cleaned = _re.sub(r'\s*```$', '', cleaned).strip()

    try:
        return _json.loads(cleaned)
    except _json.JSONDecodeError:
        log.warning("Index advisor: could not parse JSON: %s", cleaned[:200])
        raise HTTPException(502, f"HxNexus returned a response that could not be parsed. Raw: {cleaned[:200]}")


# ── DML Preview + Confirm (rollback-safe DML) ─────────────────────────────────

class DmlPreviewBody(BaseModel):
    sql: str


@router.post("/dml/preview")
async def dml_preview(
    body: DmlPreviewBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run DML inside a savepoint, return what would change, then ROLLBACK.

    Returns a preview_id valid for 5 minutes. Call POST /dml/confirm/{preview_id}
    to execute the same SQL for real. Nothing is committed here.
    """
    _require_write(current_user)

    sql = body.sql.strip()
    if not sql:
        raise HTTPException(400, "Empty query")
    if len(sql) > 50_000:
        raise HTTPException(413, "SQL body too large (max 50KB)")

    abuse_msg = _check_abuse(sql)
    if abuse_msg:
        raise HTTPException(400, f"Query blocked: {abuse_msg}")

    kind = _classify(sql)
    if kind != "dml":
        raise HTTPException(400, "Only DML (INSERT/UPDATE/DELETE) is accepted at /dml/preview")

    protected_msg = _check_protected_write(sql)
    if protected_msg:
        raise HTTPException(403, protected_msg)

    # Add RETURNING * so we can show which rows will be affected. RETURNING is
    # Postgres-only; on MySQL the preview reports the affected-row COUNT (via
    # rowcount in the savepoint below) rather than the row contents.
    insp = get_introspector(session)
    exec_sql = sql.rstrip(";")
    if insp.name == "postgresql":
        has_returning = re.search(r'\bRETURNING\b', exec_sql, re.I)
        if not has_returning:
            exec_sql += " RETURNING *"

    preview_rows: list[dict] = []
    row_count = 0

    try:
        sp = await session.begin_nested()
        try:
            result = await session.execute(text(exec_sql))
            if result.returns_rows:
                preview_rows = [dict(r) for r in result.mappings().all()]
                row_count = len(preview_rows)
            else:
                row_count = result.rowcount or 0
        finally:
            await sp.rollback()  # always rollback — this is preview only
    except Exception as exc:
        raise HTTPException(400, f"Preview failed: {exc}")

    # Purge expired previews
    now = time.time()
    expired = [pid for pid, p in _DML_PREVIEWS.items() if p["expires_at"] < now]
    for pid in expired:
        _DML_PREVIEWS.pop(pid, None)

    preview_id = str(uuid.uuid4())
    _DML_PREVIEWS[preview_id] = {
        "sql":        sql,
        "kind":       kind,
        "row_count":  row_count,
        "user_id":    current_user.user_id,
        "expires_at": now + _DML_PREVIEW_TTL,
    }

    dbview = _user_has_dbview(current_user)
    masked_preview, had_sensitive, revealed_tier2 = _mask_rows(preview_rows[:50], user_has_dbview=dbview)
    if revealed_tier2:
        await _log_dbview_reveal(session, current_user, "DML preview")
    return {
        "preview_id":          preview_id,
        "row_count":           row_count,
        "preview_rows":        masked_preview,
        "sensitive_cols_masked": had_sensitive,
        "dbview_active":       dbview,
        "expires_in_seconds":  int(_DML_PREVIEW_TTL),
        "note": (
            "Nothing has been committed. Call POST /dml/confirm/{preview_id} to execute. "
            f"Preview expires in {int(_DML_PREVIEW_TTL)}s."
        ),
    }


@router.post("/dml/confirm/{preview_id}")
async def dml_confirm(
    preview_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Execute a previously previewed DML statement for real.

    Captures a before-image log entry in dml_before_image before committing
    so the change is auditable and recoverable.
    """
    _require_write(current_user)

    preview = _DML_PREVIEWS.get(preview_id)
    if not preview:
        raise HTTPException(404, "Preview not found or expired")
    if preview["user_id"] != current_user.user_id:
        raise HTTPException(403, "Preview belongs to a different user")
    if time.time() > preview["expires_at"]:
        _DML_PREVIEWS.pop(preview_id, None)
        raise HTTPException(410, "Preview has expired (5-minute limit). Re-run /dml/preview.")

    sql  = preview["sql"]
    kind = preview["kind"]
    _tid = str(getattr(current_user, "tenant_id", "system"))

    # Capture before-image (snapshots old rows before the DML runs)
    capture_method = "partial"
    try:
        capture_method = await _capture_before_image(
            session, sql, kind, _tid, current_user.user_id
        )
    except Exception:
        log.warning("Before-image capture failed before DML confirm", exc_info=True)

    # Execute the actual DML
    t0 = time.monotonic()
    status = "success"
    error_detail = None
    rows_affected = None
    result_rows: list[dict] = []

    insp = get_introspector(session)
    try:
        await insp.set_statement_timeout(session, _QUERY_TIMEOUT_MS)
        result = await session.execute(text(sql))
        duration_ms = int((time.monotonic() - t0) * 1000)

        if result.returns_rows:
            result_rows = [dict(r) for r in result.mappings().all()]
            rows_affected = len(result_rows)
        else:
            rows_affected = result.rowcount
        await session.commit()

    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        status = "error"
        error_detail = str(exc)
        await session.rollback()
    finally:
        # Reset the session-scoped MySQL timeout so it can't leak onto the next caller
        # of this pooled connection (no-op on Postgres).
        try:
            await insp.reset_statement_timeout(session)
        except Exception:
            pass

    await _log_query(session, _tid, current_user.user_id, sql, status,
                     duration_ms, rows_affected, error_detail)

    _DML_PREVIEWS.pop(preview_id, None)

    if status != "success":
        raise HTTPException(400, error_detail or "DML execution failed")

    dbview = _user_has_dbview(current_user)
    masked_rows, had_sensitive, revealed_tier2 = _mask_rows(result_rows, user_has_dbview=dbview)
    if revealed_tier2:
        await _log_dbview_reveal(session, current_user, "DML confirm result")
    return {
        "status":              "committed",
        "rows_affected":       rows_affected,
        "duration_ms":         duration_ms,
        "dbview_active":       dbview,
        "before_image_captured": capture_method != "partial",
        "capture_method":      capture_method,
        "returning_rows":      masked_rows,
        "sensitive_cols_masked": had_sensitive,
    }


@router.get("/dml/before-images")
async def get_before_images(
    limit: int = Query(50, ge=1, le=200),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return the DML before-image log for the current user (for audit / rollback reference)."""
    _require_write(current_user)

    rows = (await session.execute(
        select(DmlBeforeImageModel)
        .where(DmlBeforeImageModel.user_id == current_user.user_id)
        .order_by(DmlBeforeImageModel.captured_at.desc())
        .limit(limit)
    )).scalars().all()

    return {"before_images": [
        {
            "id":             str(r.id),
            "operation":      r.operation,
            "table_hint":     r.table_hint,
            "original_sql":   r.original_sql[:300],
            "row_count":      r.row_count,
            "capture_method": r.capture_method,
            "old_rows":       r.old_rows,
            "captured_at":    r.captured_at.isoformat(),
        }
        for r in rows
    ]}
