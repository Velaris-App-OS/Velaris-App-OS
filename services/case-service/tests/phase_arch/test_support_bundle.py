"""R7 — the support bundle holds no secret and no personal data a pattern can see."""
from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("support_bundle", ROOT / "scripts/support_bundle.py")
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

SECRETS = [
    "postgresql+asyncpg://velaris:5d16633b4312a435b307e238fb5f6ea6@localhost:6432/velaris",
    "Authorization: Bearer abc.def.ghijklmnopqrstuvwxyz0123",
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJlLXZhbHVl",
    "VELARIS_CASE_AUTH_SECRET=0f3c9a1b2d4e6f8091a2b3c4d5e6f708",
    'password: "hunter2-but-longer"',
    "api_key=sk-live-AbCdEfGh12345",
    "smtp_password=letmein99",
    "{'password': 'pyrepr-secret', 'api_key': 'k-123456'}",
    "contact ada.lovelace@example.org about it",
    "from 203.0.113.77 and 2001:db8:85a3::8a2e:370:7334",
    "call +44 20 7946 0958",
    "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
    "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq",
    "/home/ada/helix/services/x.py",
]
MUST_VANISH = ["5d16633b4312a435b307e238fb5f6ea6", "ghijklmnopqrstuvwxyz0123", "eyJhbGci",
               "0f3c9a1b2d4e6f8091a2b3c4d5e6f708", "hunter2", "sk-live", "letmein99", "pyrepr-secret", "k-123456",
               "ada.lovelace", "203.0.113.77", "2001:db8", "7946", "MIIEvQIB",
               "age1qyqszqgp", "/home/ada/"]


def test_every_secret_and_personal_detail_is_redacted():
    out = sb.redact("\n".join(SECRETS))
    for bit in MUST_VANISH:
        assert bit not in out, bit


def test_what_is_useful_survives():
    line = ('2026-10-30T20:39:26+01:00 case-service: INFO GET /api/v1/cases/'
            '6f1c2e9a-1b2c-4d3e-8f90-123456789abc 200 in 12:30:45 from 127.0.0.1 '
            'File "/srv/velaris/services/case-service/case_service/readiness.py", line 3')
    assert sb.redact(line) == line


def test_settings_are_named_never_valued(tmp_path):
    env = tmp_path / ".env"
    env.write_text("VELARIS_CASE_AUTH_SECRET=topsecretvalue\nexport VELARIS_DB_PASSWORD='x'\n# c=1\n")
    out = sb.setting_names(env)
    assert out.split() == ["VELARIS_CASE_AUTH_SECRET", "VELARIS_DB_PASSWORD"]
    assert "topsecretvalue" not in out


def test_the_bundle_is_private_and_carries_only_what_it_lists(tmp_path):
    files = {"README.txt": "hi\n", "logs/a.log": sb.redact("user ada@example.org\n")}
    path = sb.write(files, tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600
    with tarfile.open(path) as t:
        names = [m.name.split("/", 1)[1] for m in t.getmembers()]
        assert sorted(names) == ["README.txt", "logs/a.log"]
        assert all(m.mode == 0o600 for m in t.getmembers())
        assert b"ada@" not in t.extractfile(t.getmembers()[1]).read()


def test_never_reads_keys_or_the_database():
    src = (ROOT / "scripts/support_bundle.py").read_text()
    for forbidden in ("velaris-install-report", "deploy/tls/certs", "role_id", "secret_id",
                      "pg_dump", ".bao-init"):
        assert forbidden not in src, forbidden
    assert "velaris-support-*.tar.gz" in (ROOT / ".gitignore").read_text()
