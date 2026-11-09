"""Canonical tenant identity — **moved to `velaris_shared.tenancy.identity`**.

The implementation left this package on 2026-09-06 and this module re-exports
it, unchanged, under every name it has ever had.

**Why it moved.** `hxguard.require_case` answers "does this operator have a
relationship to this case" and it lives in `velaris_shared`. It could not ask
"…and is the case even in their tenant?" without importing case-service, so for
as long as the answer lived here the tenant boundary was simply absent from the
case-scoped gate — proven on 2026-09-06, when tenant `dev` downloaded the file
content of a document on tenant `default`'s case through
`GET /documents/{id}/download`. Moving the answer down a layer is what let the
gate ask the question.

**Nothing here is reimplemented.** One implementation, one cache: a caller that
does `identity.clear_cache()` through this module clears the same dictionary
`velaris_shared` reads. Duplicating it is the failure this module's own
docstring was written about — two conventions for one idea that do not compare.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from velaris_shared.tenancy.identity import (  # noqa: F401
    DEFAULT_TENANT,
    _STRICT_ENV,
    _as_uuid,
    actor_uuid,
    caller_tenant_or_refuse,
    tenant_for_write,
    caller_tenant_tokens,
    scope_to_caller,
    is_platform_operator,
    case_visible_to,
    clear_actor_cache,
    clear_cache,
    default_tenant_tokens,
    is_superadmin,
    may_reach_tenant,
    resolve_tenant,
    same_tenant,
    same_tenant_as_default,
    strict_case_reads,
    tenant_slug,
)

__all__ = [
    "DEFAULT_TENANT",
    "actor_uuid",
    "caller_tenant_or_refuse",
    "tenant_for_write",
    "case_visible_to",
    "clear_actor_cache",
    "clear_cache",
    "default_tenant_tokens",
    "is_superadmin",
    "may_reach_tenant",
    "resolve_tenant",
    "same_tenant",
    "strict_case_reads",
    "tenant_slug",
]
