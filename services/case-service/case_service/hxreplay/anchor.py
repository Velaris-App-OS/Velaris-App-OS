"""HxReplay — result integrity: anchor a run's summary into the audit chain.

The digest of the (canonicalised) summary is written as a case-audit entry, which
the existing hash chain seals (``compliance/audit_chain.seal_new_entries``) and
the TSA anchor loop timestamps (``compliance/audit_anchor``). "We changed the
policy because the simulation showed X" is then itself provable.

Cohort runs have no single case, so the chain entry is carried by the first case
of the cohort — the carrier is arbitrary; the tamper evidence is the chained
digest, which covers the WHOLE summary.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db import repository as repo
from case_service.db.models import ReplayRunModel


def result_digest(summary: dict[str, Any] | None) -> str:
    canonical = json.dumps(summary or {}, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def anchor_run(session: AsyncSession, run: ReplayRunModel,
                     carrier_case_id: uuid.UUID | None = None) -> bool:
    """Digest the run summary and append it to the tamper-evident audit chain.

    Non-fatal by design: a replay result without an anchor is still a result —
    ``anchored`` stays False and the UI can say so.
    """
    run.result_digest = result_digest(run.summary)
    case_id = run.case_id or carrier_case_id
    if case_id is None:
        return False
    try:
        await repo.append_audit_entry(session, data={
            "case_id": case_id,
            "action": "hxreplay.result_anchored",
            "actor_id": run.created_by,
            "actor_type": "system",
            "details": {
                "run_id": str(run.id), "kind": run.kind,
                "result_digest": run.result_digest,
                "config_epoch": run.config_epoch,
            },
        })
    except Exception:
        return False
    run.anchored = True
    return True
