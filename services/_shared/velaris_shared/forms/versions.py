"""Form versions per case (Form Builder §5 step 6, 2026-10-29).

A form whose settings say `"versioning": "per_case"` is pinned for each case the
first time that case uses it — the form a case worker opens, a form submission,
a step completion. From then on the case reads that snapshot, however the form
changes. Without the setting (every form before this) a case reads the current
form, exactly as before; turning the setting off returns a form to that.

A snapshot is one row per distinct content (`content_hash`), never changed, and
kept when the form is deleted, so a pinned case still opens and validates.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


def per_case(definition: Any) -> bool:
    settings = definition.get("settings") if isinstance(definition, dict) else None
    return isinstance(settings, dict) and settings.get("versioning") == "per_case"


def content_hash(definition: Any) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True, default=str).encode()).hexdigest()


async def _snapshot(session: Any, form: Any) -> Any:
    from velaris_shared.db.models import FormSnapshotModel
    h = content_hash(form.definition_json)
    q = select(FormSnapshotModel).where(FormSnapshotModel.form_id == form.id, FormSnapshotModel.content_hash == h)
    found = (await session.execute(q)).scalar_one_or_none()
    if found is not None:
        return found
    try:
        async with session.begin_nested():
            snap = FormSnapshotModel(id=uuid.uuid4(), form_id=form.id, tenant_id=form.tenant_id, content_hash=h,
                                     name=form.name or "", definition_json=form.definition_json)
            session.add(snap)
        return snap
    except IntegrityError:                      # a concurrent first use made it
        return (await session.execute(q)).scalar_one()


async def form_for_case(session: Any, form: Any, case_id: Any) -> dict[str, Any]:
    """The definition this case reads for this form: its pinned snapshot, or
    the current form (pinning it now when the form is versioned per case)."""
    from velaris_shared.db.models import CaseFormVersionModel, FormSnapshotModel
    if form is None or case_id is None or not per_case(form.definition_json):
        return form.definition_json if form is not None else {}
    cid = uuid.UUID(str(case_id))
    pin = await session.get(CaseFormVersionModel, (cid, form.id))
    if pin is not None:
        snap = await session.get(FormSnapshotModel, pin.snapshot_id)
        if snap is not None:
            return snap.definition_json
    snap = await _snapshot(session, form)
    try:
        async with session.begin_nested():
            session.add(CaseFormVersionModel(case_id=cid, form_id=form.id, snapshot_id=snap.id))
    except IntegrityError:                      # a concurrent first use pinned it
        pin = await session.get(CaseFormVersionModel, (cid, form.id))
        if pin is not None and pin.snapshot_id != snap.id:
            other = await session.get(FormSnapshotModel, pin.snapshot_id)
            if other is not None:
                return other.definition_json
    return snap.definition_json
