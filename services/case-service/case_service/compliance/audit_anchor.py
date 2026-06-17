"""RFC-3161 external anchoring of the audit hash chain (Group I).

The hash chain (audit_chain.py) is tamper-EVIDENT: any rewrite of
case_audit_log breaks recomputed hashes. But an attacker with full DB access
can rewrite the chain itself. Anchoring closes that hole: a timestamp
authority (TSA) signs sha256(tip_hash) with its own key and clock, so the
chain state at anchor time is provable to an external party. Rewriting
history then requires forging the TSA's signature.

No ASN.1 dependency: a TimeStampReq is a small fixed DER structure built by
hand below. The response is stored raw (audit_anchors.tsr_der) — full
cryptographic verification is an offline operation:

    openssl ts -verify -digest <sha256(tip_hash)> -in receipt.tsr \
        -CAfile tsa-cacert.pem

Online we check the TSA's PKIStatus and that our digest + nonce are echoed
inside the signed token.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.config import get_settings
from case_service.db.models import AuditAnchorModel

log = logging.getLogger(__name__)

# DER: AlgorithmIdentifier for SHA-256 — SEQUENCE { OID 2.16.840.1.101.3.4.2.1, NULL }
_SHA256_ALG_ID = bytes.fromhex("300d06096086480165030402010500")


# ── Minimal DER encoding ─────────────────────────────────────────────


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def _der_int(value: int) -> bytes:
    body = value.to_bytes((value.bit_length() + 8) // 8 or 1, "big")
    return _der(0x02, body)


def build_timestamp_request(digest: bytes, nonce: int) -> bytes:
    """DER TimeStampReq over a SHA-256 digest (RFC 3161 §2.4.1).

    certReq TRUE so the TSA embeds its signing cert — keeps the stored
    receipt independently verifiable years later, after TSA cert rotation.
    """
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes (SHA-256)")
    message_imprint = _der(0x30, _SHA256_ALG_ID + _der(0x04, digest))
    return _der(0x30, (
        _der_int(1)                 # version v1
        + message_imprint
        + _der_int(nonce)
        + _der(0x01, b"\xff")       # certReq TRUE
    ))


def parse_timestamp_response_status(tsr: bytes) -> int:
    """PKIStatus from a DER TimeStampResp: 0=granted, 1=grantedWithMods.

    TimeStampResp ::= SEQUENCE { status PKIStatusInfo, token OPTIONAL }
    PKIStatusInfo ::= SEQUENCE { status INTEGER, ... }
    Walks just far enough to reach the first INTEGER; no full ASN.1 parse.
    """
    def _read_header(buf: bytes, off: int) -> tuple[int, int, int]:
        tag = buf[off]
        first = buf[off + 1]
        if first < 0x80:
            return tag, first, off + 2
        n_bytes = first & 0x7F
        length = int.from_bytes(buf[off + 2 : off + 2 + n_bytes], "big")
        return tag, length, off + 2 + n_bytes

    tag, _, off = _read_header(tsr, 0)          # outer TimeStampResp
    if tag != 0x30:
        raise ValueError("not a DER SEQUENCE")
    tag, _, off = _read_header(tsr, off)        # PKIStatusInfo
    if tag != 0x30:
        raise ValueError("malformed PKIStatusInfo")
    tag, length, off = _read_header(tsr, off)   # status INTEGER
    if tag != 0x02:
        raise ValueError("malformed PKIStatus")
    return int.from_bytes(tsr[off : off + length], "big")


# ── Anchoring ────────────────────────────────────────────────────────


async def _latest_anchor(session: AsyncSession) -> AuditAnchorModel | None:
    q = select(AuditAnchorModel).order_by(AuditAnchorModel.anchored_at.desc()).limit(1)
    return (await session.execute(q)).scalar_one_or_none()


async def anchor_chain_tip(session: AsyncSession, force: bool = False) -> dict:
    """Timestamp the current chain tip at the configured TSA.

    Seals pending audit rows first so the receipt covers everything written
    so far. Skips (no TSA call) when the tip hasn't moved since the last
    anchor, unless force=True.
    """
    from case_service.compliance.audit_chain import seal_new_entries
    from case_service.hxbridge.security import validate_outbound_url

    settings = get_settings()
    seal = await seal_new_entries(session)
    tip_hash: str = seal["tip_hash"]
    tip_sequence: int = seal["tip_sequence"]

    if tip_sequence == 0:
        return {"anchored": False, "reason": "empty_chain"}

    last = await _latest_anchor(session)
    if last is not None and last.tip_hash == tip_hash and not force:
        return {
            "anchored": False,
            "reason": "tip_unchanged",
            "tip_sequence": tip_sequence,
            "last_anchored_at": last.anchored_at.isoformat(),
        }

    tsa_url = settings.audit_tsa_url
    await validate_outbound_url(tsa_url)

    digest = hashlib.sha256(tip_hash.encode("ascii")).digest()
    nonce = int.from_bytes(os.urandom(8), "big") | 1  # never zero
    tsq = build_timestamp_request(digest, nonce)

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            tsa_url,
            content=tsq,
            headers={
                "Content-Type": "application/timestamp-query",
                "Accept": "application/timestamp-reply",
            },
        )
    resp.raise_for_status()
    tsr = resp.content

    status = parse_timestamp_response_status(tsr)
    if status not in (0, 1):  # granted / grantedWithMods
        raise ValueError(f"TSA rejected the request (PKIStatus={status})")
    # The signed TSTInfo must echo our imprint and nonce; their DER bytes
    # appearing in the token is a cheap integrity check short of full CMS
    # verification (which stays offline via openssl ts).
    if digest not in tsr:
        raise ValueError("TSA response does not contain our message imprint")
    if _der_int(nonce) not in tsr:
        raise ValueError("TSA response does not echo our nonce")

    anchor = AuditAnchorModel(
        id=uuid.uuid4(),
        tip_sequence=tip_sequence,
        tip_hash=tip_hash,
        tsa_url=tsa_url,
        tsr_der=tsr,
        anchored_at=datetime.now(timezone.utc),
    )
    session.add(anchor)
    await session.flush()
    log.info("audit chain anchored: tip_seq=%d tsa=%s (%d-byte receipt)",
             tip_sequence, tsa_url, len(tsr))
    return {
        "anchored": True,
        "anchor_id": str(anchor.id),
        "tip_sequence": tip_sequence,
        "tip_hash": tip_hash,
        "tsa_url": tsa_url,
        "receipt_bytes": len(tsr),
        "anchored_at": anchor.anchored_at.isoformat(),
    }


async def audit_anchor_loop() -> None:
    """Long-running background task — anchors at startup and on the
    configured interval thereafter (default daily). Skips silently when the
    tip hasn't moved, so an idle platform doesn't spam the TSA."""
    import asyncio

    settings = get_settings()
    period = max(1, settings.audit_anchor_interval_hours) * 3600
    while True:
        try:
            from case_service.db.session import get_analytics_session_factory
            async with get_analytics_session_factory()() as session:
                # commit unconditionally — anchor_chain_tip also seals
                # pending audit rows, which must persist even on a skip
                await anchor_chain_tip(session)
                await session.commit()
        except Exception as exc:
            log.warning("audit_anchor_loop error: %s", exc)
        await asyncio.sleep(period)


async def list_anchors(session: AsyncSession, limit: int = 50) -> list[dict]:
    q = (
        select(AuditAnchorModel)
        .order_by(AuditAnchorModel.anchored_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": str(r.id),
            "tip_sequence": r.tip_sequence,
            "tip_hash": r.tip_hash,
            "tsa_url": r.tsa_url,
            "receipt_bytes": len(r.tsr_der or b""),
            "anchored_at": r.anchored_at.isoformat() if r.anchored_at else None,
        }
        for r in rows
    ]
