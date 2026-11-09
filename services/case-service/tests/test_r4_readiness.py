"""R4 — say so when something is missing (2026-10-30).

Every optional dependency, removed in turn, gives a visible and specific reason
within one request; off by choice is "off", never an error; the endpoint is the
operator's and refuses a viewer; the AI banner asks about the configured model,
not only Ollama; indexing with no embedding model says so.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from case_service import readiness as R

CLOSED = "127.0.0.1:1"      # nothing listens on port 1: refused at once


def _s(**kw):
    base = dict(
        redis_enabled=False, ai_provider="", ai_backend="ollama", ai_api_key="", ai_base_url="",
        ai_openai_api_key="", ai_anthropic_api_key="", ai_egress_policy="minimized",
        ai_ollama_url="http://127.0.0.1:1", ai_ollama_model="llama3.2",
        ai_ollama_embed_model="nomic-embed-text", ai_embed_model="",
        storage_backend="local", storage_local_path="/tmp", temporal_host=CLOSED,
        meet_driver="off_platform", livekit_url="", livekit_http_url="", livekit_api_key="",
        livekit_api_secret="", alert_smtp_host="", alert_smtp_port="", event_bus_enabled=False,
        event_bus_brokers="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _st(item):
    return item["state"], item["reason"]


async def test_database():
    assert R.check_database.__name__
    state, why = _st(await R.check_database(None))
    assert state == R.DEGRADED and "database" in why.lower()


async def test_cache_off_by_choice_and_down(monkeypatch):
    assert _st(await R.check_cache(_s()))[0] == R.OFF
    import velaris_shared.redis_client as rc
    monkeypatch.setattr(rc, "get_redis", AsyncMock(return_value=None))
    state, why = _st(await R.check_cache(_s(redis_enabled=True)))
    assert state == R.DEGRADED and "not answering" in why


async def test_ai_with_no_model_server():
    comp, emb = await R.check_ai(_s(), None)
    assert comp["state"] == emb["state"] == R.DEGRADED
    assert "Ollama" in comp["reason"] and "cannot be searched by meaning" in emb["reason"]


async def test_ai_with_models_not_pulled_names_the_setting():
    comp, emb = await R.check_ai(_s(), ["mistral:latest"])
    assert "VELARIS_CASE_AI_OLLAMA_MODEL" in comp["reason"]
    assert "VELARIS_CASE_AI_OLLAMA_EMBED_MODEL" in emb["reason"]
    comp, emb = await R.check_ai(_s(), ["llama3.2:latest", "nomic-embed-text:latest"])
    assert comp["state"] == emb["state"] == R.WORKING


async def test_ai_external_provider_missing_key_and_configured():
    comp, _ = await R.check_ai(_s(ai_provider="openai"), None)
    assert comp["state"] == R.DEGRADED and "VELARIS_CASE_AI_API_KEY" in comp["reason"]
    comp, emb = await R.check_ai(_s(ai_provider="openai", ai_api_key="k"), None)
    assert comp["state"] == R.WORKING          # not called: a status screen must not spend money
    assert emb["state"] == R.DEGRADED          # embeddings stay local under `minimized`


async def test_ocr(monkeypatch):
    import case_service.documents.ocr as ocr
    monkeypatch.setattr(ocr.shutil, "which", lambda b: None)
    state, why = _st(await R.check_ocr(None))
    assert state == R.DEGRADED and "tesseract" in why
    monkeypatch.setattr(ocr.shutil, "which", lambda b: "/x" if b == "tesseract" else None)
    state, why = _st(await R.check_ocr(None))
    assert state == R.DEGRADED and "poppler" in why


async def test_storage(tmp_path, monkeypatch):
    ro = tmp_path / "ro"; ro.mkdir(); ro.chmod(0o500)
    state, why = _st(await R.check_storage(_s(storage_local_path=str(ro))))
    assert state == R.DEGRADED and "VELARIS_CASE_STORAGE_LOCAL_PATH" in why
    assert _st(await R.check_storage(_s(storage_local_path=str(tmp_path))))[0] == R.WORKING
    import velaris_shared.storage.factory as f
    broken = MagicMock(); broken.exists = AsyncMock(side_effect=ConnectionError())
    monkeypatch.setattr(f, "get_storage_backend", lambda: broken)
    state, why = _st(await R.check_storage(_s(storage_backend="minio")))
    assert state == R.DEGRADED and "uploads and downloads fail" in why


async def test_temporal():
    state, why = _st(await R.check_temporal(_s()))
    assert state == R.DEGRADED and "VELARIS_CASE_TEMPORAL_HOST" in why


async def test_meetings():
    assert _st(await R.check_meetings(_s()))[0] == R.OFF
    state, why = _st(await R.check_meetings(_s(meet_driver="livekit")))
    assert state == R.DEGRADED and "key or secret" in why
    state, _ = _st(await R.check_meetings(_s(meet_driver="livekit", livekit_url="ws://127.0.0.1:1",
                                             livekit_api_key="k", livekit_api_secret="s")))
    assert state == R.DEGRADED


async def test_email(session):
    from velaris_shared.db.models import EmailAccountModel
    state, why = _st(await R.check_email(session))
    assert state == R.OFF and "sign-in" in why
    session.add(EmailAccountModel(id=uuid.uuid4(), name="r4", address="r4@test.local",
                                  smtp_host="127.0.0.1", smtp_port=1, is_default_outbound=True,
                                  is_active=True))
    await session.commit()
    state, why = _st(await R.check_email(session))
    assert state == R.DEGRADED and "does not accept connections" in why


async def test_alert_email():
    assert _st(await R.check_alert_email(_s()))[0] == R.OFF
    assert _st(await R.check_alert_email(_s(alert_smtp_host="127.0.0.1", alert_smtp_port="1")))[0] == R.DEGRADED


async def test_connectors_are_the_callers_tenants(session):
    from velaris_shared.auth.models import AuthenticatedUser
    from velaris_shared.db.models import ConnectorRegistryModel as C
    sms, conn = await R.check_connectors(session, None)
    session.add_all([
        C(id=uuid.uuid4(), name="acme-crm", connector_type="rest", tenant_id="acme-r4", last_test_ok=False),
        C(id=uuid.uuid4(), name="evil-sms", connector_type="twilio", tenant_id="evil-r4", last_test_ok=False),
    ])
    await session.commit()
    acme = AuthenticatedUser(user_id="a", roles=["admin"], tenant_id="acme-r4")
    sms, conn = await R.check_connectors(session, acme)
    assert conn["state"] == R.DEGRADED and "acme-crm" in conn["reason"]
    assert "evil-sms" not in conn["reason"] and sms["state"] == R.OFF


async def test_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("VELARIS_INSTALL_ROOT", str(tmp_path))
    state, why = _st(await R.check_secrets(None))
    assert state == R.OFF and ".env" in why
    (tmp_path / "deploy/openbao").mkdir(parents=True)
    (tmp_path / "deploy/openbao/enabled").touch()
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(side_effect=httpx.ConnectError("x")))
    assert _st(await R.check_secrets(None))[0] == R.DEGRADED


async def test_event_bus():
    assert _st(await R.check_event_bus(_s()))[0] == R.OFF
    state, why = _st(await R.check_event_bus(_s(event_bus_enabled=True, event_bus_brokers=CLOSED)))
    assert state == R.DEGRADED and "outbox" in why


async def test_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("VELARIS_INSTALL_ROOT", str(tmp_path))
    monkeypatch.delenv("VELARIS_BACKUP_DIR", raising=False)
    assert _st(await R.check_backup(None))[0] == R.OFF


def test_summary_lines_name_every_degraded_item():
    rep = {"summary": {"working": 1, "degraded": 1, "off": 1}, "items": [
        R._item("a", "A", R.WORKING, "fine"), R._item("b", "B", R.DEGRADED, "why b"),
        R._item("c", "C", R.OFF, "chosen")]}
    assert R.summary_lines(rep) == ["Health: 1 working, 1 degraded, 1 off", "  ⚠ B: why b"]


def test_the_update_prints_it():
    root = Path(__file__).resolve().parents[3]
    text = (root / "update-velaris.sh").read_text()
    assert "-m case_service.readiness" in text and "|| true" in text.split("-m case_service.readiness")[1][:200]


# ── through the API, as the people it is for ─────────────────────────────────

def _headers(user_id: str, roles: list[str]) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings
    s = get_settings()
    t = create_dev_token(user_id=user_id, username=user_id, roles=roles,
                         secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {t}"}


async def test_readiness_endpoint_admin_sees_it_viewer_is_refused(client):
    R._cache["items"] = None
    r = await client.get("/api/v1/status/readiness", headers=_headers("r4-admin", ["admin"]))
    assert r.status_code == 200, r.text
    body = r.json()
    keys = {i["key"] for i in body["items"]}
    assert {"database", "storage", "ai_completion", "ai_embedding", "ocr", "email", "alert_email",
            "sms", "connectors", "temporal", "cache", "meetings", "event_bus", "secrets",
            "backup"} <= keys
    assert all(i["state"] in (R.WORKING, R.DEGRADED, R.OFF) and i["reason"] for i in body["items"])
    assert sum(body["summary"].values()) == len(body["items"])
    r = await client.get("/api/v1/status/readiness", headers=_headers("r4-viewer", ["viewer"]))
    assert r.status_code == 403


async def test_ai_banner_asks_about_the_configured_model(client, monkeypatch):
    # A cloud model and no Ollama read as "AI unavailable" until R4.
    from velaris_shared.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "ai_provider", "openai")
    monkeypatch.setattr(s, "ai_api_key", "sk-test")
    monkeypatch.setattr(s, "ai_ollama_url", "http://127.0.0.1:1")
    r = await client.get("/api/v1/sitemap/ai-status", headers=_headers("r4-viewer", ["viewer"]))
    assert r.status_code == 200 and r.json()["ai_available"] is True
    monkeypatch.setattr(s, "ai_provider", "")
    r = await client.get("/api/v1/sitemap/ai-status", headers=_headers("r4-viewer", ["viewer"]))
    assert r.json()["ai_available"] is False


async def test_indexing_with_no_embedding_model_says_so(session):
    from case_service.hxnexus.service import index_document, not_embedded_note
    b = MagicMock(); b.available = True; b.embed = AsyncMock(return_value=[])
    report: dict = {}
    n = await index_document(session, uuid.uuid4(), uuid.uuid4(), ("words here. " * 200).encode(),
                             "text/plain", None, backend=b, report=report)
    assert n > 0 and report == {"embedded": 0, "not_embedded": n}
    note = not_embedded_note(report)
    assert note and "without an embedding" in note and "Status" in note
    b.embed = AsyncMock(return_value=[0.1] * 8)
    report = {}
    await index_document(session, uuid.uuid4(), uuid.uuid4(), b"some text " * 50, "text/plain",
                         None, backend=b, report=report)
    assert not_embedded_note(report) is None


async def test_another_tenants_admin_sees_only_what_their_users_depend_on(client, session):
    # The install's machinery — backups (whose reason can say where the backup
    # key sits), OpenBao, the alert mailbox — is the platform operator's.
    from velaris_shared.db.models import EmailAccountModel, TenantModel, UserDirectoryModel
    from case_service.tenancy import identity as tenancy
    tenancy.clear_cache()
    session.add_all([
        TenantModel(id=uuid.uuid4(), slug="acme-r4t", name="Acme R4"),
        UserDirectoryModel(id=uuid.uuid4(), user_id="r4-acme-admin", tenant_id="acme-r4t"),
        # the platform's own mailbox works; acme has none
        EmailAccountModel(id=uuid.uuid4(), name="platform", address="p@test.local",
                          smtp_host="127.0.0.1", smtp_port=1, is_default_outbound=True,
                          is_active=True, tenant_id=None),
    ])
    await session.commit()
    R._cache["items"] = None
    r = await client.get("/api/v1/status/readiness", headers=_headers("r4-acme-admin", ["admin"]))
    assert r.status_code == 200, r.text
    keys = {i["key"] for i in r.json()["items"]}
    assert keys == set(R.TENANT_VISIBLE)
    email = next(i for i in r.json()["items"] if i["key"] == "email")
    assert email["state"] == R.OFF            # acme's own mailbox: none — not the platform's
    assert "This tenant" in email["reason"]
    r = await client.get("/api/v1/status/readiness", headers=_headers("r4-operator", ["admin"]))
    keys = {i["key"] for i in r.json()["items"]}
    assert {"backup", "secrets", "alert_email", "temporal", "cache", "event_bus"} <= keys
    email = next(i for i in r.json()["items"] if i["key"] == "email")
    assert email["state"] == R.DEGRADED       # the platform's mailbox, port 1 refused
