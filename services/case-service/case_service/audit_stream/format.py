"""Security events → the payload a SIEM ingests.

**Splunk HEC is the one that is built.** `market-gaps-secondary.md` asked which
SIEM to name and warned against three half-tested ones; HEC covers the most
installations for the least protocol surface — an HTTPS POST of newline-
delimited JSON objects, with a token in a header. CEF-over-syslog covers more
on-prem estates and is fiddlier (framing, facility/severity mapping, escaping
rules that differ per vendor); it is deliberately NOT built, and `kind` on the
target row is the seam it would arrive through.

**What is sent, and what is not.** A security event is metadata about an act:
who, what, on which resource, allowed or denied, from which address. It is not
case content, and `details` is the one field that could carry some, so it is
passed through a redactor rather than shipped whole. This matters more than it
looks: audit egress is justified by legitimate interest and legal obligation,
not by a data subject's consent, and that justification holds only while the
stream stays metadata. A payload that started carrying case data would need the
consent gate HxSync has — and would then go silent for exactly the subjects an
investigator most needs to see.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

#: Keys inside `details` that are never forwarded. Not a redaction of values —
#: the key is dropped entirely, because a SIEM's own index would otherwise hold
#: the thing this list exists to keep out of it.
_NEVER_FORWARD = frozenset({
    "password", "new_password", "old_password", "token", "access_token",
    "refresh_token", "secret", "client_secret", "api_key", "private_key",
    "authorization", "cookie", "session", "otp", "code", "assertion",
    "case_data", "data", "payload", "body", "content", "note", "message",
})

#: Query-string parameters whose VALUE is a credential. The audit middleware
#: records `request.url.query` verbatim, and this platform really does carry
#: capabilities there — `GET /documents/{id}?grant=<token>` is a document access
#: token, and an OAuth/SAML callback carries `?code=` and `?state=`.
#:
#: Shipping those to a SIEM would put a live credential into another system's
#: search index, where it is retained on that system's terms and read by people
#: who are not administrators here. The parameter NAME is kept — "somebody
#: called this with a grant" is exactly the metadata a SOC wants — and only the
#: value goes.
_SECRET_QUERY_PARAMS = frozenset({
    "grant", "token", "access_token", "refresh_token", "id_token", "code",
    "state", "key", "api_key", "apikey", "secret", "password", "otp", "sig",
    "signature", "saml_code", "session", "auth",
})

#: Ordered weakest to strongest, so `min_severity` is a comparison rather than
#: a set membership test an operator has to enumerate.
SEVERITY_ORDER = ("debug", "info", "notice", "warning", "error", "critical")


def severity_rank(severity: str | None) -> int:
    """Where a severity sits in the ladder. Unknown ranks as `info`.

    An unknown severity must not sort BELOW the floor and vanish: a name this
    platform has not seen before is more likely to be new and interesting than
    to be noise.
    """
    if not severity:
        return SEVERITY_ORDER.index("info")
    try:
        return SEVERITY_ORDER.index(severity.strip().lower())
    except ValueError:
        return SEVERITY_ORDER.index("info")


def scrub_query(query: str | None) -> str | None:
    """A query string with every credential value replaced, names kept.

    Deliberately not a drop: an operator reading the SIEM needs to know that a
    request carried a `grant` at all.
    """
    if not query:
        return query
    from urllib.parse import parse_qsl, urlencode

    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except Exception:  # noqa: BLE001
        # Unparseable: it may still hold a credential, so it does not travel.
        return "[unparseable query redacted]"
    if not pairs:
        return query
    return urlencode([
        (k, "[redacted]" if k.strip().lower() in _SECRET_QUERY_PARAMS else v)
        for k, v in pairs
    ])


def redact_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Drop anything from `details` that is content rather than metadata.

    Recursive, because a nested dict is exactly where a payload ends up when
    somebody logs "the request" for debugging.
    """
    if not isinstance(details, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in details.items():
        lowered = str(key).strip().lower()
        if lowered in _NEVER_FORWARD:
            clean[key] = "[redacted]"
            continue
        if lowered == "query" and isinstance(value, str):
            # The one field that is metadata AND can hold a credential.
            clean[key] = scrub_query(value)
            continue
        if isinstance(value, dict):
            clean[key] = redact_details(value)
        elif isinstance(value, list):
            clean[key] = [redact_details(v) if isinstance(v, dict) else v
                          for v in value[:50]]
        else:
            clean[key] = value
    return clean


def _epoch(moment: datetime | None) -> float:
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        # Naive columns are UTC everywhere in this codebase; a naive datetime
        # read as local time is how a three-second lease once displayed as two
        # hours.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def hec_event(event: dict[str, Any], *, host: str, source: str) -> dict[str, Any]:
    """One security event as a Splunk HEC envelope.

    `time` is the event's own timestamp, never the moment it was forwarded: a
    backlog that is caught up an hour later must not land in the SIEM as an hour
    of events happening at once.
    """
    return {
        "time": _epoch(event.get("timestamp")),
        "host": host,
        "source": source,
        "sourcetype": "velaris:security",
        "event": {
            "id": str(event.get("id") or ""),
            "event_type": event.get("event_type"),
            "severity": event.get("severity"),
            "outcome": event.get("outcome"),
            "action": event.get("action"),
            "actor": event.get("user_id"),
            "resource_type": event.get("resource_type"),
            "resource_id": event.get("resource_id"),
            "src_ip": event.get("ip_address"),
            "user_agent": event.get("user_agent"),
            "tenant": event.get("tenant_id"),
            "details": redact_details(event.get("details")),
            # How many identical events this one stands for (migration 231):
            # >1 = a summary of refused repeats within a minute.
            "count": event.get("repeat_count") or 1,
        },
    }


def format_batch(events: list[dict[str, Any]], *, host: str,
                 source: str = "velaris") -> str:
    """The HEC body: one JSON object per event, newline-delimited, no array.

    That is HEC's own framing, not a convenience — an array is rejected.
    """
    return "\n".join(
        json.dumps(hec_event(e, host=host, source=source), default=str)
        for e in events
    )
