"""`curl -w '%{http_code}' … || echo 000` yields "000000" when nothing answers:
curl itself prints 000, then the fallback appends another. Every wait loop
compared with "000", so it ended on the first try and the unseal/init after it
was skipped — OpenBao stayed sealed after a reboot (R5b drill, 2026-10-30)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
_BAD = re.compile(r"%\{http_code\}[^)]*?\|\|\s*echo\s*['\"]?0+['\"]?\s*\)", re.S)


def test_no_script_appends_a_second_000_to_curls_own():
    hits = []
    for p in ROOT.rglob("*.sh"):
        if any(part in {"node_modules", "Velaris", ".venv"} for part in p.parts):
            continue
        if _BAD.search(p.read_text(errors="replace")):
            hits.append(str(p.relative_to(ROOT)))
    assert not hits, f"curl -w http_code with `|| echo 000` (gives 000000): {hits}"
