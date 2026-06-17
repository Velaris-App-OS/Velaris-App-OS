"""Official-package registry — the trust anchor for the Official tier.

`official_registry.json` (shipped in the image) is the authoritative allowlist of
package ids that may render as `official`. Tier is decided by
`_effective_tier(source_url, package_id)`: official ONLY IF the source URL's org is
configured official AND the id is listed here. Manifests cannot self-tag, and a
community contributor cannot append a line (it is a platform-release change), so
the Official badge cannot be spoofed even when official and community packages live
in the same GitHub repo/org.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_REGISTRY_FILE = Path(__file__).parent / "official_registry.json"


@lru_cache(maxsize=1)
def official_package_ids() -> frozenset[str]:
    """Return the set of package ids allowed to be Official.

    Fail-closed: any read/parse error yields an EMPTY set (nothing is official),
    never a permissive default — a corrupt registry must not silently bless
    arbitrary packages."""
    try:
        data = json.loads(_REGISTRY_FILE.read_text())
        ids = data.get("official_packages", [])
        if not isinstance(ids, list):
            raise ValueError("official_packages must be a JSON array")
        return frozenset(str(i).strip() for i in ids if str(i).strip())
    except Exception as exc:                       # noqa: BLE001 — fail-closed on any error
        logger.error("Official registry unreadable (%s); treating ALL packages as community.", exc)
        return frozenset()


def is_registered_official(package_id: str) -> bool:
    return package_id in official_package_ids()
