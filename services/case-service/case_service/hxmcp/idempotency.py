"""Durable idempotency for mutating MCP tool calls (P2).

Claim-first protocol, each step in its OWN session/commit (same discipline as
the HxGuard audit writer) so the claim is visible to concurrent duplicates
independent of the request transaction that performs the actual write:

  1. claim()    — INSERT a 'pending' row (UNIQUE user_id+key). A conflicting
                  key means: replay the stored 'done' response, reject a
                  mismatched request, or report an in-flight duplicate.
  2. <execute the wrapped write on the request session — it commits the write>
  3. complete() — flip the row to 'done' and store the response.
  4. release()  — on tool failure, delete the claim so a genuine retry can
                  re-run (a raised handler rolled its write back).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from case_service.db.models import MCPIdempotencyKeyModel


class IdempotencyConflict(Exception):
    """Key reused with different arguments, or a duplicate is still in-flight."""


@dataclass
class Replay:
    """A previously completed call — return its stored result verbatim."""
    response: dict
    is_error: bool


def request_hash(tool_name: str, arguments: dict) -> str:
    """Stable hash of the call, excluding the idempotency_key itself."""
    material = {k: v for k, v in arguments.items() if k != "idempotency_key"}
    canonical = json.dumps({"tool": tool_name, "args": material},
                           sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def claim(user_id: str, key: str, tool_name: str, req_hash: str) -> Replay | None:
    """Reserve the key. Returns a Replay if already completed, else None
    (caller proceeds to execute). Raises IdempotencyConflict on mismatch or
    an in-flight duplicate."""
    from case_service.db.session import get_session_factory

    async with get_session_factory()() as s:
        row = MCPIdempotencyKeyModel(
            user_id=user_id, idempotency_key=key,
            tool_name=tool_name, request_hash=req_hash, status="pending",
        )
        s.add(row)
        try:
            await s.commit()
            return None                       # claimed — go execute
        except IntegrityError:
            await s.rollback()

        existing = (await s.execute(
            select(MCPIdempotencyKeyModel).where(
                MCPIdempotencyKeyModel.user_id == user_id,
                MCPIdempotencyKeyModel.idempotency_key == key,
            )
        )).scalar_one()

        if existing.request_hash != req_hash:
            raise IdempotencyConflict(
                "idempotency_key already used with different arguments")
        if existing.status != "done":
            raise IdempotencyConflict(
                "a call with this idempotency_key is still being processed")
        return Replay(response=existing.response_json or {}, is_error=existing.is_error)


async def complete(user_id: str, key: str, response: dict, is_error: bool) -> None:
    from case_service.db.session import get_session_factory

    async with get_session_factory()() as s:
        existing = (await s.execute(
            select(MCPIdempotencyKeyModel).where(
                MCPIdempotencyKeyModel.user_id == user_id,
                MCPIdempotencyKeyModel.idempotency_key == key,
            )
        )).scalar_one_or_none()
        if existing is not None:
            existing.status = "done"
            existing.response_json = response
            existing.is_error = is_error
            await s.commit()


async def release(user_id: str, key: str) -> None:
    """Delete a claim whose write did not commit, so a retry can re-run."""
    from case_service.db.session import get_session_factory

    async with get_session_factory()() as s:
        existing = (await s.execute(
            select(MCPIdempotencyKeyModel).where(
                MCPIdempotencyKeyModel.user_id == user_id,
                MCPIdempotencyKeyModel.idempotency_key == key,
                MCPIdempotencyKeyModel.status == "pending",
            )
        )).scalar_one_or_none()
        if existing is not None:
            await s.delete(existing)
            await s.commit()
