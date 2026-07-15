"""HxReplay P1 — decision-time input reconstruction (immutable-at-intake only).

Replay is only as good as its ability to reconstruct the inputs a decision saw
AT THE TIME it ran. P1 recovers the class that needs zero new instrumentation:
variables set once and never edited — their CURRENT value in
``case_instance_variables`` IS the decision-time value. Mutability is proven
from the lineage record (``data_lineage_events``, kind=``variable_write``):
more than one write to a field ⇒ time-varying ⇒ NOT reconstructable in P1
(P2 replays lineage to the decision timestamp).

SECURITY INVARIANT (design §5): a missing/unprovable input is flagged
``unreconstructable`` — downstream decisions become ``indeterminate`` and are
EXCLUDED from hard metrics. Nothing here ever infers or imputes a value.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import CaseInstanceVariableModel, DataLineageEventModel


def variable_value(v: CaseInstanceVariableModel) -> Any:
    """The typed value of a case variable (first non-null typed column wins)."""
    if v.value_json is not None:
        return v.value_json
    if v.value_bool is not None:
        return v.value_bool
    if v.value_num is not None:
        return v.value_num
    return v.value_text


async def _write_stats(session: AsyncSession, case_id: uuid.UUID) -> dict[str, tuple[int, Any]]:
    """Per-field (write-count, first-write-at) from lineage (kind=variable_write)."""
    rows = (await session.execute(
        select(DataLineageEventModel.field_path, func.count(),
               func.min(DataLineageEventModel.at))
        .where(DataLineageEventModel.case_id == case_id,
               DataLineageEventModel.kind == "variable_write")
        .group_by(DataLineageEventModel.field_path)
    )).all()
    return {fp: (int(n), first_at) for fp, n, first_at in rows if fp}


async def reconstruct_inputs(session: AsyncSession, case_id: uuid.UUID,
                             intake_cutoff: Any = None) -> dict[str, Any]:
    """P1 decision-time inputs for a case.

    ``intake_cutoff`` (datetime, optional): a variable whose single write happened
    AFTER this instant was added mid-case, not at intake — early decisions never
    saw it, so it is not reconstructable either.

    Returns::

        {
          "variables":         {full_key: value},   # provably immutable-at-intake
          "unreconstructable": [full_key, ...],     # edited/late/unproven → P2
        }
    """
    vars_ = (await session.execute(
        select(CaseInstanceVariableModel)
        .where(CaseInstanceVariableModel.case_id == case_id)
    )).scalars().all()
    stats = await _write_stats(session, case_id)

    variables: dict[str, Any] = {}
    unreconstructable: list[str] = []
    for v in vars_:
        # Exactly one recorded write, at intake ⇒ current value IS the decision-time
        # value. No lineage row (unprovable history), >1 write (edited), or a late
        # single write (added mid-case) ⇒ not reconstructable in P1.
        n, first_at = stats.get(v.full_key, (0, None))
        late = (intake_cutoff is not None and first_at is not None
                and _aware(first_at) > _aware(intake_cutoff))
        if n == 1 and not late:
            variables[v.full_key] = variable_value(v)
        else:
            unreconstructable.append(v.full_key)
    return {"variables": variables, "unreconstructable": sorted(unreconstructable)}


def _aware(dt):
    from datetime import timezone
    return dt.replace(tzinfo=timezone.utc) if getattr(dt, "tzinfo", None) is None else dt


# ── P2: time-varying reconstruction (lineage replay to decision time) ───────────

REDACTED = "***"          # what the unprivileged rules façade shows for pii/secret
_HASHED = object()        # sentinel: lineage stored a sha256, not the value


def _unwrap(v: Any) -> Any:
    """Undo record_lineage_event's PortableJSON wrapping ({'value': x}) and
    recognise pii/secret hash records ({'sha256': …})."""
    if isinstance(v, dict):
        if set(v.keys()) == {"value"}:
            return v["value"]
        if "sha256" in v:
            return _HASHED
    return v


async def load_write_history(session: AsyncSession, case_id: uuid.UUID
                             ) -> dict[str, list[tuple[Any, Any, Any]]]:
    """All variable_write lineage for the case: {field: [(at, before, after), …]} sorted."""
    rows = (await session.execute(
        select(DataLineageEventModel)
        .where(DataLineageEventModel.case_id == case_id,
               DataLineageEventModel.kind == "variable_write")
        .order_by(DataLineageEventModel.at, DataLineageEventModel.id)
    )).scalars().all()
    hist: dict[str, list[tuple[Any, Any, Any]]] = {}
    for r in rows:
        if r.field_path:
            hist.setdefault(r.field_path, []).append(
                (_aware(r.at), _unwrap(r.before_value), _unwrap(r.after_value)))
    return hist


def value_at(writes: list[tuple[Any, Any, Any]], at: Any) -> tuple[str, Any]:
    """The field's value at instant ``at``, from its write history.

    Returns (status, value):
      ("value", v)    — last write ≤ at gives v
      ("absent", None) — field did not exist yet (first write is later AND its
                         before_value is empty → deterministic None)
      ("unknown", None) — history before the first capture is unknown (first
                          write is later but had a non-empty before_value), or
                          the stored record is a hash → NEVER guessed
    """
    at = _aware(at)
    last = None
    for w_at, _before, after in writes:
        if w_at <= at:
            last = after
        else:
            break
    if last is not None or any(w_at <= at for w_at, _b, _a in writes):
        return ("unknown", None) if last is _HASHED else ("value", last)
    first_before = writes[0][1] if writes else None
    if first_before in (None, {}):
        return ("absent", None)
    return ("unknown", None)


async def rules_visible_sensitivities(session: AsyncSession, case_type_id,
                                      keys: set[str]) -> dict[str, str]:
    """Effective sensitivity per key, as the RULES façade resolves it (namespace
    base, case-type override may only tighten). pii/secret keys read as '***'
    to rules — replay must feed the same constant for parity."""
    if not keys or case_type_id is None:
        return {}
    from case_service.db.models import CaseTypeVariableModel, VariableNamespaceModel

    ns_names = {k.split(".", 1)[0] for k in keys if "." in k}
    ns_rows = (await session.execute(
        select(VariableNamespaceModel).where(VariableNamespaceModel.name.in_(ns_names))
    )).scalars().all() if ns_names else []
    ns_sens = {n.name: n.sensitivity for n in ns_rows}

    defs = (await session.execute(
        select(CaseTypeVariableModel).where(
            CaseTypeVariableModel.case_type_id == case_type_id,
            CaseTypeVariableModel.full_key.in_(keys))
    )).scalars().all()
    overrides = {d.full_key: d.sensitivity_override for d in defs}

    from case_service.case_vars.service import SENSITIVITY_ORDER   # single source of truth
    out: dict[str, str] = {}
    for k in keys:
        base = ns_sens.get(k.split(".", 1)[0], "internal") or "internal"
        ov = overrides.get(k)
        if ov in SENSITIVITY_ORDER and base in SENSITIVITY_ORDER \
                and SENSITIVITY_ORDER.index(ov) > SENSITIVITY_ORDER.index(base):
            out[k] = ov          # stricter-only, mirrors _effective_sensitivity
        else:
            out[k] = base
    return out


async def current_variable_keys(session: AsyncSession, case_id: uuid.UUID) -> set[str]:
    rows = (await session.execute(
        select(CaseInstanceVariableModel.full_key)
        .where(CaseInstanceVariableModel.case_id == case_id)
    )).scalars().all()
    return set(rows)
