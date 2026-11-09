"""Oct 8b — HxFusion scriptTask no longer runs arbitrary Python."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import client  # type: ignore[attr-defined]  # noqa: F401

from case_service.hxfusion.engine import _run_script_task
from case_service.hxfusion.parser import BpmnNode
from case_service.hxfusion.script_guard import check_bpmn, check_script, run_script

# The escape that worked against exec(script, {"__builtins__": {}}, ctx).
ESCAPE = (
    "w=[c for c in ().__class__.__base__.__subclasses__() if c.__name__=='_wrap_close'][0]\n"
    "r=w.__init__.__globals__['popen']('id').read()"
)


def _bpmn(script: str) -> str:
    esc = script.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace("\n", "&#10;")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
             xmlns:camunda="http://camunda.org/schema/1.0/bpmn">
  <process id="S1" name="Script Process">
    <startEvent id="start" />
    <sequenceFlow id="f1" sourceRef="start" targetRef="calc" />
    <scriptTask id="calc" name="Calc">
      <extensionElements><camunda:properties>
        <camunda:property name="script" value="{esc}" />
      </camunda:properties></extensionElements>
    </scriptTask>
    <sequenceFlow id="f2" sourceRef="calc" targetRef="end" />
    <endEvent id="end" />
  </process>
</definitions>"""


def _node(script: str) -> BpmnNode:
    return BpmnNode(id="calc", node_type="scriptTask", name="Calc", extensions={"script": script})


# ── The escape is refused and never runs ──────────────────────────

def test_escape_refused_at_run_time():
    result, error = _run_script_task(_node(ESCAPE), {})
    assert result is None
    assert error and error.startswith("Script refused by the safe-script check")


@pytest.mark.parametrize("script", [
    ESCAPE,
    "x = ().__class__",
    "x = __import__('os')",
    "import os",
    "x = open('/etc/passwd')",
    "exec('1')",
    "x = [1][0]",                 # subscript
    "x = (lambda: 1)()",
    "a.b = 1",
    "a, b = 1, 2",
    "_private = 1",
    "for i in range(3): x = i",
    "x = y = 1",
])
def test_refused(script):
    assert check_script(script) is not None


# ── Legitimate scripts still work ─────────────────────────────────

def test_assignments_run():
    out = run_script(
        "total = amount * qty\nband = 'high' if total > 100 else 'low'\ntotal += 5",
        {"amount": 30, "qty": 4},
    )
    assert out == {"total": 125, "band": "high"}


def test_result_contract_matches_old_exec_path():
    # A reassigned context key is not returned — same as before.
    assert run_script("amount = amount + 1\nnew = amount", {"amount": 1}) == {"new": 2}
    result, error = _run_script_task(_node("x = max(a, 3)"), {"a": 7})
    assert (result, error) == ({"x": 7}, None)


def test_empty_script_unchanged():
    assert _run_script_task(_node(""), {}) == (None, None)


def test_runtime_error_is_named():
    result, error = _run_script_task(_node("x = missing + 1"), {})
    assert result is None and error.startswith("Script error")


# ── Upload refuses, validate reports, scan lists ──────────────────

def test_check_bpmn_names_the_node():
    refused = check_bpmn(_bpmn(ESCAPE))
    assert [r["node_id"] for r in refused] == ["calc"]
    assert check_bpmn(_bpmn("x = 1")) == []
    assert check_bpmn("<not-xml") == []   # unparsable is not this check's call


@pytest.mark.asyncio
async def test_create_refuses_unsafe_script(client: AsyncClient):
    resp = await client.post("/api/v1/fusion/definitions",
                             json={"name": "Evil", "bpmn_xml": _bpmn(ESCAPE)})
    assert resp.status_code == 422
    assert resp.json()["detail"]["refused"][0]["node_id"] == "calc"

    ok = await client.post("/api/v1/fusion/definitions",
                           json={"name": "Good", "bpmn_xml": _bpmn("x = 1")})
    assert ok.status_code == 201


@pytest.mark.asyncio
async def test_update_refuses_unsafe_script(client: AsyncClient):
    cr = await client.post("/api/v1/fusion/definitions",
                           json={"name": "Upd", "bpmn_xml": _bpmn("x = 1")})
    defn_id = cr.json()["id"]
    resp = await client.patch(f"/api/v1/fusion/definitions/{defn_id}",
                              json={"bpmn_xml": _bpmn(ESCAPE)})
    assert resp.status_code == 422
    # A patch that does not touch bpmn_xml is unaffected.
    assert (await client.patch(f"/api/v1/fusion/definitions/{defn_id}",
                               json={"status": "inactive"})).status_code == 200


@pytest.mark.asyncio
async def test_validate_reports_refused_scripts(client: AsyncClient):
    resp = await client.post("/api/v1/fusion/definitions/validate",
                             json={"bpmn_xml": _bpmn(ESCAPE)})
    assert resp.json()["refused_scripts"][0]["node_id"] == "calc"


@pytest.mark.asyncio
async def test_scan_lists_stored_unsafe_definitions(client: AsyncClient):
    # Stored before the check existed: write straight to the table.
    from velaris_shared.db.models import ProcessDefinitionModel
    from velaris_shared.db.session import get_session_factory

    async with get_session_factory()() as session:
        session.add(ProcessDefinitionModel(name="Legacy", version=1, bpmn_xml=_bpmn(ESCAPE)))
        await session.commit()

    resp = await client.get("/api/v1/fusion/definitions/script-scan")
    assert resp.status_code == 200
    names = [d["name"] for d in resp.json()["definitions"]]
    assert "Legacy" in names


# ── Resource bounds: a safe grammar can still stall the event loop ─

@pytest.mark.parametrize("script", [
    "x = 9 ** 9 ** 9",
    "x = 2 ** 100000",
    "x = 'a' * 10000000",
    "s = 'aaaaaaaaaa'\n" + "s = s + s\n" * 30,
    "n = 2 ** 60000\n" + "n = n * n\n" * 10,
])
def test_resource_bombs_fail_fast(script):
    import time
    t = time.monotonic()
    result, error = _run_script_task(_node(script), {})
    assert result is None and error.startswith("Script error")
    assert time.monotonic() - t < 1.0


def test_ordinary_arithmetic_unaffected():
    assert run_script("x = 2 ** 10\ny = 'ab' * 3\nz = 1.5 ** 2", {}) == {"x": 1024, "y": "ababab", "z": 2.25}


# ── Marketplace packs are refused whole ───────────────────────────

def test_pack_with_unsafe_script_refused():
    from commerce_service.marketplace.packs import PackError, _validate_expressions

    with pytest.raises(PackError, match="safe-script"):
        _validate_expressions({"process_definitions": [{"name": "Evil", "bpmn_xml": _bpmn(ESCAPE)}]})
    _validate_expressions({"process_definitions": [{"name": "Good", "bpmn_xml": _bpmn("x = 1")}]})


@pytest.mark.asyncio
async def test_hxdeploy_import_skips_unsafe_process():
    from case_service.hxdeploy.packager import apply_bundle
    from velaris_shared.db.session import get_session_factory

    bundle = {"process_definitions": [
        {"name": "EvilImport", "version": 1, "bpmn_xml": _bpmn(ESCAPE)},
    ]}
    async with get_session_factory()() as session:
        result = await apply_bundle(session, bundle, "default")
    assert any("unsafe script refused" in e for e in result.get("errors", [])), result
