"""DB SDK Phase 1b — Postgres/MySQL parity gates.

These run on the default (SQLite) harness — no DB needed, they compare generated
artifacts against committed files — so they are always-on CI guards against the
recurring "I changed the Postgres side and forgot the MySQL side" drift that has
bitten this project repeatedly (helix_settings missing from the baseline, a unique
constraint present in the model but not the shipped DDL, a feature added to one
manifest but not the other).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# services/case-service/tests/phase_dbsdk/test_parity.py → repo root is parents[4]
_ROOT = Path(__file__).resolve().parents[4]


def test_mysql_baseline_in_sync_with_orm_models():
    """migrations/mysql/0001_baseline.sql must equal what the generator emits from the
    current ORM metadata. Fails if a model/table/index/constraint was added or changed
    without regenerating the baseline — i.e. the table would silently be missing on a
    fresh MySQL install (the helix_settings class of bug)."""
    script = _ROOT / "scripts" / "gen_mysql_baseline.py"
    assert script.exists(), f"baseline generator not found at {script}"
    r = subprocess.run([sys.executable, str(script), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "MySQL baseline has drifted from the ORM models — regenerate with "
        "`python scripts/gen_mysql_baseline.py` and review the diff.\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def test_mysql_bpm_seed_in_sync_with_pg_migration():
    """migrations/mysql/0003_seed_bpm_concepts.sql must equal what the generator emits
    from the Postgres migration 040. Fails if a bpm_concept was added/changed on the PG
    side without regenerating the MySQL parity seed."""
    script = _ROOT / "scripts" / "gen_mysql_bpm_seed.py"
    assert script.exists(), f"bpm seed generator not found at {script}"
    r = subprocess.run([sys.executable, str(script), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "MySQL bpm_concepts parity seed has drifted from Postgres migration 040 — "
        "regenerate with `python scripts/gen_mysql_bpm_seed.py`.\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def _manifest_features(path: Path) -> set[tuple[str, str]]:
    """Extract (feature_key, version) pairs from a release-manifest seed file. The first
    VALUES column is the dialect's UUID function (gen_random_uuid()/UUID()), the 2nd is
    feature_key, the 3rd is version."""
    # Strip SQL line-comments first — both manifests carry a commented-out template
    # row (`-- (gen_random_uuid(), 'feature_key', ...)`) that must not be parsed as a
    # real feature.
    sql = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("--"))
    pairs = re.findall(
        r"(?:gen_random_uuid\(\)|UUID\(\))\s*,\s*'([a-z_]+)'\s*,\s*'([^']+)'",
        sql,
    )
    return set(pairs)


def test_release_manifests_in_lockstep():
    """releases/manifest.sql (Postgres) and manifest.mysql.sql must seed the SAME set of
    features at the SAME versions. The runner compares INSERT counts for parity, but a
    swapped key/version would pass that check — so assert the actual (key, version) sets
    match, guarding the 'added a feature to one manifest only' drift."""
    pg = _manifest_features(_ROOT / "releases" / "manifest.sql")
    my = _manifest_features(_ROOT / "releases" / "manifest.mysql.sql")
    assert pg, "no features parsed from manifest.sql — extraction regex may be stale"
    assert pg == my, (
        "Postgres and MySQL release manifests are out of lockstep.\n"
        f"  only in manifest.sql:       {sorted(pg - my)}\n"
        f"  only in manifest.mysql.sql: {sorted(my - pg)}"
    )
