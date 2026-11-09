"""HxFusion scriptTask guard — Oct item 8b.

A scriptTask's ``extensions.script`` used to run through
``exec(script, {"__builtins__": {}}, ctx)``. Empty builtins is not a sandbox:
``().__class__.__base__.__subclasses__()`` reaches ``os``, so anyone who could
upload a BPMN process ran code inside case-service. Configuration became
platform code.

A script is now a list of **assignments of safe expressions**, one per line::

    total = amount * qty
    band = "high" if total > 1000 else "low"
    total += fee

Each right-hand side must be CONFORMING under ``core.safe_expression`` (no
attribute access, no dunders, whitelisted calls only) and is evaluated by its
pure-Python walker. ``exec``/``eval`` never run.

The same check runs at **upload** (create / update / hxdeploy import) and at
**run time**, so a process stored before this change cannot slip through: its
task fails with a named error instead of executing or silently passing.
``scan_definitions`` lists every stored process the check now refuses.
"""
from __future__ import annotations

import ast
from typing import Any

from velaris_shared.core.safe_expression import (
    _BINOP_FN,
    Classification,
    _eval_node,
    classify_expression,
)

MAX_STATEMENTS = 50


class ScriptRejected(ValueError):
    """The script is outside the allowed grammar; the reason names why."""


def _target_name(target: ast.AST) -> str:
    if not isinstance(target, ast.Name):
        raise ScriptRejected("only plain names may be assigned (no attributes, subscripts or unpacking)")
    if target.id.startswith("_"):
        raise ScriptRejected(f"name '{target.id}' may not start with '_'")
    return target.id


def compile_script(script: str) -> list[tuple[str, ast.expr, ast.operator | None]]:
    """Parse and check *script*. Returns (name, expression, aug_op) steps.

    Raises :class:`ScriptRejected` with a human-readable reason.
    """
    try:
        tree = ast.parse(script, mode="exec")
    except SyntaxError as exc:
        raise ScriptRejected(f"syntax error on line {exc.lineno}: {exc.msg}") from None
    if len(tree.body) > MAX_STATEMENTS:
        raise ScriptRejected(f"too many statements ({len(tree.body)} > {MAX_STATEMENTS})")

    steps: list[tuple[str, ast.expr, ast.operator | None]] = []
    for stmt in tree.body:
        line = getattr(stmt, "lineno", "?")
        try:
            if isinstance(stmt, ast.Assign):
                if len(stmt.targets) != 1:
                    raise ScriptRejected("chained assignment is not allowed")
                name, value, op = _target_name(stmt.targets[0]), stmt.value, None
            elif isinstance(stmt, ast.AugAssign):
                name, value, op = _target_name(stmt.target), stmt.value, stmt.op
            else:
                raise ScriptRejected(
                    f"'{type(stmt).__name__}' is not allowed — only 'name = expression' lines"
                )
            classification, reason = classify_expression(ast.unparse(value))
            if classification is not Classification.CONFORMING:
                raise ScriptRejected(reason or "expression outside the safe grammar")
        except ScriptRejected as exc:
            raise ScriptRejected(f"line {line}: {exc}") from None
        steps.append((name, value, op))
    return steps


def check_script(script: str) -> str | None:
    """Return the rejection reason for *script*, or None if it is allowed."""
    try:
        compile_script(script)
    except ScriptRejected as exc:
        return str(exc)
    return None


def run_script(script: str, context: dict[str, Any]) -> dict[str, Any]:
    """Run *script* against *context*; return the names it newly defined.

    Same result contract as the old ``exec`` path: only keys that were not
    already in *context* come back. Raises ScriptRejected or the evaluation
    error.
    """
    names = dict(context)
    for name, value, op in compile_script(script):
        result = _eval_node(ast.Expression(body=value), names)
        if op is not None:
            if name not in names:
                raise ScriptRejected(f"'{name}' is used before it is assigned")
            result = _BINOP_FN[type(op)](names[name], result)
        names[name] = result
    return {k: v for k, v in names.items() if k not in context}


def check_bpmn(bpmn_xml: str) -> list[dict[str, str]]:
    """Every scriptTask in *bpmn_xml* whose script is refused, with the reason.

    XML that does not parse yields no findings — whether a process parses is
    not this check's decision, and it was never refused at upload before.
    """
    from velaris_shared.hxfusion.parser import parse

    try:
        process = parse(bpmn_xml)
    except Exception:  # noqa: BLE001
        return []
    findings = []
    for node in process.nodes.values():
        if node.node_type != "scriptTask":
            continue
        script = node.extensions.get("script", "")
        if not script:
            continue
        reason = check_script(script)
        if reason:
            findings.append({"node_id": node.id, "node_name": node.name or "", "reason": reason})
    return findings


async def scan_definitions(session) -> list[dict[str, Any]]:
    """Every stored process definition with at least one refused script."""
    from sqlalchemy import select

    from velaris_shared.db.models import ProcessDefinitionModel

    rows = (await session.execute(select(ProcessDefinitionModel))).scalars().all()
    out = []
    for d in rows:
        findings = check_bpmn(d.bpmn_xml or "")
        if findings:
            out.append({
                "id": str(d.id), "name": d.name, "version": d.version,
                "status": d.status, "tenant_id": d.tenant_id, "refused": findings,
            })
    return out


if __name__ == "__main__":  # pragma: no cover — operator pre-upgrade scan
    import asyncio
    import json

    async def _main() -> None:
        from velaris_shared.db.session import get_session_factory

        async with get_session_factory()() as session:
            found = await scan_definitions(session)
        print(json.dumps(found, indent=2, default=str))
        print(f"{len(found)} process definition(s) have scripts that will now be refused.")

    asyncio.run(_main())
