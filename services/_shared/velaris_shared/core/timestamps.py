"""One way to put a timestamp on the wire.

Some columns in this schema are `timestamp with time zone` and some are
`timestamp without time zone`. **Both hold UTC** — the difference is only
whether the driver hands back an aware datetime. `.isoformat()` then emits an
offset for one and not the other, and a browser reads a bare timestamp as
*local* time. Two columns describing the same instant land on screen hours
apart.

That is not cosmetic on an audit surface. The Bot Console's Audit view showed a
credential leased at 08:27:32 and revoked at 10:27:35 — a three-second lease
displayed as a two-hour one, on the exact screen somebody would open to ask how
long a bot held a supplier's password. Found in a browser on 2026-08-21; the
API's own JSON had said the same thing for as long as the view existed.

`iso_utc` is the boundary fix: a naive value is stamped UTC (which is what it
already was), an aware one is converted, and the result always carries an
offset. Use it for every datetime a router serialises.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(value: datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset, or None.

    Naive input is *assumed* UTC rather than converted from local time. Every
    writer in this service stamps `datetime.now(timezone.utc)`, so the assumption
    holds; guessing the server's local zone instead would silently shift stored
    history whenever the deployment moved.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()
