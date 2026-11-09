"""Say so when something is missing — every optional dependency, one answer (R4).

Before this, a missing embedding model returned empty vectors and a warning in a
log nobody reads, and document Q&A kept "working". Studio's banner asked only
whether Ollama answered — so a box with a cloud model and no Ollama showed "AI
unavailable", and a box with Ollama but no model pulled showed nothing at all.

Each dependency is reported in one of three states, with a plain reason:

    working    it answered (or, for a paid provider, is fully configured — a
               status screen must not spend money on every refresh)
    degraded   it is meant to be on and is not answering, or is half-set-up;
               the reason says what the user loses and what to set
    off        turned off, or never set up — by choice, never an error

Nothing here turns anything on or writes anything. Every probe is bounded
(PROBE_TIMEOUT) and runs concurrently; a probe that raises is reported as
degraded, never as a crash. Reasons name settings, never hosts, keys or URLs.
The platform operator sees every line; another tenant's administrator sees only
what their users depend on (TENANT_VISIBLE), with their own mailbox and
connectors.

Used by `GET /api/v1/status/readiness` (Studio's Status screen), the AI banner
(`/sitemap/ai-status`), and `python -m case_service.readiness` — the closing
summary of update-velaris.sh.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

WORKING, DEGRADED, OFF = "working", "degraded", "off"
PROBE_TIMEOUT = 3.0
CACHE_SECONDS = 20.0

_cache: dict[str, Any] = {"at": 0.0, "items": None}


def _item(key: str, label: str, state: str, reason: str) -> dict[str, str]:
    return {"key": key, "label": label, "state": state, "reason": reason}


async def _tcp(target: str, default_port: int) -> bool:
    """Does something accept a TCP connection at host[:port]? Bounded."""
    host, _, port = target.rpartition(":") if ":" in target else (target, "", "")
    host = host or target
    try:
        p = int(port) if port else default_port
    except ValueError:
        p = default_port
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), p), PROBE_TIMEOUT)
        w.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit
    u = urlsplit(url if "://" in url else f"tcp://{url}")
    return f"{u.hostname}:{u.port}" if u.port else (u.hostname or "")


# ── the checks ───────────────────────────────────────────────────────────────

async def check_database(session) -> dict:
    from sqlalchemy import text
    if session is None:
        return _item("database", "Database", DEGRADED, "No database session — nothing can be stored.")
    try:
        await session.execute(text("SELECT 1"))
        return _item("database", "Database", WORKING, "Answering.")
    except Exception as exc:  # noqa: BLE001
        return _item("database", "Database", DEGRADED,
                     f"Not answering ({type(exc).__name__}) — the platform cannot store or read work.")


async def check_cache(s) -> dict:
    label = "Shared cache (Valkey)"
    if not s.redis_enabled:
        return _item("cache", label, OFF,
                     "Turned off (VELARIS_CASE_REDIS_ENABLED). Live updates and limits work "
                     "within one process only.")
    try:
        from velaris_shared.redis_client import get_redis
        r = await get_redis()
        if r is None or not await asyncio.wait_for(r.ping(), PROBE_TIMEOUT):
            raise RuntimeError("no answer")
        return _item("cache", label, WORKING, "Answering.")
    except Exception:  # noqa: BLE001
        return _item("cache", label, DEGRADED,
                     "On but not answering — live updates stop between processes and each "
                     "process keeps its own limits until it is back.")


async def _ollama_models(s) -> list[str] | None:
    """The models the local Ollama has pulled; None when it does not answer."""
    import httpx
    from velaris_shared.llm.ollama_backend import ollama_client_kwargs
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=False,
                                     **ollama_client_kwargs()) as c:
            r = await c.get(f"{s.ai_ollama_url.rstrip('/')}/api/tags")
            if r.status_code >= 400:
                return None
            return [str(m.get("name", "")) for m in r.json().get("models", [])]
    except Exception:  # noqa: BLE001
        return None


def _pulled(models: list[str], name: str) -> bool:
    want = name.strip()
    base = want.split(":", 1)[0]
    return any(m == want or (":" not in want and m.split(":", 1)[0] == base) for m in models)


def _external_provider(s) -> str:
    p = (s.ai_provider or "").strip().lower()
    if p and p != "ollama":
        return p
    b = (s.ai_backend or "ollama").strip().lower()
    if b == "openai" and s.ai_openai_api_key:
        return "openai"
    if b == "anthropic" and s.ai_anthropic_api_key:
        return "anthropic"
    return ""


async def check_ai(s, models: list[str] | None) -> tuple[dict, dict]:
    """(completion model, embedding model)."""
    from velaris_shared.llm.factory import detect_provider_misconfig

    provider = _external_provider(s)
    policy = (s.ai_egress_policy or "minimized").strip().lower()
    fault = detect_provider_misconfig(s)

    # completion
    if fault:
        completion = _item("ai_completion", "AI model", DEGRADED,
                           f"Provider '{fault['provider']}' is missing {fault['missing']} — "
                           "AI answers come from the local model, if there is one.")
    elif provider and policy != "local_only":
        completion = _item("ai_completion", "AI model", WORKING,
                           f"External provider '{provider}' is configured (not called by this check).")
    else:
        model = s.ai_ollama_model
        if models is None:
            completion = _item("ai_completion", "AI model", DEGRADED,
                               "The local model server (Ollama) is not answering — AI features "
                               "(case Q&A, drafting, summaries) return nothing.")
        elif not _pulled(models, model):
            completion = _item("ai_completion", "AI model", DEGRADED,
                               f"The local model '{model}' is not pulled (VELARIS_CASE_AI_OLLAMA_MODEL) "
                               "— AI features return nothing.")
        else:
            completion = _item("ai_completion", "AI model", WORKING, f"Local model '{model}' is ready.")

    # embeddings: always local unless egress policy is `full` with an external key
    if provider in ("openai",) and policy == "full" and not fault:
        embedding = _item("ai_embedding", "AI embeddings", WORKING,
                          f"External provider '{provider}' embeds documents (egress policy full).")
    else:
        em = s.ai_embed_model or s.ai_ollama_embed_model
        if models is None:
            embedding = _item("ai_embedding", "AI embeddings", DEGRADED,
                              "The local model server (Ollama) is not answering — documents are "
                              "stored but cannot be searched by meaning, and document Q&A finds nothing.")
        elif not _pulled(models, em):
            embedding = _item("ai_embedding", "AI embeddings", DEGRADED,
                              f"The embedding model '{em}' is not pulled "
                              "(VELARIS_CASE_AI_OLLAMA_EMBED_MODEL) — documents are stored but cannot "
                              "be searched by meaning, and document Q&A finds nothing.")
        else:
            embedding = _item("ai_embedding", "AI embeddings", WORKING, f"Embedding model '{em}' is ready.")
    return completion, embedding


async def check_ocr(_s) -> dict:
    from case_service.documents.ocr import TesseractOCREngine as T
    if not T.available():
        return _item("ocr", "Text from scans (OCR)", DEGRADED,
                     "tesseract is not installed — a scanned document is stored with no text, "
                     "so it cannot be searched.")
    if not T.can_rasterise():
        return _item("ocr", "Text from scans (OCR)", DEGRADED,
                     "poppler (pdftoppm) is not installed — scanned images are read, scanned PDFs are not.")
    return _item("ocr", "Text from scans (OCR)", WORKING, "tesseract and poppler are installed.")


async def check_storage(s) -> dict:
    label = "Document storage"
    mode = (s.storage_backend or "local").strip().lower()
    if mode == "local":
        p = Path(s.storage_local_path)
        ok = p.is_dir() and os.access(p, os.W_OK)
        if not ok and not p.exists():
            ok = os.access(p.parent if p.parent.exists() else Path("."), os.W_OK)
        return (_item("storage", label, WORKING, "Local disk, writable.") if ok else
                _item("storage", label, DEGRADED,
                      "The local document folder is not writable (VELARIS_CASE_STORAGE_LOCAL_PATH) — "
                      "uploads fail."))
    try:
        from velaris_shared.storage.factory import get_storage_backend
        await asyncio.wait_for(get_storage_backend().exists("__velaris_readiness__"), PROBE_TIMEOUT)
        return _item("storage", label, WORKING, f"Object storage ({mode}) answering.")
    except Exception as exc:  # noqa: BLE001
        return _item("storage", label, DEGRADED,
                     f"Object storage ({mode}) is not answering ({type(exc).__name__}) — uploads and "
                     "downloads fail.")


async def check_temporal(s) -> dict:
    label = "Workflow engine (Temporal)"
    if await _tcp(s.temporal_host, 7233):
        return _item("temporal", label, WORKING, "Answering.")
    return _item("temporal", label, DEGRADED,
                 "Not answering (VELARIS_CASE_TEMPORAL_HOST) — long-running workflows wait until it is back.")


async def check_meetings(s) -> dict:
    label = "Meetings (LiveKit)"
    driver = (s.meet_driver or "off_platform").strip().lower()
    if driver != "livekit":
        return _item("meetings", label, OFF,
                     "Meetings use an outside link (VELARIS_CASE_MEET_DRIVER) — no in-platform video.")
    if not (s.livekit_api_key and s.livekit_api_secret and (s.livekit_url or s.livekit_http_url)):
        return _item("meetings", label, DEGRADED,
                     "VELARIS_CASE_MEET_DRIVER=livekit but its URL, key or secret is not set — "
                     "meetings cannot start.")
    target = _host_of(s.livekit_http_url or s.livekit_url.replace("wss://", "https://").replace("ws://", "http://"))
    if target and await _tcp(target, 7880):
        return _item("meetings", label, WORKING, "Answering.")
    return _item("meetings", label, DEGRADED, "Not answering — meetings cannot start.")


async def check_email(session, user=None) -> dict:
    """The CALLER'S tenant's default outbound mailbox (one per tenant); no
    caller = the platform's own (default tenant), as the update script asks."""
    label = "Email (outbound)"
    from sqlalchemy import select
    from case_service.mail import tenancy as mail_tenancy
    from velaris_shared.db.models import EmailAccountModel
    scope = await mail_tenancy.scope_for_token(session, getattr(user, "tenant_id", None))
    try:
        acct = (await session.execute(
            select(EmailAccountModel).where(EmailAccountModel.is_default_outbound.is_(True),
                                            EmailAccountModel.is_active.is_(True),
                                            mail_tenancy.clause(scope, EmailAccountModel.tenant_id))
            .limit(1)
        )).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        acct = None
    if acct is None:
        if scope.owns_unassigned:      # the platform's own mailbox: sign-in codes use it
            return _item("email", label, OFF,
                         "No default outbound mailbox — sign-in, reset and notification emails are "
                         "not sent. Add one in Email settings.")
        return _item("email", label, OFF,
                     "This tenant has no default outbound mailbox — its case emails are not sent. "
                     "Add one in Email settings.")
    if not acct.smtp_host:
        return _item("email", label, DEGRADED, "The default outbound mailbox has no SMTP server set.")
    if await _tcp(f"{acct.smtp_host}:{acct.smtp_port or 587}", 587):
        return _item("email", label, WORKING, "The outbound mail server accepts connections.")
    return _item("email", label, DEGRADED,
                 "The outbound mail server does not accept connections — emails wait or fail.")


async def check_alert_email(s) -> dict:
    label = "Operator alert email"
    if not (s.alert_smtp_host or "").strip():
        return _item("alert_email", label, OFF,
                     "Not set up (VELARIS_CASE_ALERT_SMTP_HOST) — backup and health alerts show in "
                     "Studio only.")
    port = s.alert_smtp_port.strip() if str(s.alert_smtp_port or "").strip().isdigit() else "587"
    if await _tcp(f"{s.alert_smtp_host}:{port}", 587):
        return _item("alert_email", label, WORKING, "The alert mail server accepts connections.")
    return _item("alert_email", label, DEGRADED,
                 "The alert mail server does not accept connections — operator alerts are not emailed.")


async def check_connectors(session, user) -> tuple[dict, dict]:
    """(SMS, connectors) — from each connector's own last test, never a live call."""
    from sqlalchemy import select
    from velaris_shared.db.models import ConnectorRegistryModel as C
    from velaris_shared.tenancy.identity import scope_to_caller
    stmt = select(C)
    if user is not None:
        stmt = await scope_to_caller(stmt, C, session, user)
    try:
        rows = (await session.execute(stmt)).scalars().all()
    except Exception:  # noqa: BLE001
        rows = []
    active = [r for r in rows if r.enabled is not False]
    sms = [r for r in active if "twilio" in (r.connector_type or "").lower()]

    def judge(key: str, label: str, group: list, off_reason: str) -> dict:
        if not group:
            return _item(key, label, OFF, off_reason)
        failed = [r.name or str(r.id) for r in group if r.last_test_ok is False]
        untested = sum(1 for r in group if r.last_test_ok is None)
        if failed:
            more = f" and {len(failed) - 5} more" if len(failed) > 5 else ""
            return _item(key, label, DEGRADED,
                         f"Last test failed: {', '.join(failed[:5])}{more} — calls through "
                         f"{'it' if len(failed) == 1 else 'them'} fail.")
        tail = f" ({untested} never tested)" if untested else ""
        return _item(key, label, WORKING, f"{len(group)} set up, none failing its last test{tail}.")

    return (judge("sms", "SMS (Twilio)", sms, "No Twilio connector — SMS is not sent."),
            judge("connectors", "Connectors", active, "None set up."))


async def check_secrets(_s) -> dict:
    label = "Secrets store (OpenBao)"
    from case_service.compliance.dr_status import install_root
    if not (install_root() / "deploy/openbao/enabled").exists():
        return _item("secrets", label, OFF, "Not used — secrets are kept in .env.")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
            r = await c.get("http://127.0.0.1:8350/v1/sys/health")
        if r.status_code == 200:
            return _item("secrets", label, WORKING, "Unsealed and answering.")
        if r.status_code == 503:
            return _item("secrets", label, DEGRADED,
                         "Sealed — a restart cannot read its secrets until it is unsealed "
                         "(scripts/secrets-unseal.sh).")
        return _item("secrets", label, DEGRADED, f"Answering with HTTP {r.status_code}.")
    except Exception:  # noqa: BLE001
        return _item("secrets", label, DEGRADED,
                     "Not answering — the next restart keeps the existing .env.")


async def check_event_bus(s) -> dict:
    label = "Event stream (Kafka)"
    if not s.event_bus_enabled:
        return _item("event_bus", label, OFF, "Turned off (VELARIS_CASE_EVENT_BUS_ENABLED).")
    first = (s.event_bus_brokers or "").split(",")[0].strip()
    if first and await _tcp(first, 9092):
        return _item("event_bus", label, WORKING, "Answering.")
    return _item("event_bus", label, DEGRADED,
                 "On but not answering — events wait in the outbox and are sent when it is back.")


async def check_backup(_s) -> dict:
    label = "Backups"
    from case_service.compliance.dr_status import backup_status
    st = backup_status()
    high = [f for f in st.get("findings", []) if f.get("severity") == "high"]
    if any(f.get("code") == "no_backup_status" for f in high):
        return _item("backup", label, OFF, "No backup has run on this server (./backup-velaris.sh).")
    if high:
        return _item("backup", label, DEGRADED, high[0]["message"])
    return _item("backup", label, WORKING, "Recent and verified.")


# ── all of them ──────────────────────────────────────────────────────────────

async def _guard(key: str, label: str, coro: Awaitable) -> Any:
    try:
        return await asyncio.wait_for(coro, PROBE_TIMEOUT * 2)
    except Exception as exc:  # noqa: BLE001
        return _item(key, label, DEGRADED, f"The check itself failed ({type(exc).__name__}).")


#: What another tenant's administrator sees on a shared install: what their
#: users depend on. The install's own machinery — the alert mailbox, Temporal,
#: the cache, the event stream, OpenBao, backups (whose reason can say where the
#: backup key sits) — is the platform operator's alone.
TENANT_VISIBLE = frozenset({"database", "storage", "ai_completion", "ai_embedding", "ocr",
                            "email", "sms", "connectors", "meetings"})


async def check_all(session, user=None, *, use_cache: bool = True) -> dict:
    """Every dependency, concurrently. Cached briefly per process: a screen
    left open, or many of them, must not become a probe storm. No caller (the
    update script) or the platform operator sees everything; any other
    administrator sees TENANT_VISIBLE, with their own tenant's mailbox and
    connectors."""
    from velaris_shared.tenancy.identity import is_platform_operator
    operator = user is None or await is_platform_operator(session, user)
    now = time.monotonic()
    tenant_key = f"{getattr(user, 'tenant_id', None)}|{operator}"
    if use_cache and _cache["items"] is not None and now - _cache["at"] < CACHE_SECONDS \
            and _cache.get("tenant") == tenant_key:
        items = _cache["items"]
    else:
        from velaris_shared.config import get_settings
        s = get_settings()
        models = await _ollama_models(s)
        # The session is used by one check at a time (an AsyncSession is not
        # safe to share between concurrent tasks); the network probes overlap.
        db = await _guard("database", "Database", check_database(session))
        email = await _guard("email", "Email (outbound)", check_email(session, user))
        conn = await _guard("connectors", "Connectors", check_connectors(session, user))
        sms, connectors = conn if isinstance(conn, tuple) else (conn, conn)
        net: list[tuple[str, str, Callable[[], Awaitable]]] = [
            ("cache", "Shared cache (Valkey)", lambda: check_cache(s)),
            ("ai", "AI", lambda: check_ai(s, models)),
            ("ocr", "Text from scans (OCR)", lambda: check_ocr(s)),
            ("storage", "Document storage", lambda: check_storage(s)),
            ("temporal", "Workflow engine (Temporal)", lambda: check_temporal(s)),
            ("meetings", "Meetings (LiveKit)", lambda: check_meetings(s)),
            ("alert_email", "Operator alert email", lambda: check_alert_email(s)),
            ("secrets", "Secrets store (OpenBao)", lambda: check_secrets(s)),
            ("event_bus", "Event stream (Kafka)", lambda: check_event_bus(s)),
            ("backup", "Backups", lambda: check_backup(s)),
        ]
        if not operator:     # never probe what this caller will not be shown
            net = [n for n in net if n[0] in TENANT_VISIBLE or n[0] == "ai"]
        results = await asyncio.gather(*(_guard(k, lbl, f()) for k, lbl, f in net))
        by_key = dict(zip((k for k, _l, _f in net), results))
        ai = by_key.pop("ai")
        completion, embedding = ai if isinstance(ai, tuple) else (ai, ai)
        items = [db, by_key.get("storage"), completion, embedding, by_key.get("ocr"), email,
                 by_key.get("alert_email"), sms, connectors, by_key.get("temporal"),
                 by_key.get("cache"), by_key.get("meetings"), by_key.get("event_bus"),
                 by_key.get("secrets"), by_key.get("backup")]
        items = [i for i in items if i is not None and (operator or i["key"] in TENANT_VISIBLE)]
        _cache.update(at=now, items=items, tenant=tenant_key)
    summary = {st: sum(1 for i in items if i["state"] == st) for st in (WORKING, DEGRADED, OFF)}
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "items": items}


def summary_lines(report: dict) -> list[str]:
    """The update script's closing lines: counts, then each degraded item."""
    s = report["summary"]
    out = [f"Health: {s['working']} working, {s['degraded']} degraded, {s['off']} off"]
    out += [f"  ⚠ {i['label']}: {i['reason']}" for i in report["items"] if i["state"] == DEGRADED]
    return out


async def _main() -> int:
    from velaris_shared.db.session import get_session_factory
    async with get_session_factory()() as session:
        report = await check_all(session, None, use_cache=False)
    print("\n".join(summary_lines(report)))
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(asyncio.run(_main()))
    except Exception as exc:  # noqa: BLE001 — a summary line must never fail an update
        print(f"Health: could not be checked ({type(exc).__name__})")
        sys.exit(0)
