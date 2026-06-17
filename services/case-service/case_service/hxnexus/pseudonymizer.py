"""Group H (AI Egress Guard layer 6) — PII pseudonymization before egress.

Structured PII in outgoing text is replaced with stable placeholders before
it leaves the platform for an external AI provider; the mapping stays in
memory for the single call and real values are re-substituted into the
answer. The provider never sees the originals.

Covered (regex-detectable, structured PII):
  emails → ⟨EMAIL_n⟩ · phone numbers → ⟨PHONE_n⟩ · UUIDs → ⟨ID_n⟩
  long digit runs (8+, account/reference numbers) → ⟨NUM_n⟩

Honest limitation: free-text *names* are not reliably detectable without an
NER model and are NOT redacted. Documented in hxnexus.md.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ID",    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    # NUM before PHONE so contiguous account/reference numbers get the right label
    ("NUM",   re.compile(r"\b\d{8,}\b")),
    # International-ish phone: optional +, 8-15 digits with common separators
    ("PHONE", re.compile(r"(?<![\w.])\+?\d[\d ().\-]{6,16}\d(?![\w.])")),
]


class Pseudonymizer:
    """Stateful per-call redactor: redact() outgoing text, restore() the answer."""

    def __init__(self) -> None:
        self._forward: dict[str, str] = {}   # original -> placeholder
        self._reverse: dict[str, str] = {}   # placeholder -> original
        self._counters: dict[str, int] = {}

    @property
    def replaced_count(self) -> int:
        return len(self._forward)

    def _placeholder(self, kind: str, original: str) -> str:
        if original in self._forward:
            return self._forward[original]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        ph = f"⟨{kind}_{self._counters[kind]}⟩"
        self._forward[original] = ph
        self._reverse[ph] = original
        return ph

    def redact(self, text: str) -> str:
        if not text:
            return text
        for kind, pattern in _PATTERNS:
            def _sub(m: re.Match, _kind: str = kind) -> str:
                value = m.group(0)
                # PHONE pattern can over-match digit groups already handled
                if value in self._forward:
                    return self._forward[value]
                return self._placeholder(_kind, value)
            text = pattern.sub(_sub, text)
        return text

    def restore(self, text: str) -> str:
        if not text:
            return text
        for placeholder, original in self._reverse.items():
            text = text.replace(placeholder, original)
        return text
