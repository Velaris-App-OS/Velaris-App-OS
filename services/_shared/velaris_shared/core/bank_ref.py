"""A data subject's bank reference, encrypted under the tenant's key.

Moved out of `case_service.api.routers.payments` (2026-10-19, step 7 — the
commerce split): the portal (case-service) records disbursements, and the
payments router (commerce-service) does too. Unchanged.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json

from velaris_shared.hxbridge.encryption import encrypt_credentials_vaulted


async def _encrypt_bank_ref(session, value: str, tenant_id) -> str:
    """Encrypt a bank reference for TEXT column storage, under the TENANT's key.

    A bank reference is a data subject's banking detail, so it belongs under a
    key that can be destroyed: erasing a tenant should make this unreadable,
    which is the entire point of the per-tenant DEK. It sat on the platform-wide
    key until 2026-08-24 — sound encryption, but nothing about it could be
    crypto-shredded for one person.

    Falls back to the platform key when no tenant DEK can be resolved, so a
    disbursement never fails over key material and never stores plaintext.
    `_decrypt_bank_ref` reads every format, so existing rows are untouched.
    """
    return json.dumps(
        await encrypt_credentials_vaulted(session, {"v": value}, tenant_id=tenant_id))
