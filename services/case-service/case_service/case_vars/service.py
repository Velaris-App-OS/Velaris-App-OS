"""Case Variables Phase 1 — the single chokepoint for variable reads/writes.

Spec: docs/Future/case_variables.md (v2). The four Phase-1 security gates live
in this module and nowhere else:

  1. Identity-derived namespaces — set() takes a variable NAME; the namespace
     comes from the authenticated CallerContext via the registry. There is no
     parameter with which to spoof another integration's namespace.
  2. Redaction inside the service — every read path applies sensitivity-class
     masking (pii masked for non-privileged readers, secret masked for all
     non-owner readers). HTTP endpoints add nothing; they cannot forget.
  3. velaris.* is virtual — writes to reserved namespaces are rejected here
     with a pointer to the lifecycle endpoints (projection lands in Phase 3).
  4. No dynamic DDL — nothing in this module builds SQL from variable names;
     the EAV indexes are fixed in migration 087.

Phase 2 adds the read-façade fallback: get/get_all/get_all_bulk merge the
case.data blob underneath typed variables (bare keys, no sensitivity class,
EAV wins on collision), so consumers never check two stores themselves.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel,
    CaseInstanceVariableModel,
    CaseTypeVariableModel,
    NamespaceGrantModel,
    VariableNamespaceModel,
)

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
RESERVED_NAMESPACES = {"velaris", "form", "portal", "legacy"}
SENSITIVITY_ORDER = ["public", "internal", "pii", "secret"]

MAX_UNDECLARED_PER_NS = 100      # per (case_type × namespace) — flood cap
MAX_VALUE_BYTES = 64 * 1024      # single value cap (independent of HTTP body cap)
MAX_VARS_PER_CASE = 500


class VariableError(ValueError):
    """All policy violations raise this — callers map it to HTTP 4xx."""


@dataclass(frozen=True)
class CallerContext:
    """Authenticated caller identity. Built by trusted code paths only —
    never from request payloads.

    kind/ref identify WHO is calling (drives namespace resolution and grant
    checks); actor_id is the human/system label recorded in written_by and
    lineage; privileged marks operators allowed to see pii unredacted
    (mirrors _has_sensitive_access in the cases router).
    """
    kind: str                      # platform | connector | devconn | form | portal | rules | operator
    ref: uuid.UUID | None = None   # connector_registry.id for connector/devconn
    actor_id: str = "system"
    privileged: bool = False


# ── Namespace resolution (gate 1) ────────────────────────────────────


async def _resolve_write_namespace(
    session: AsyncSession, ctx: CallerContext,
) -> VariableNamespaceModel:
    """Map an authenticated caller to the ONE namespace it may write.

    Reserved namespaces are unreachable through this path: velaris is virtual
    (gate 3), and form/portal resolve only for their own pipeline kinds.
    """
    if ctx.kind == "platform":
        raise VariableError(
            "velaris.* variables are virtual — status/priority/subject change "
            "through the case lifecycle endpoints, not the variable store."
        )
    if ctx.kind in ("form", "portal"):
        q = select(VariableNamespaceModel).where(VariableNamespaceModel.name == ctx.kind)
    elif ctx.kind in ("connector", "devconn"):
        if ctx.ref is None:
            raise VariableError("Connector callers must carry their connector id.")
        # The identity binding is the connector UUID; both integration owner
        # types resolve (an HxBridge-registered connector can be exercised
        # through the DevConn webhook pipeline and vice versa).
        q = (
            select(VariableNamespaceModel)
            .where(VariableNamespaceModel.owner_type.in_(("connector", "devconn")))
            .where(VariableNamespaceModel.owner_ref == ctx.ref)
        )
    else:
        # rules / operator / anything else: no owned namespace — writes only
        # via an explicit grant, handled in set() after this raises
        raise VariableError(f"Caller kind {ctx.kind!r} owns no namespace.")

    ns = (await session.execute(q.limit(1))).scalar_one_or_none()
    if ns is None:
        raise VariableError("No registered namespace for this caller — register the integration first.")
    if ns.status == "retired":
        raise VariableError(f"Namespace '{ns.name}' is retired — writes are closed.")
    return ns


async def _grant_exists(
    session: AsyncSession, ctx: CallerContext, namespace_id: uuid.UUID, capability: str,
) -> bool:
    grantee_ref = str(ctx.ref) if ctx.ref else ctx.actor_id
    row = (await session.execute(
        select(NamespaceGrantModel.id)
        .where(NamespaceGrantModel.namespace_id == namespace_id)
        .where(NamespaceGrantModel.grantee_type == ctx.kind)
        .where(NamespaceGrantModel.grantee_ref.in_((grantee_ref, "*")))
        .where(NamespaceGrantModel.capability == capability)
        .limit(1)
    )).scalar_one_or_none()
    return row is not None


# ── Sensitivity + redaction (gate 2) ─────────────────────────────────


def _effective_sensitivity(ns: VariableNamespaceModel, definition: CaseTypeVariableModel | None) -> str:
    base = ns.sensitivity
    override = definition.sensitivity_override if definition else None
    if override and SENSITIVITY_ORDER.index(override) > SENSITIVITY_ORDER.index(base):
        return override  # stricter-only: looser overrides are ignored
    return base


def _redact_value(value: Any, sensitivity: str, ctx: CallerContext, is_owner: bool) -> Any:
    if sensitivity == "secret":
        # masked even for privileged display; only the owning integration
        # reads it back (it sent it — Phase 2 outbound use)
        return value if is_owner else "***"
    if sensitivity == "pii":
        return value if (ctx.privileged or is_owner) else "***"
    return value


# ── Value packing (typed EAV columns) ────────────────────────────────


def _pack(var_type: str, value: Any) -> dict[str, Any]:
    cols: dict[str, Any] = {"value_text": None, "value_num": None, "value_bool": None, "value_json": None}
    if value is None:
        return cols

    # For "any" (undeclared variables) route by the actual Python type so
    # scalars land in their typed columns — value_json is reserved for
    # genuine list/dict, never bare scalars (a bare string in a JSONB column
    # does not round-trip through the JSON decoder).
    effective = var_type
    if var_type == "any":
        if isinstance(value, bool):              effective = "bool"
        elif isinstance(value, (int, float)):    effective = "float"
        elif isinstance(value, str):             effective = "str"
        else:                                    effective = "json"

    if effective == "bool" and isinstance(value, bool):
        cols["value_bool"] = value
    elif effective in ("int", "float") and isinstance(value, (int, float)) and not isinstance(value, bool):
        cols["value_num"] = float(value)
    elif effective in ("str", "date", "datetime") and isinstance(value, str):
        cols["value_text"] = value
    elif effective in ("list", "dict", "json"):
        cols["value_json"] = value
    else:
        # advisory typing: declared/actual mismatch degrades to JSON + a
        # warning, never a crash (spec: type is advisory at write time)
        log.warning("case_vars: type mismatch for declared %s, got %s — stored as JSON",
                    var_type, type(value).__name__)
        cols["value_json"] = value
    return cols


def _unpack(row: CaseInstanceVariableModel) -> Any:
    if row.value_bool is not None:
        return row.value_bool
    if row.value_num is not None:
        num = row.value_num
        return int(num) if num == int(num) else num
    if row.value_text is not None:
        return row.value_text
    return row.value_json


def _value_size_ok(value: Any) -> bool:
    try:
        return len(json.dumps(value, default=str).encode()) <= MAX_VALUE_BYTES
    except Exception:
        return False


def _lineage_repr(value: Any, sensitivity: str) -> Any:
    """pii/secret history stores hashes, not values — lineage events feed the
    audit chain and evidence packs, which must not become PII copies."""
    if sensitivity in ("pii", "secret"):
        digest = hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()
        return {"sha256": digest}
    return value


# ── Write path ───────────────────────────────────────────────────────


async def set_variable(
    session: AsyncSession,
    ctx: CallerContext,
    case_id: uuid.UUID,
    name: str,
    value: Any,
) -> dict:
    """Write one variable. The namespace is derived from ctx — never passed.

    Exported as ``case_vars.set`` (the spec's API name); kept as
    ``set_variable`` inside this module so the builtin ``set`` stays usable.
    """
    if not NAME_RE.match(name or ""):
        raise VariableError("Variable names must match ^[a-z][a-z0-9_]{0,99}$.")
    if not _value_size_ok(value):
        raise VariableError(f"Value exceeds {MAX_VALUE_BYTES // 1024}KB limit.")

    ns = await _resolve_write_namespace(session, ctx)
    return await _set_in_namespace(session, ctx, ns, case_id, name, value)


async def set_granted(
    session: AsyncSession,
    ctx: CallerContext,
    case_id: uuid.UUID,
    full_key: str,
    value: Any,
) -> dict:
    """Grant-checked cross-namespace write (Phase 3).

    For callers that own NO namespace (rules, modules): the target carries
    the namespace explicitly, and the write is allowed only when a
    ``namespace_grants`` row exists for (ctx.kind, ctx ref/actor, write).
    Reserved namespaces are never grantable targets — ``velaris.*`` routes
    through the lifecycle machinery, form/portal/legacy through their own
    pipelines.
    """
    ns_name, dot, name = (full_key or "").partition(".")
    if not dot or not NAME_RE.match(ns_name) or not NAME_RE.match(name):
        raise VariableError("Target must be 'namespace.name' (lowercase, [a-z0-9_]).")
    if not _value_size_ok(value):
        raise VariableError(f"Value exceeds {MAX_VALUE_BYTES // 1024}KB limit.")
    if ns_name in RESERVED_NAMESPACES:
        raise VariableError(f"'{ns_name}' is reserved — not a grantable write target.")

    ns = (await session.execute(
        select(VariableNamespaceModel)
        .where(VariableNamespaceModel.name == ns_name).limit(1)
    )).scalar_one_or_none()
    if ns is None:
        raise VariableError(f"Namespace '{ns_name}' is not registered.")
    if ns.status == "retired":
        raise VariableError(f"Namespace '{ns_name}' is retired — writes are closed.")

    # Grant decision via HxGuard (first PDP consumer — same namespace_grants
    # semantics incl. the "*" wildcard, now cached + deny-audited centrally).
    from case_service import hxguard
    subject = hxguard.Subject(
        kind=ctx.kind, id=str(ctx.ref) if ctx.ref else ctx.actor_id,
    )
    decision = await hxguard.check(
        session, subject, "namespace.write", {"namespace_id": ns.id},
    )
    if not decision.allow:
        raise VariableError(
            f"No write grant for {ctx.kind} '{ctx.actor_id}' on namespace '{ns_name}'."
        )
    return await _set_in_namespace(session, ctx, ns, case_id, name, value)


async def _set_in_namespace(
    session: AsyncSession,
    ctx: CallerContext,
    ns: VariableNamespaceModel,
    case_id: uuid.UUID,
    name: str,
    value: Any,
) -> dict:
    """Shared write body — caller has already resolved + authorised ``ns``."""
    full_key = f"{ns.name}.{name}"

    # only the case_type_id is needed — avoid loading the heavy data blob
    case_type_id = (await session.execute(
        select(CaseInstanceModel.case_type_id).where(CaseInstanceModel.id == case_id)
    )).scalar_one_or_none()
    if case_type_id is None:
        raise VariableError("Case not found.")

    definition = (await session.execute(
        select(CaseTypeVariableModel)
        .where(CaseTypeVariableModel.case_type_id == case_type_id)
        .where(CaseTypeVariableModel.full_key == full_key)
    )).scalar_one_or_none()

    if definition is None:
        if ns.status == "frozen":
            raise VariableError(f"Namespace '{ns.name}' is frozen — no new variables.")
        definition = await _create_undeclared(session, case_type_id, ns, name, full_key, ctx)

    # per-case variable count cap
    count = (await session.execute(
        select(func.count()).select_from(CaseInstanceVariableModel)
        .where(CaseInstanceVariableModel.case_id == case_id)
    )).scalar_one()
    if count >= MAX_VARS_PER_CASE:
        existing = (await session.execute(
            select(CaseInstanceVariableModel.id)
            .where(CaseInstanceVariableModel.case_id == case_id)
            .where(CaseInstanceVariableModel.full_key == full_key).limit(1)
        )).scalar_one_or_none()
        if existing is None:
            raise VariableError(f"Case variable limit reached ({MAX_VARS_PER_CASE}).")

    # previous value for lineage
    prev_row = (await session.execute(
        select(CaseInstanceVariableModel)
        .where(CaseInstanceVariableModel.case_id == case_id)
        .where(CaseInstanceVariableModel.full_key == full_key)
    )).scalar_one_or_none()
    prev_value = _unpack(prev_row) if prev_row else None

    written_by = f"{ns.name}:{ctx.actor_id}"   # resolved server-side — gate 1
    cols = _pack(definition.var_type, value)
    stmt = pg_insert(CaseInstanceVariableModel).values(
        id=uuid.uuid4(), case_id=case_id, full_key=full_key,
        written_by=written_by, written_at=datetime.now(timezone.utc), **cols,
    ).on_conflict_do_update(
        index_elements=["case_id", "full_key"],
        set_={**cols, "written_by": written_by, "written_at": datetime.now(timezone.utc)},
    )
    await session.execute(stmt)

    sensitivity = _effective_sensitivity(ns, definition)
    try:
        from case_service.compliance.lineage import record_lineage_event
        await record_lineage_event(
            session, case_id=case_id, kind="variable_write", field_path=full_key,
            before_value=_lineage_repr(prev_value, sensitivity) if prev_value is not None else None,
            after_value=_lineage_repr(value, sensitivity),
            actor_id=written_by, source=ctx.kind,
        )
    except Exception as exc:  # lineage failure must not lose the write
        log.warning("case_vars: lineage event failed for %s: %s", full_key, exc)

    return {"full_key": full_key, "definition_status": definition.definition_status}


async def _create_undeclared(
    session: AsyncSession, case_type_id: uuid.UUID, ns: VariableNamespaceModel,
    name: str, full_key: str, ctx: CallerContext,
) -> CaseTypeVariableModel:
    undeclared = (await session.execute(
        select(func.count()).select_from(CaseTypeVariableModel)
        .where(CaseTypeVariableModel.case_type_id == case_type_id)
        .where(CaseTypeVariableModel.namespace_id == ns.id)
        .where(CaseTypeVariableModel.definition_status == "undeclared")
    )).scalar_one()
    if undeclared >= MAX_UNDECLARED_PER_NS:
        # ONE aggregated alert at the cap, not one per rejected key
        try:
            from case_service.enterprise.security_events import log_security_event
            await log_security_event(
                session, event_type="case_vars.undeclared_cap", severity="warning",
                user_id=ctx.actor_id, resource_type="namespace", resource_id=ns.name,
                action="undeclared_write", outcome="denied",
                details={"case_type_id": str(case_type_id), "cap": MAX_UNDECLARED_PER_NS},
            )
        except Exception:
            pass
        raise VariableError(
            f"Undeclared-variable cap reached for namespace '{ns.name}' on this "
            f"case type ({MAX_UNDECLARED_PER_NS}). Define or ignore pending variables first."
        )

    definition = CaseTypeVariableModel(
        id=uuid.uuid4(), case_type_id=case_type_id, namespace_id=ns.id,
        name=name, full_key=full_key, var_type="any",
        definition_status="undeclared",
        label=name.replace("_", " ").title(),
    )
    session.add(definition)
    await session.flush()
    log.info("case_vars: undeclared variable %s auto-registered (case_type=%s)", full_key, case_type_id)
    return definition


# ── Read paths (redaction enforced here — gate 2) ────────────────────


#: velaris.* virtual projection — column name per variable (Phase 3).
#: Projected at read time, never stored; the reserved namespace guarantees
#: no EAV row can shadow them.
VELARIS_PROJECTION = {
    "velaris.status": "status",
    "velaris.priority": "priority",
    "velaris.stage": "current_stage_id",
    "velaris.case_number": "case_number",
    "velaris.created_by": "created_by",
}


async def _read_rows(
    session: AsyncSession, ctx: CallerContext, case_ids: list[uuid.UUID],
    namespace: str | None = None, include_blob: bool = True,
) -> dict[uuid.UUID, dict[str, Any]]:
    out: dict[uuid.UUID, dict[str, Any]] = {cid: {} for cid in case_ids}
    promoted_by_ct: dict[uuid.UUID, dict[str, str]] = {}   # ct → {src: full_key}
    blob_ct_by_case: dict[uuid.UUID, uuid.UUID] = {}

    # Phase 3: velaris.* virtual projection from first-class columns —
    # included in unscoped reads and in get_namespace("velaris").
    if namespace is None or namespace == "velaris":
        proj_rows = (await session.execute(
            select(
                CaseInstanceModel.id, CaseInstanceModel.status,
                CaseInstanceModel.priority, CaseInstanceModel.current_stage_id,
                CaseInstanceModel.case_number, CaseInstanceModel.created_by,
            ).where(CaseInstanceModel.id.in_(case_ids))
        )).all()
        for cid, status, priority, stage, case_number, created_by in proj_rows:
            out[cid].update({
                "velaris.status": status,
                "velaris.priority": priority,
                "velaris.stage": stage,
                "velaris.case_number": case_number,
                "velaris.created_by": created_by,
            })
        if namespace == "velaris":
            return out

    # Phase 2: case.data blob fallback — the façade is the ONLY place that
    # knows two stores exist. Blob keys surface bare (no namespace prefix;
    # legacy.* is reserved for Phase-4 promotion) and carry no sensitivity
    # class, so they pass through unredacted exactly as case.data always has.
    # Namespaced reads skip the blob: bare keys belong to no namespace.
    #
    # SPOOF GUARD: blob keys containing a dot are NEVER surfaced. The blob is
    # writable by operators (PATCH /cases) and form/portal pipelines with
    # caller-controlled key names — a blob key named "crm.verified" must not
    # masquerade as a typed, provenance-tracked namespaced variable. The
    # namespaced shape is exclusive to the EAV store.
    if include_blob and namespace is None:
        blob_rows = (await session.execute(
            select(CaseInstanceModel.id, CaseInstanceModel.data, CaseInstanceModel.case_type_id)
            .where(CaseInstanceModel.id.in_(case_ids))
        )).all()
        # Phase 4: the typed row is the source of truth for promoted keys —
        # the blob value stops surfacing, and after the EAV merge below the
        # typed (redaction-enforced) value is ALIASED back onto the original
        # bare name so bare-key consumers (portal subject, board titles,
        # rules on old names) keep working. The blob column is never modified.
        ct_ids = {ct for _, _, ct in blob_rows}
        if ct_ids:
            for ct_id, src, fk in (await session.execute(
                select(CaseTypeVariableModel.case_type_id,
                       CaseTypeVariableModel.promoted_source,
                       CaseTypeVariableModel.full_key)
                .where(CaseTypeVariableModel.case_type_id.in_(ct_ids))
                .where(CaseTypeVariableModel.promoted_source.is_not(None))
            )).all():
                promoted_by_ct.setdefault(ct_id, {})[src] = fk
        for cid, blob, ct_id in blob_rows:
            blob_ct_by_case[cid] = ct_id
            if isinstance(blob, dict):
                hidden = promoted_by_ct.get(ct_id, {})
                out[cid].update({
                    k: v for k, v in blob.items()
                    if "." not in k and k not in hidden
                })

    q = select(CaseInstanceVariableModel).where(CaseInstanceVariableModel.case_id.in_(case_ids))
    if namespace:
        q = q.where(CaseInstanceVariableModel.full_key.like(f"{namespace}.%"))
    rows = (await session.execute(q)).scalars().all()
    if not rows:
        return out

    # one pass over definitions + namespaces for sensitivity resolution
    ns_rows = (await session.execute(select(VariableNamespaceModel))).scalars().all()
    ns_by_name = {n.name: n for n in ns_rows}
    case_types = {
        cid: ctid for cid, ctid in (await session.execute(
            select(CaseInstanceModel.id, CaseInstanceModel.case_type_id)
            .where(CaseInstanceModel.id.in_(case_ids))
        )).all()
    }
    defs = (await session.execute(
        select(CaseTypeVariableModel)
        .where(CaseTypeVariableModel.case_type_id.in_(set(case_types.values())))
    )).scalars().all()
    def_by_key = {(d.case_type_id, d.full_key): d for d in defs}

    owned_ns: set[str] = set()
    if ctx.kind in ("connector", "devconn") and ctx.ref is not None:
        owned_ns = {
            n.name for n in ns_rows
            if n.owner_type in ("connector", "devconn") and n.owner_ref == ctx.ref
        }
    elif ctx.kind in ("form", "portal"):
        owned_ns = {ctx.kind}

    # Typed variables overwrite same-named blob keys: the EAV store wins.
    for row in rows:
        ns_name = row.full_key.split(".", 1)[0]
        ns = ns_by_name.get(ns_name)
        if ns is None:
            continue
        definition = def_by_key.get((case_types.get(row.case_id), row.full_key))
        sensitivity = _effective_sensitivity(ns, definition)
        out[row.case_id][row.full_key] = _redact_value(
            _unpack(row), sensitivity, ctx, is_owner=ns_name in owned_ns,
        )

    # Phase 4 alias: promoted keys also answer to their original bare name —
    # same already-redacted typed value, so bare-key consumers keep working
    # and the bare name gains the sensitivity enforcement the blob never had.
    for cid, ct_id in blob_ct_by_case.items():
        for src, fk in promoted_by_ct.get(ct_id, {}).items():
            if fk in out[cid]:
                out[cid][src] = out[cid][fk]
    return out


async def get(session: AsyncSession, ctx: CallerContext, case_id: uuid.UUID, full_key: str) -> Any:
    result = await _read_rows(session, ctx, [case_id])
    return result[case_id].get(full_key)


async def get_all(session: AsyncSession, ctx: CallerContext, case_id: uuid.UUID) -> dict[str, Any]:
    return (await _read_rows(session, ctx, [case_id]))[case_id]


async def get_namespace(
    session: AsyncSession, ctx: CallerContext, case_id: uuid.UUID, namespace: str,
) -> dict[str, Any]:
    if not NAME_RE.match(namespace or ""):
        raise VariableError("Invalid namespace.")
    return (await _read_rows(session, ctx, [case_id], namespace=namespace))[case_id]


async def get_all_bulk(
    session: AsyncSession, ctx: CallerContext, case_ids: list[uuid.UUID],
) -> dict[uuid.UUID, dict[str, Any]]:
    """Batch read for case-list views — the N+1 killer (spec v2)."""
    if not case_ids:
        return {}
    return await _read_rows(session, ctx, case_ids[:500])


# ── Config-time namespace lifecycle (Phase 2) ────────────────────────


async def register_connector_namespace(
    session: AsyncSession, *, name: str, owner_type: str, owner_ref: uuid.UUID,
    created_by: str, sensitivity: str = "internal",
) -> VariableNamespaceModel:
    """Register the namespace an integration will write to, at config time.

    The operator names it; the identity binding (owner_type + owner_ref) is
    what set() resolves against — one namespace per connector. Idempotent
    when the same owner re-registers the same name.
    """
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise VariableError("Namespace must match ^[a-z][a-z0-9_]{0,99}$.")
    if name in RESERVED_NAMESPACES:
        raise VariableError(f"'{name}' is a reserved namespace.")
    if owner_type not in ("connector", "devconn"):
        raise VariableError("Config-time registration is for connector/devconn owners.")
    if sensitivity not in SENSITIVITY_ORDER:
        raise VariableError(f"sensitivity must be one of {SENSITIVITY_ORDER}.")

    owned = (await session.execute(
        select(VariableNamespaceModel)
        .where(VariableNamespaceModel.owner_type.in_(("connector", "devconn")))
        .where(VariableNamespaceModel.owner_ref == owner_ref)
        .limit(1)
    )).scalar_one_or_none()
    if owned is not None:
        if owned.name == name and owned.status != "retired":
            return owned  # idempotent re-registration
        raise VariableError(
            f"This integration already owns namespace '{owned.name}' "
            f"({owned.status}) — one namespace per connector."
        )

    taken = (await session.execute(
        select(VariableNamespaceModel)
        .where(VariableNamespaceModel.name == name).limit(1)
    )).scalar_one_or_none()
    if taken is not None:
        raise VariableError(f"Namespace '{name}' is already registered to another owner.")

    ns = VariableNamespaceModel(
        id=uuid.uuid4(), name=name, owner_type=owner_type, owner_ref=owner_ref,
        sensitivity=sensitivity, created_by=created_by,
    )
    session.add(ns)
    await session.flush()
    return ns


async def retire_connector_namespaces(
    session: AsyncSession, *, owner_type: str, owner_ref: uuid.UUID,
) -> int:
    """Retire (never delete) the namespaces owned by a removed integration —
    variable rows and lineage stay readable; writes are closed."""
    rows = (await session.execute(
        select(VariableNamespaceModel)
        .where(VariableNamespaceModel.owner_type == owner_type)
        .where(VariableNamespaceModel.owner_ref == owner_ref)
    )).scalars().all()
    for ns in rows:
        ns.status = "retired"
    return len(rows)


# ── case.data promotion (Phase 4 — migration wizard) ─────────────────


def _infer_var_type(values: list[Any]) -> str:
    """Majority Python type over sampled values → VAR_TYPES vocabulary
    ("bool"/"float"/"str"/"dict"/"list" — same names _pack dispatches on).
    Mixed or empty samples → "any" (routed per value at pack time)."""
    counts = {"bool": 0, "float": 0, "str": 0, "dict": 0, "list": 0}
    for v in values:
        if isinstance(v, bool):
            counts["bool"] += 1
        elif isinstance(v, (int, float)):
            counts["float"] += 1
        elif isinstance(v, str):
            counts["str"] += 1
        elif isinstance(v, dict):
            counts["dict"] += 1
        elif isinstance(v, list):
            counts["list"] += 1
    total = sum(counts.values())
    if not total:
        return "any"
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] == total else "any"   # mixed → any


async def _blob_pii_hints(session: AsyncSession, case_type_id: uuid.UUID) -> set[str]:
    """Field names HxSync classifies as pii for this case type — the only
    real per-field redaction metadata in the platform (the cases-router
    blob redaction map is a stub). Carry-over source for promotion."""
    from case_service.db.models import SyncFieldMappingModel, SyncRedactionRuleModel
    hints: set[str] = set()
    ct = str(case_type_id)
    rows = (await session.execute(
        select(SyncFieldMappingModel.source_field)
        .where(SyncFieldMappingModel.pii == True)  # noqa: E712
        .where((SyncFieldMappingModel.case_type_id == ct)
               | (SyncFieldMappingModel.case_type_id.is_(None)))
    )).scalars().all()
    hints.update(rows)
    rows = (await session.execute(
        select(SyncRedactionRuleModel.field_path)
        .where((SyncRedactionRuleModel.case_type_id == ct)
               | (SyncRedactionRuleModel.case_type_id.is_(None)))
    )).scalars().all()
    hints.update(r.split(".")[-1] for r in rows)
    return hints


async def scan_blob_keys(session: AsyncSession, case_type_id: uuid.UUID) -> list[dict]:
    """Inventory bare case.data keys for a case type (Phase 4 wizard scan).

    Dotted blob keys are ignored — the spoof guard hides them from the
    façade, so there is nothing to promote.
    """
    blobs = (await session.execute(
        select(CaseInstanceModel.data)
        .where(CaseInstanceModel.case_type_id == case_type_id)
    )).scalars().all()

    samples: dict[str, list[Any]] = {}
    counts: dict[str, int] = {}
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for k, v in blob.items():
            if "." in k:
                continue
            counts[k] = counts.get(k, 0) + 1
            if len(samples.setdefault(k, [])) < 50:
                samples[k].append(v)

    promoted = {
        d.promoted_source: d for d in (await session.execute(
            select(CaseTypeVariableModel)
            .where(CaseTypeVariableModel.case_type_id == case_type_id)
            .where(CaseTypeVariableModel.promoted_source.is_not(None))
        )).scalars().all()
    }
    pii_hints = await _blob_pii_hints(session, case_type_id)

    out = []
    for k in sorted(counts):
        d = promoted.get(k)
        out.append({
            "key": k,
            "count": counts[k],
            "inferred_type": _infer_var_type(samples.get(k, [])),
            "valid_name": bool(NAME_RE.match(k)),
            "pii_hint": k in pii_hints,
            "promoted_to": d.full_key if d else None,
        })
    return out


async def promote_blob_key(
    session: AsyncSession, *,
    case_type_id: uuid.UUID,
    key: str,
    target_namespace: str = "legacy",
    target_name: str | None = None,
    var_type: str | None = None,
    indexed: bool = False,
    actor: str = "admin",
) -> dict:
    """Promote one bare case.data key into a typed variable (Phase 4).

    The ONLY write path into the reserved ``legacy`` namespace — runtime
    callers still cannot reach it through set()/set_granted(). Copies values
    for every case of the type (written_by="migration:<actor>"), records ONE
    audit event for the whole promotion, and leaves the case.data column
    untouched: the façade suppresses the blob key via promoted_source, so
    un-promoting (deleting the definition + rows) is a clean rollback.

    pii carry-over is FORCED: when HxSync classifies the key as pii, the
    definition's sensitivity_override is at least pii — server-side, the
    wizard cannot downgrade it.
    """
    if "." in (key or "") or not key:
        raise VariableError("Only bare case.data keys can be promoted.")
    name = target_name or key
    if not NAME_RE.match(name):
        raise VariableError(
            f"'{name}' is not a valid variable name — pass target_name "
            "matching ^[a-z][a-z0-9_]{0,99}$ (original key is kept in promoted_source)."
        )

    if target_namespace == "legacy":
        ns = (await session.execute(
            select(VariableNamespaceModel)
            .where(VariableNamespaceModel.name == "legacy").limit(1)
        )).scalar_one_or_none()
        if ns is None:
            raise VariableError("Reserved 'legacy' namespace missing — run migration 087.")
    else:
        if target_namespace in RESERVED_NAMESPACES:
            raise VariableError(f"'{target_namespace}' is reserved — promote to 'legacy' or a registered namespace.")
        ns = (await session.execute(
            select(VariableNamespaceModel)
            .where(VariableNamespaceModel.name == target_namespace).limit(1)
        )).scalar_one_or_none()
        if ns is None:
            raise VariableError(f"Namespace '{target_namespace}' is not registered.")
        if ns.status == "retired":
            raise VariableError(f"Namespace '{target_namespace}' is retired.")

    full_key = f"{ns.name}.{name}"
    existing = (await session.execute(
        select(CaseTypeVariableModel)
        .where(CaseTypeVariableModel.case_type_id == case_type_id)
        .where(CaseTypeVariableModel.full_key == full_key)
    )).scalar_one_or_none()
    already_promoted = (await session.execute(
        select(CaseTypeVariableModel)
        .where(CaseTypeVariableModel.case_type_id == case_type_id)
        .where(CaseTypeVariableModel.promoted_source == key)
    )).scalar_one_or_none()
    if already_promoted is not None:
        raise VariableError(f"'{key}' is already promoted to {already_promoted.full_key}.")

    # gather values + infer type
    rows = (await session.execute(
        select(CaseInstanceModel.id, CaseInstanceModel.data)
        .where(CaseInstanceModel.case_type_id == case_type_id)
    )).all()
    values = [(cid, blob[key]) for cid, blob in rows
              if isinstance(blob, dict) and key in blob]
    chosen_type = var_type or _infer_var_type([v for _, v in values[:200]])

    pii_hints = await _blob_pii_hints(session, case_type_id)
    forced_pii = key in pii_hints

    if existing is None:
        existing = CaseTypeVariableModel(
            id=uuid.uuid4(), case_type_id=case_type_id, namespace_id=ns.id,
            name=name, full_key=full_key, var_type=chosen_type,
            definition_status="defined", indexed=indexed,
            promoted_source=key, label=key,
        )
        session.add(existing)
    else:
        existing.promoted_source = key
    # redaction carry-over: stricter-only, never downgrade
    if forced_pii and _effective_sensitivity(ns, existing) not in ("pii", "secret"):
        existing.sensitivity_override = "pii"
    await session.flush()

    now = datetime.now(timezone.utc)
    written_by = f"migration:{actor}"
    promoted = skipped = 0
    for i in range(0, len(values), 500):
        for cid, val in values[i:i + 500]:
            if not _value_size_ok(val):
                skipped += 1
                continue
            cols = _pack(existing.var_type, val)
            stmt = pg_insert(CaseInstanceVariableModel).values(
                id=uuid.uuid4(), case_id=cid, full_key=full_key,
                written_by=written_by, written_at=now, **cols,
            ).on_conflict_do_update(
                index_elements=["case_id", "full_key"],
                set_={**cols, "written_by": written_by, "written_at": now},
            )
            await session.execute(stmt)
            promoted += 1
        await session.flush()

    # ONE audit event for the whole promotion — not one per row
    try:
        from case_service.enterprise.security_events import log_security_event
        await log_security_event(
            session, event_type="variable_promotion",
            user_id=actor, action="promote",
            resource_type="case_type_variable", resource_id=full_key,
            details={
                "case_type_id": str(case_type_id), "key": key,
                "full_key": full_key, "rows": promoted,
                "skipped_oversize": skipped, "pii_forced": forced_pii,
            },
        )
    except Exception as exc:
        log.warning("promotion audit event failed for %s: %s", full_key, exc)

    return {
        "full_key": full_key, "var_type": existing.var_type,
        "sensitivity": _effective_sensitivity(ns, existing),
        "promoted": promoted, "skipped_oversize": skipped,
        "pii_forced": forced_pii,
    }
