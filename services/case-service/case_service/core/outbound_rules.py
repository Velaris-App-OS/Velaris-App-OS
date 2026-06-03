"""Outbound connector rule firing.

Queries OutboundConnectorRuleModel for rules matching (trigger_event, case_type_id),
evaluates the optional condition against case_data, resolves input_mapping, and
calls execute_connector() for each matching rule.

All execution is fire-and-forget (asyncio.create_task) so the case lifecycle is
never blocked waiting for an external connector to respond.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import OutboundConnectorRuleModel, ConnectorRegistryModel
from case_service.hxbridge.executor import execute_connector

logger = logging.getLogger(__name__)


def _resolve_input(mapping: dict, case_data: dict) -> dict:
    """Substitute {field_key} placeholders in mapping values from case_data."""
    result: dict = {}
    for param_key, value_expr in mapping.items():
        if isinstance(value_expr, str) and value_expr.startswith("{") and value_expr.endswith("}"):
            field_key = value_expr[1:-1]
            result[param_key] = case_data.get(field_key, "")
        else:
            result[param_key] = case_data.get(value_expr, value_expr)
    return result


def _condition_matches(condition_expr: dict | None, case_data: dict) -> bool:
    """Simple key=value condition evaluation. All pairs must match."""
    if not condition_expr:
        return True
    for key, expected in condition_expr.items():
        if case_data.get(key) != expected:
            return False
    return True


async def _fire_rule(
    rule: OutboundConnectorRuleModel,
    connector: ConnectorRegistryModel,
    input_data: dict,
    case_id: uuid.UUID,
) -> None:
    """Execute one rule's connector in a separate session to avoid contaminating the caller's session."""
    from case_service.db.session import get_session_factory
    try:
        async with get_session_factory()() as session:
            await execute_connector(
                session, connector, input_data,
                case_id=case_id,
                step_id=f"outbound_rule:{rule.id}",
            )
    except Exception as exc:
        logger.warning(
            "Outbound rule %s (%s) failed for case %s: %s",
            rule.name, rule.id, case_id, exc,
        )


async def fire_outbound_rules(
    session: AsyncSession,
    *,
    trigger_event: str,
    case_id: uuid.UUID,
    case_type_id: uuid.UUID | str | None,
    case_data: dict,
    tenant_id: str,
) -> None:
    """Query matching outbound rules and fire connectors as background tasks.

    Never raises — failures are logged and sent to DLQ by execute_connector.
    """
    try:
        stmt = select(OutboundConnectorRuleModel).where(
            OutboundConnectorRuleModel.trigger_event == trigger_event,
            OutboundConnectorRuleModel.tenant_id == tenant_id,
            OutboundConnectorRuleModel.enabled == True,  # noqa: E712
        )
        if case_type_id is not None:
            from sqlalchemy import or_
            stmt = stmt.where(
                or_(
                    OutboundConnectorRuleModel.case_type_id == uuid.UUID(str(case_type_id)),
                    OutboundConnectorRuleModel.case_type_id.is_(None),
                )
            )
        else:
            stmt = stmt.where(OutboundConnectorRuleModel.case_type_id.is_(None))

        rules = (await session.execute(stmt)).scalars().all()
        if not rules:
            return

        for rule in rules:
            if not _condition_matches(rule.condition_expr, case_data):
                continue
            if not rule.connector_id:
                continue
            connector = await session.get(ConnectorRegistryModel, rule.connector_id)
            if not connector or not connector.enabled:
                continue
            input_data = _resolve_input(rule.input_mapping or {}, case_data)
            asyncio.create_task(
                _fire_rule(rule, connector, input_data, case_id),
                name=f"outbound_rule_{rule.id}",
            )

    except Exception as exc:
        logger.warning("fire_outbound_rules failed (trigger=%s, case=%s): %s", trigger_event, case_id, exc)
