#!/usr/bin/env python
"""Generate the MySQL bpm_concepts parity seed from the Postgres migration (DB SDK 1b).

bpm_concepts (PG migration 040) is advisory reference data for the HxMigrate BPM importer
(BPM-tool concept → Helix concept mapping). It is cosmetic-parity (the importer degrades
gracefully without it), 66 rows across 4 tools, and the table has NO unique key — so it
can't use ON DUPLICATE KEY for idempotency. This generator transcodes the PG VALUES rows
into a single idempotent INSERT…SELECT…WHERE NOT EXISTS block (id=UUID(), created_at=NOW()
supplied since the metadata-built MySQL schema lacks PG's server defaults), written to a
SEPARATE file so the boot-critical 0002_seed.sql stays lean.

Usage:
    python scripts/gen_mysql_bpm_seed.py            # writes migrations/mysql/0003_seed_bpm_concepts.sql
    python scripts/gen_mysql_bpm_seed.py --check     # non-zero exit if the file is stale

Regenerate + hand-review the diff whenever migration 040 changes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = next((ROOT / "migrations" / "postgresql").glob("040_*.sql"))
OUT = ROOT / "migrations" / "mysql" / "0003_seed_bpm_concepts.sql"

# A bpm_concepts VALUES row is a single line beginning with ('<tool>', … and closing on
# the same line. (Verified: all 66 rows are single-line.)
_ROW = re.compile(r"^\('(?:pega|camunda|appian|servicenow)'.*\)\s*[,;]?\s*(?:--.*)?$")

HEADER = """\
-- ============================================================================
-- Velaris — MySQL bpm_concepts parity seed (DB SDK Phase 1b). GENERATED — DO NOT
-- EDIT BY HAND; regenerate via scripts/gen_mysql_bpm_seed.py from the Postgres
-- migration 040 and hand-review the diff.
--
-- Advisory reference data for the HxMigrate BPM importer (cosmetic parity — the
-- importer degrades gracefully without it). The table has no unique key, so this is
-- one idempotent INSERT…SELECT guarded by WHERE NOT EXISTS (re-applying is a no-op).
-- ============================================================================
"""


def _split_top_level(body: str) -> list[str]:
    """Split a VALUES tuple body on top-level commas, respecting '…' string literals
    (with '' as the escaped quote)."""
    out, buf, i, in_str = [], [], 0, False
    while i < len(body):
        ch = body[i]
        if ch == "'":
            buf.append(ch)
            if in_str and i + 1 < len(body) and body[i + 1] == "'":  # '' escape
                buf.append("'"); i += 2; continue
            in_str = not in_str
        elif ch == "," and not in_str:
            out.append("".join(buf).strip()); buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf).strip())
    return out


def _row_body(line: str) -> str:
    """Strip the outer parens + trailing punctuation/comment from a VALUES row line."""
    s = line.strip()
    s = re.sub(r"\s*(?:--.*)?$", "", s)        # drop trailing comment/space
    s = s.rstrip(";,").strip()
    assert s.startswith("(") and s.endswith(")"), s
    return s[1:-1].strip()


def render() -> str:
    rows = [_row_body(l) for l in SRC.read_text().splitlines() if _ROW.match(l)]
    if not rows:
        raise SystemExit("no bpm_concepts rows found in migration 040 — generator stale")

    # First row carries column aliases for the derived table; the rest are bare SELECTs.
    first = _split_top_level(rows[0])
    assert len(first) == 8, f"expected 8 columns, got {len(first)}: {first}"
    aliased = ", ".join(f"{v} AS c{i+1}" for i, v in enumerate(first))

    lines = [f"  SELECT {aliased}"]
    lines += [f"  UNION ALL SELECT {r}" for r in rows[1:]]
    derived = "\n".join(lines)

    return (
        HEADER
        + "\nINSERT INTO bpm_concepts\n"
        + "  (id, source_tool, source_concept, helix_equiv, helix_node_type,\n"
        + "   description, example, confidence, notes, created_at)\n"
        + "SELECT UUID(), t.c1, t.c2, t.c3, t.c4, t.c5, t.c6, t.c7, t.c8, NOW()\n"
        + "FROM (\n" + derived + "\n) t\n"
        + "WHERE NOT EXISTS (SELECT 1 FROM bpm_concepts LIMIT 1);\n"
    )


def main() -> int:
    sql = render()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != sql:
            print(f"STALE: {OUT} is out of date — run: python scripts/gen_mysql_bpm_seed.py")
            return 1
        print(f"OK: {OUT} is up to date")
        return 0
    OUT.write_text(sql)
    print(f"wrote {OUT} ({sql.count('SELECT') - 1} concept rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
