"""HxPrivacy — scan orchestration and finding lifecycle (§8.10).

Runs the detector over every form and data model, and reconciles the result
with what is already recorded.

The reconciliation is the part with teeth. A rescan must be **idempotent and
must not overwrite a human decision**: if a reviewer confirmed or dismissed a
finding, re-running the scanner refreshes when it was last seen and leaves the
decision alone. Otherwise the scan would quietly re-open every dismissal on
each run, and reviewers would learn to ignore the list.

Findings whose field has disappeared are marked `stale` rather than deleted —
the fact that personal data *used to* be collected is itself worth keeping.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.compliance import pii_detect
from velaris_shared.db.models import (
    DataModelModel,
    FormDefinitionModel,
    PIIFindingModel,
)

logger = logging.getLogger(__name__)

DETECTED = "detected"
CONFIRMED = "confirmed"
DISMISSED = "dismissed"
STALE = "stale"

#: A reviewer's verdict. The scanner never changes these.
HUMAN_DECIDED = frozenset({CONFIRMED, DISMISSED})

FORM = "form"
DATA_MODEL = "data_model"


async def scan_all(
    session: AsyncSession, *, tenant_id: str | None = None,
) -> dict[str, Any]:
    """Scan every form and data model; reconcile against stored findings."""
    now = datetime.now(timezone.utc)
    seen: set[tuple[str, str, str]] = set()
    created = updated = 0

    forms = (await session.execute(select(FormDefinitionModel))).scalars().all()
    for form in forms:
        for f in pii_detect.scan_form_definition(form.definition_json or {}):
            key = (FORM, str(form.id), f["field_path"])
            seen.add(key)
            made = await _upsert(
                session, resource_type=FORM, resource_id=form.id,
                resource_name=form.name, finding=f, now=now, tenant_id=tenant_id,
            )
            created += int(made)
            updated += int(not made)

    models = (await session.execute(select(DataModelModel))).scalars().all()
    for dm in models:
        for f in pii_detect.scan_data_model(dm.definition_json or {}):
            key = (DATA_MODEL, str(dm.id), f["field_path"])
            seen.add(key)
            made = await _upsert(
                session, resource_type=DATA_MODEL, resource_id=dm.id,
                resource_name=dm.name, finding=f, now=now, tenant_id=tenant_id,
            )
            created += int(made)
            updated += int(not made)

    stale = await _mark_stale(session, seen=seen, now=now)

    return {
        "scanned_forms": len(forms),
        "scanned_data_models": len(models),
        "findings_created": created,
        "findings_updated": updated,
        "findings_marked_stale": stale,
    }


async def _upsert(
    session: AsyncSession, *, resource_type: str, resource_id,
    resource_name: str | None, finding: dict[str, Any], now, tenant_id,
) -> bool:
    """Insert or refresh one finding. Returns True when newly created."""
    existing = (await session.execute(
        select(PIIFindingModel).where(
            PIIFindingModel.resource_type == resource_type,
            PIIFindingModel.resource_id == resource_id,
            PIIFindingModel.field_path == finding["field_path"],
        )
    )).scalar_one_or_none()

    if existing is not None:
        existing.last_seen_at = now
        # Re-detection of a previously-stale field revives it, but a human
        # verdict is never touched — otherwise every scan would silently
        # re-open every dismissal and reviewers would stop trusting the list.
        if existing.status == STALE:
            existing.status = DETECTED
        if existing.status not in HUMAN_DECIDED:
            existing.category = finding["category"]
            existing.confidence = finding["confidence"]
            existing.reason = finding["reason"]
            existing.special_category = finding["special_category"]
        return False

    session.add(PIIFindingModel(
        resource_type=resource_type, resource_id=resource_id,
        resource_name=resource_name, field_path=finding["field_path"],
        field_label=finding.get("field_label"), category=finding["category"],
        confidence=finding["confidence"], reason=finding["reason"],
        special_category=finding["special_category"],
        status=DETECTED, tenant_id=tenant_id,
        first_seen_at=now, last_seen_at=now,
    ))
    return True


async def _mark_stale(session: AsyncSession, *, seen, now) -> int:
    """Mark findings whose field no longer exists.

    Not deleted: that personal data *was* collected is itself a fact worth
    keeping, and deleting the row would erase the only record of it.
    """
    rows = (await session.execute(
        select(PIIFindingModel).where(PIIFindingModel.status != STALE)
    )).scalars().all()

    n = 0
    for row in rows:
        key = (row.resource_type, str(row.resource_id), row.field_path)
        if key not in seen:
            row.status = STALE
            row.last_seen_at = now
            n += 1
    return n


async def review(
    session: AsyncSession, finding_id, *, decision: str, actor_id: str,
) -> PIIFindingModel:
    """Confirm or dismiss a finding — the human half of §8.10."""
    if decision not in HUMAN_DECIDED:
        raise ValueError(f"decision must be one of {', '.join(sorted(HUMAN_DECIDED))}")

    row = await session.get(PIIFindingModel, finding_id)
    if row is None:
        raise ValueError(f"Finding {finding_id} not found")

    row.status = decision
    row.reviewed_by = actor_id
    row.reviewed_at = datetime.now(timezone.utc)
    return row


async def summary(session: AsyncSession, where=None) -> dict[str, Any]:
    """Counts by status and category, plus the special-category total.

    `where`: the caller's tenant clause. The route scoped its LIST from
    2026-09-17 but not this summary, so every tenant's counts by category
    still reached every tenant's admin (8d, 2026-09-29).
    """
    q = select(PIIFindingModel)
    if where is not None:
        q = q.where(where)
    rows = (await session.execute(q)).scalars().all()

    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_category[r.category] = by_category.get(r.category, 0) + 1

    return {
        "total": len(rows),
        "by_status": by_status,
        "by_category": by_category,
        # Article 9 data carries a higher bar (explicit consent) — surfaced
        # separately so it cannot be read as ordinary personal data.
        "special_category": sum(1 for r in rows if r.special_category),
        "awaiting_review": by_status.get(DETECTED, 0),
    }

# ── Export redaction (§8.10) ──────────────────────────────────────
# The helpers live in `velaris_shared.db.pii_redaction` and are re-exported here so
# compliance stays their documented home. They were moved out because
# `analytics` and `costing` need them, and importing `compliance` from an
# extraction target would mean dragging the whole compliance package —
# audit chain, ledger, DR — along at service-split time. `core` is a leaf.
from velaris_shared.db.pii_redaction import (  # noqa: E402,F401
    REDACTED,
    confirmed_field_paths,
    redact_rows,
)
