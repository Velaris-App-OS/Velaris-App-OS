"""The case-service checks in front of a crypto-shred.

HxVault moved to `velaris_shared.hxvault` (2026-10-19, step 7 — the commerce
split). These two checks need case-service's domain (legal hold, HxArbiter), so
they stayed here and are registered on the shared keyring by
`case_service.hxvault`. The code is what `shred` ran inline before the move.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

logger = logging.getLogger("velaris_shared.hxvault.keyring")   # the same log name as before


async def legal_hold_guard(session, tid) -> None:
    # Shredding the key destroys every case the tenant holds, held ones
    # included (Oct item 13). Refused first, and raised — False means "there
    # was no key", which is a different and reassuring answer.
    if tid:
        from case_service.legal_hold import service as _legal_hold
        await _legal_hold.require_tenant_free(session, tid, "crypto-shredded")


async def arbiter_guard(session, tid) -> None:
    # ── HxArbiter PEP (ARB-C) ────────────────────────────────────────────────
    # Shredding a DEK makes everything under it permanently unrecoverable. It is
    # the single most destructive operation in the platform, and until now the
    # only thing in front of it was the caller's own authorization. The engine
    # gets asked here so a tenant can express "nobody shreds an EU tenant's key
    # without X" as a rule rather than as a convention. Inert while `vault` sits
    # at shadow; a promoted deny raises, because a shred that is refused must
    # not return False — False means "there was no key", which is a different
    # and reassuring answer.
    try:
        from case_service.hxarbiter import pep as _arb_pep

        if not await _arb_pep.check(
            session,
            tenant_id=str(tid) if tid else "default",
            action=_arb_pep.ACTION_VAULT_SHRED,
            subject_id="hxvault",
            resource_kind="tenant_dek",
            resource_id=str(tid) if tid else None,
            context={"purpose": "crypto_shred"},
            legacy_allowed=True,
            claimed_by="hxvault.keyring",
        ):
            raise PermissionError(
                "VEL-ARB-VAULT-SHRED-DENIED-403: policy refuses this crypto-shred.")
    except PermissionError:
        raise
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("HxVault: HxArbiter PEP unavailable for shred: %s", exc)
