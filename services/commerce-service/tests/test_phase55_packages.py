"""HxDeploy phase 1 — frozen promotion packages, real selection, tenant scoping.

Three defects these tests pin, all of them in the path that already shipped:

* the Promote screen's artifact selection was cosmetic — the push called
  `build_bundle()` with no selection and rebuilt the whole environment;
* nothing recorded what was approved — the bundle was built fresh at execute
  time, so anything edited in dev in between shipped too;
* `build_bundle` took a `tenant_id` and used it for the log line only, so a
  bundle carried every other tenant's rows and wrote them into the target.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseTypeModel,
    TenantModel,
    ConnectorRegistryModel,
    DeploymentPackageModel,
    DeploymentRunModel,
    PortalModel,
)
from case_service.hxdeploy import packages
from case_service.hxdeploy.packager import build_bundle, normalise_selection

from tests.conftest import client, session  # type: ignore[attr-defined]


async def _env(client: AsyncClient, name: str = "staging") -> dict:
    r = await client.post("/api/v1/deploy/environments", json={
        "name": name, "label": name.title(), "order_index": 1,
    })
    assert r.status_code == 201
    return r.json()


async def _tenant(session: AsyncSession, slug: str) -> TenantModel:
    """Tenants must be real rows: `case_types.tenant_id` is a GUID while the
    other tenant-scoped models store a slug, so the packager resolves one to the
    other and an unresolvable token means "platform-shared rows only"."""
    from sqlalchemy import select as _select
    from case_service.tenancy.identity import clear_cache

    existing = (await session.execute(
        _select(TenantModel).where(TenantModel.slug == slug))).scalar_one_or_none()
    if existing:
        return existing
    t = TenantModel(id=uuid.uuid4(), slug=slug, name=slug.title())
    session.add(t)
    await session.commit()
    clear_cache()
    return t


async def _case_type(session: AsyncSession, name: str, tenant: str = "default") -> CaseTypeModel:
    t = await _tenant(session, tenant)
    ct = CaseTypeModel(
        id=uuid.uuid4(), name=name, version="1.0.0", description="",
        definition_json={"stages": []}, tenant_id=t.id,
    )
    session.add(ct)
    await session.commit()
    return ct


# ── Selection is honoured ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_selection_narrows_the_bundle(session: AsyncSession):
    keep = await _case_type(session, "Keep")
    await _case_type(session, "Drop")

    bundle = await build_bundle(session, "default", selection={
        "sections": {"case_types": [str(keep.id)]}
    })
    names = [ct["name"] for ct in bundle["case_types"]]
    assert names == ["Keep"]


@pytest.mark.asyncio
async def test_empty_list_means_none_absent_means_all(session: AsyncSession):
    await _case_type(session, "Alpha")
    await _case_type(session, "Beta")

    none_of_them = await build_bundle(session, "default", selection={
        "sections": {"case_types": []}})
    assert none_of_them["case_types"] == []

    all_of_them = await build_bundle(session, "default", selection={"sections": {}})
    assert len(all_of_them["case_types"]) == 2


@pytest.mark.asyncio
async def test_unknown_section_is_refused(session: AsyncSession):
    with pytest.raises(ValueError) as exc:
        normalise_selection({"sections": {"case_typo": []}})
    assert "case_typo" in str(exc.value)


@pytest.mark.asyncio
async def test_dependency_closure_pulls_a_parent_the_selection_left_out(session: AsyncSession):
    """A rule that names a connector the selection excluded still imports."""
    conn = ConnectorRegistryModel(
        id=uuid.uuid4(), name="Supplier API", connector_type="rest",
        config_schema={}, config={}, credentials={}, tenant_id="default", enabled=True,
    )
    session.add(conn)
    await session.commit()

    from velaris_shared.db.models import WebhookReceiverRuleModel
    rule = WebhookReceiverRuleModel(
        id=uuid.uuid4(), name="Inbound", connector_id=conn.id,
        field_updates={}, enabled=True, tenant_id="default",
    )
    session.add(rule)
    await session.commit()

    bundle = await build_bundle(session, "default", selection={"sections": {
        "connectors": [],                      # explicitly none
        "webhook_receiver_rules": [str(rule.id)],
    }})
    # The parent came anyway — the alternative is an import with a dangling FK.
    assert [c["name"] for c in bundle["connectors"]] == ["Supplier API"]
    assert bundle["summary"]["auto_included"]["connectors"] == 1


# ── Tenant scoping ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bundle_never_carries_another_tenants_rows(session: AsyncSession):
    await _case_type(session, "Ours", tenant="default")
    await _case_type(session, "Theirs", tenant="other-tenant")
    session.add(PortalModel(
        id=uuid.uuid4(), name="Their Portal", portal_type="customer",
        modules=[], theme={}, is_active=True, tenant_id="other-tenant",
    ))
    session.add(ConnectorRegistryModel(
        id=uuid.uuid4(), name="Their Connector", connector_type="rest",
        config_schema={}, config={}, credentials={"api_key": "x"},
        tenant_id="other-tenant", enabled=True,
    ))
    await session.commit()

    bundle = await build_bundle(session, "default")

    assert [ct["name"] for ct in bundle["case_types"]] == ["Ours"]
    assert "Their Portal" not in [p["name"] for p in bundle["portals"]]
    assert "Their Connector" not in [c["name"] for c in bundle["connectors"]]
    # …and their credential requirement did not leak into ours either.
    assert all(n["name"] != "Their Connector" for n in bundle["needs_configuration"])


# ── Sealing ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_seal_freezes_and_checksums(client: AsyncClient, session: AsyncSession):
    await _case_type(session, "Sealed CT")
    r = await client.post("/api/v1/deploy/packages", json={
        "name": "release", "version": "1.0.0",
    })
    assert r.status_code == 201, r.text
    pkg = r.json()
    assert pkg["status"] == "draft" and pkg["checksum"] is None

    s = await client.post(f"/api/v1/deploy/packages/{pkg['id']}/seal")
    assert s.status_code == 200
    sealed = s.json()
    assert sealed["status"] == "sealed" and len(sealed["checksum"]) == 64

    # Sealing twice would restamp the checksum — a checksum that can move is not
    # evidence of anything.
    again = await client.post(f"/api/v1/deploy/packages/{pkg['id']}/seal")
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_sealed_package_cannot_be_rebuilt_or_deleted(client: AsyncClient, session: AsyncSession):
    r = await client.post("/api/v1/deploy/packages", json={"name": "frozen", "version": "1.0.0"})
    pid = r.json()["id"]
    await client.post(f"/api/v1/deploy/packages/{pid}/seal")

    assert (await client.post(f"/api/v1/deploy/packages/{pid}/rebuild")).status_code == 409
    assert (await client.delete(f"/api/v1/deploy/packages/{pid}")).status_code == 409


@pytest.mark.asyncio
async def test_duplicate_version_is_refused(client: AsyncClient):
    a = await client.post("/api/v1/deploy/packages", json={"name": "dup", "version": "1.0.0"})
    assert a.status_code == 201
    b = await client.post("/api/v1/deploy/packages", json={"name": "dup", "version": "1.0.0"})
    assert b.status_code == 400 and "already exists" in b.json()["detail"]


@pytest.mark.asyncio
async def test_tampering_with_a_sealed_bundle_is_detected(client: AsyncClient, session: AsyncSession):
    r = await client.post("/api/v1/deploy/packages", json={"name": "tamper", "version": "1.0.0"})
    pid = r.json()["id"]
    await client.post(f"/api/v1/deploy/packages/{pid}/seal")

    pkg = await session.get(DeploymentPackageModel, uuid.UUID(pid))
    bundle = dict(pkg.bundle)
    bundle["case_types"] = [{"id": str(uuid.uuid4()), "name": "Smuggled"}]
    pkg.bundle = bundle
    await session.commit()

    with pytest.raises(packages.PackageError) as exc:
        packages.verify_package(pkg)
    assert "modified after sealing" in str(exc.value)


# ── Promotion binds to a package ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_promote_seals_a_package_from_the_manifest(client: AsyncClient, session: AsyncSession):
    keep = await _case_type(session, "Promoted")
    await _case_type(session, "Left behind")
    env = await _env(client)

    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"],
        "package_manifest": {"case_types": [str(keep.id)], "forms": [], "version": "2.1.0"},
    })
    assert r.status_code == 201
    run = await session.get(DeploymentRunModel, uuid.UUID(r.json()["id"]))
    assert run.package_id is not None, "the run must name the bytes it will ship"
    assert run.package_checksum

    pkg = await session.get(DeploymentPackageModel, run.package_id)
    assert pkg.status == "sealed"
    assert [ct["name"] for ct in pkg.bundle["case_types"]] == ["Promoted"]


@pytest.mark.asyncio
async def test_promote_with_no_selection_still_means_everything(client: AsyncClient, session: AsyncSession):
    await _case_type(session, "One")
    await _case_type(session, "Two")
    env = await _env(client)

    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"], "package_manifest": {"case_types": [], "forms": []},
    })
    run = await session.get(DeploymentRunModel, uuid.UUID(r.json()["id"]))
    pkg = await session.get(DeploymentPackageModel, run.package_id)
    assert len(pkg.bundle["case_types"]) == 2


@pytest.mark.asyncio
async def test_promote_refuses_an_unsealed_package(client: AsyncClient):
    env = await _env(client)
    p = await client.post("/api/v1/deploy/packages", json={"name": "draft-only", "version": "1.0.0"})
    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"], "package_id": p.json()["id"], "package_manifest": {},
    })
    assert r.status_code == 400 and "not sealed" in r.json()["detail"]


@pytest.mark.asyncio
async def test_promote_refuses_another_tenants_package(client: AsyncClient, session: AsyncSession):
    env = await _env(client)
    theirs = DeploymentPackageModel(
        id=uuid.uuid4(), tenant_id="other-tenant", name="theirs", version="1.0.0",
        selection={}, bundle={"case_types": []}, summary={}, status="sealed",
        checksum=packages.checksum_of({"case_types": []}), created_by="them",
    )
    session.add(theirs)
    await session.commit()

    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"], "package_id": str(theirs.id), "package_manifest": {},
    })
    assert r.status_code == 400 and r.json()["detail"] == "Package not found"


@pytest.mark.asyncio
async def test_another_tenants_package_is_absent_not_forbidden(client: AsyncClient, session: AsyncSession):
    theirs = DeploymentPackageModel(
        id=uuid.uuid4(), tenant_id="other-tenant", name="hidden", version="1.0.0",
        selection={}, bundle={}, summary={}, status="draft", created_by="them",
    )
    session.add(theirs)
    await session.commit()

    assert (await client.get(f"/api/v1/deploy/packages/{theirs.id}")).status_code == 404
    listed = await client.get("/api/v1/deploy/packages")
    assert all(p["name"] != "hidden" for p in listed.json())


# ── The wire ─────────────────────────────────────────────────────────────────

def test_checksum_ignores_transport_fields():
    """The push stamps run id and source env onto the body in flight. Hashing
    those would make every push fail its own integrity check."""
    bundle = {"case_types": [{"name": "A"}], "summary": {}}
    before = packages.checksum_of(bundle)
    in_flight = dict(bundle, deployment_run_id="r1", source_env="dev",
                     package_id="p1", package_checksum=before)
    assert packages.checksum_of(in_flight) == before


def test_checksum_is_order_independent():
    a = {"x": 1, "y": [1, 2]}
    b = {"y": [1, 2], "x": 1}
    assert packages.checksum_of(a) == packages.checksum_of(b)


@pytest.mark.asyncio
async def test_import_refuses_a_bundle_that_fails_its_checksum(client: AsyncClient, session: AsyncSession):
    from velaris_shared.db.models import EnvironmentRegistryModel
    env = EnvironmentRegistryModel(
        id=uuid.uuid4(), tenant_id="default", name="target", label="Target",
        order_index=1, import_api_key="k-integrity",
    )
    session.add(env)
    await session.commit()

    bundle = {"bundle_schema_version": "2", "case_types": [], "needs_configuration": []}
    good = packages.checksum_of(bundle)

    ok = await client.post("/api/v1/deploy/import",
                           json=dict(bundle, package_checksum=good),
                           headers={"Authorization": "Bearer k-integrity"})
    assert ok.status_code == 200

    tampered = dict(bundle, case_types=[{"id": str(uuid.uuid4()), "name": "Smuggled",
                                         "version": "1.0.0", "definition_json": {}}])
    bad = await client.post("/api/v1/deploy/import",
                            json=dict(tampered, package_checksum=good),
                            headers={"Authorization": "Bearer k-integrity"})
    assert bad.status_code == 400 and "Checksum mismatch" in bad.json()["detail"]


@pytest.mark.asyncio
async def test_push_ships_the_sealed_bytes_not_a_rebuild(client: AsyncClient, session: AsyncSession):
    """The whole point of sealing: what ships is what was approved, even if dev
    has moved on since."""
    from velaris_shared.db.models import EnvironmentRegistryModel
    from case_service.hxdeploy import service as deploy_service

    await _case_type(session, "At approval time")
    env = EnvironmentRegistryModel(
        id=uuid.uuid4(), tenant_id="default", name="pushed", label="Pushed",
        # http is allowed to loopback only — the push carries the import key.
        order_index=1, url="http://127.0.0.1:9", import_api_key="k",
        delivery_method="push",
    )
    session.add(env)
    await session.commit()

    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": str(env.id), "package_manifest": {"case_types": [], "forms": []},
    })
    run = await session.get(DeploymentRunModel, uuid.UUID(r.json()["id"]))

    # dev moves on after approval
    await _case_type(session, "Added after approval")

    sent: dict = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): return None
        def json(self): return {"imported": 1, "skipped": 0, "errors": [], "needs_configuration": []}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent.update(json or {})
            return _Resp()

    original = deploy_service.httpx.AsyncClient
    deploy_service.httpx.AsyncClient = _Client
    try:
        await deploy_service._push_bundle(session, run, env)
    finally:
        deploy_service.httpx.AsyncClient = original

    names = [ct["name"] for ct in sent["case_types"]]
    assert "At approval time" in names
    assert "Added after approval" not in names, "the push rebuilt from live dev"
    assert sent["package_checksum"] == run.package_checksum


# ── Phase 2: the promotion path is privileged ────────────────────────────────

def _headers_for(roles: list[str]) -> dict:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username=f"test-{'-'.join(roles) or 'none'}",
        roles=roles, secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
    )
    return {"Authorization": f"Bearer {token}"}


def _deploy_routes():
    """Every /deploy and /apps route, with the action gating it.

    Read from the BUILT route table rather than a hand-kept list: a route added
    without a gate has to show up here, and absence has no spelling in source."""
    from fastapi.routing import APIRoute
    from case_service.main import app

    out = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        if not (r.path.startswith("/api/v1/deploy") or r.path.startswith("/api/v1/apps")):
            continue
        actions = [getattr(d.call, "hxguard_action", None) for d in r.dependant.dependencies]
        out.append((r.path, sorted(r.methods)[0], [a for a in actions if a]))
    return out


#: The two machine-to-machine endpoints. They carry their own credential — an
#: environment's import key, and an HMAC signature — and a JWT gate would refuse
#: the very caller they exist for.
M2M = {"/api/v1/deploy/import", "/api/v1/deploy/runs/{run_id}/callback"}


def test_every_promotion_route_names_an_action():
    ungated = [(p, m) for p, m, a in _deploy_routes() if not a and p not in M2M]
    assert not ungated, (
        f"{len(ungated)} promotion route(s) with no privilege gate: {ungated}. "
        f"Before 2026-08-21 this was all 34 of them."
    )


@pytest.mark.asyncio
async def test_a_viewer_cannot_reach_any_of_them(client: AsyncClient):
    """A viewer holds no deploy action, so every route refuses — including the
    reads. Before phase 2 a viewer could register an environment, promote to
    production and approve the run."""
    viewer = _headers_for(["viewer"])
    checked = 0
    for path, method, actions in _deploy_routes():
        if path in M2M or not actions:
            continue
        url = (path.replace("{env_id}", str(uuid.uuid4()))
                   .replace("{run_id}", str(uuid.uuid4()))
                   .replace("{window_id}", str(uuid.uuid4()))
                   .replace("{package_id}", str(uuid.uuid4()))
                   .replace("{other_id}", str(uuid.uuid4()))
                   .replace("{case_type_id}", str(uuid.uuid4()))
                   .replace("{case_type_name}", "Anything")
                   .replace("{environment}", "prod"))
        r = await client.request(method, url, headers=viewer,
                                 json={} if method in ("POST", "PATCH", "PUT") else None)
        assert r.status_code == 403, f"{method} {url} answered {r.status_code}, not 403"
        checked += 1
    assert checked >= 30, f"only {checked} routes exercised"


@pytest.mark.asyncio
async def test_a_developer_may_build_but_not_seal_or_promote(client: AsyncClient, session: AsyncSession):
    """Sealing declares what ships and promoting releases it. Building a package
    to look at it is ordinary design work; the other two are not."""
    dev = _headers_for(["developer"])
    r = await client.post("/api/v1/deploy/packages", headers=dev,
                          json={"name": "dev-built", "version": "0.1.0"})
    assert r.status_code == 201
    pid = r.json()["id"]

    assert (await client.post(f"/api/v1/deploy/packages/{pid}/seal", headers=dev)).status_code == 403

    env = await _env(client, "prod")
    promote = await client.post("/api/v1/deploy/promote", headers=dev, json={
        "to_env_id": env["id"], "package_manifest": {},
    })
    assert promote.status_code == 403


# ── Phase 3: bots and schedules travel, and arrive inert ─────────────────────

async def _bot(session: AsyncSession, name: str, tenant: str = "default",
               status: str = "active", **over):
    from velaris_shared.db.models import BotDefinitionModel
    fields = dict(
        id=uuid.uuid4(), tenant_id=tenant, name=name, description="",
        image="docker.io/library/python:3.12-alpine",
        digest="sha256:" + "a" * 64,
        declared_command=["python", "-c", "print(1)"], declared_env={},
        credential_refs=[], input_schema={}, output_schema={}, granted_domains=[],
        timeout_seconds=300, max_concurrency=1, resource_profile="light",
        status=status, created_by="author",
        approved_by="reviewer" if status == "active" else None,
    )
    fields.update(over)
    b = BotDefinitionModel(**fields)
    session.add(b)
    await session.commit()
    return b


async def _schedule(session: AsyncSession, name: str, bot_id, tenant: str = "default",
                    status: str = "active"):
    from velaris_shared.db.models import ScheduleModel
    sc = ScheduleModel(
        id=uuid.uuid4(), tenant_id=tenant, name=name, description="",
        cron="0 2 * * *", timezone="UTC", action_kind="bot",
        action_config={"bot_id": str(bot_id), "input": {}},
        status=status, created_by="author",
        approved_by="reviewer" if status == "active" else None,
    )
    session.add(sc)
    await session.commit()
    return sc


def _registry_allows_docker_io(monkeypatch=None):
    """The target's allowlist decides. The test DB has no platform config, so
    point it at docker.io for the duration."""
    from velaris_shared.config import get_settings
    s = get_settings()
    original = s.marketplace_l2_registries
    s.marketplace_l2_registries = "docker.io"
    return original


@pytest.mark.asyncio
async def test_bots_and_schedules_travel_in_the_bundle(session: AsyncSession):
    b = await _bot(session, "Nightly Sync")
    await _schedule(session, "Nightly", b.id)

    bundle = await build_bundle(session, "default")
    assert [x["name"] for x in bundle["bot_definitions"]] == ["Nightly Sync"]
    assert [x["name"] for x in bundle["schedules"]] == ["Nightly"]
    # Approval is not part of what a bot IS — it is a fact about one environment.
    assert "status" not in bundle["bot_definitions"][0]
    assert "approved_by" not in bundle["bot_definitions"][0]
    assert "next_run_at" not in bundle["schedules"][0]
    assert bundle["bundle_schema_version"] == "3"


@pytest.mark.asyncio
async def test_a_schedule_pulls_in_the_bot_it_fires(session: AsyncSession):
    b = await _bot(session, "Pulled Bot")
    sc = await _schedule(session, "Fires It", b.id)

    bundle = await build_bundle(session, "default", selection={"sections": {
        "schedules": [str(sc.id)], "bot_definitions": [],
    }})
    assert [x["name"] for x in bundle["bot_definitions"]] == ["Pulled Bot"]
    assert bundle["summary"]["auto_included"]["bot_definitions"] == 1


@pytest.mark.asyncio
async def test_bot_credentials_never_travel_but_their_names_do(session: AsyncSession):
    await _bot(session, "Needs Creds", credential_refs=["SUPPLIER_PORTAL"],
               granted_domains=["supplier.example.com"])
    bundle = await build_bundle(session, "default")
    entry = bundle["bot_definitions"][0]
    assert entry["credential_refs"] == ["SUPPLIER_PORTAL"]
    assert not any("password" in str(v).lower() for v in entry.values())
    nc = [n for n in bundle["needs_configuration"] if n["type"] == "bot_credential"]
    assert nc and nc[0]["missing"] == ["SUPPLIER_PORTAL"]


@pytest.mark.asyncio
async def test_an_imported_bot_is_a_draft_nobody_approved(session: AsyncSession):
    """The import key is a machine credential. It must not be able to start a
    container here because a container was approved somewhere else."""
    from velaris_shared.db.models import BotDefinitionModel
    from case_service.hxdeploy.packager import apply_bundle
    from sqlalchemy import select as _select

    b = await _bot(session, "Approved Over There")
    bundle = await build_bundle(session, "default")
    # a different environment
    await session.execute(_select(BotDefinitionModel))
    await session.delete(await session.get(BotDefinitionModel, b.id))
    await session.commit()

    original = _registry_allows_docker_io()
    try:
        result = await apply_bundle(session, bundle, "default", imported_by="import:prod")
    finally:
        from velaris_shared.config import get_settings
        get_settings().marketplace_l2_registries = original
    await session.commit()

    row = (await session.execute(_select(BotDefinitionModel))).scalars().one()
    assert row.status == "draft"
    assert row.approved_by is None and row.approved_at is None
    assert row.created_by == "import:prod"
    assert any(n["type"] == "bot_definition" and n["missing"] == ["approval"]
               for n in result["needs_configuration"])


@pytest.mark.asyncio
async def test_importing_over_an_active_bot_revokes_its_approval(session: AsyncSession):
    """An import that changes the image is an edit, and an edit returns a bot to
    draft — the same rule `PUT /bots/{id}` carries, for the same reason."""
    from velaris_shared.db.models import BotDefinitionModel
    from case_service.hxdeploy.packager import apply_bundle

    live = await _bot(session, "Live Bot")
    bundle = await build_bundle(session, "default")
    bundle["bot_definitions"][0]["image"] = "docker.io/library/alpine"

    original = _registry_allows_docker_io()
    try:
        await apply_bundle(session, bundle, "default", imported_by="import:prod")
    finally:
        from velaris_shared.config import get_settings
        get_settings().marketplace_l2_registries = original
    await session.commit()
    await session.refresh(live)

    assert live.status == "draft"
    assert live.approved_by is None
    assert live.last_edited_by == "import:prod"


@pytest.mark.asyncio
async def test_a_no_op_import_does_not_de_approve_a_live_bot(session: AsyncSession):
    """Re-promoting an unchanged package is normal. It must be harmless, or the
    importer becomes a denial of service on every bot it has ever touched."""
    from case_service.hxdeploy.packager import apply_bundle

    live = await _bot(session, "Untouched")
    bundle = await build_bundle(session, "default")

    original = _registry_allows_docker_io()
    try:
        await apply_bundle(session, bundle, "default", imported_by="import:prod")
    finally:
        from velaris_shared.config import get_settings
        get_settings().marketplace_l2_registries = original
    await session.commit()
    await session.refresh(live)

    assert live.status == "active"
    assert live.approved_by == "reviewer"


@pytest.mark.asyncio
async def test_the_targets_registry_allowlist_decides(session: AsyncSession):
    """Dev may allow a registry production does not. The refusal belongs at
    import, not at the first run when nobody is watching."""
    from velaris_shared.config import get_settings
    from velaris_shared.db.models import BotDefinitionModel
    from case_service.hxdeploy.packager import apply_bundle
    from sqlalchemy import select as _select

    b = await _bot(session, "Wrong Registry", image="ghcr.io/acme/bot")
    bundle = await build_bundle(session, "default")
    await session.delete(await session.get(BotDefinitionModel, b.id))
    await session.commit()

    s = get_settings()
    original = s.marketplace_l2_registries
    s.marketplace_l2_registries = "docker.io"          # ghcr.io is not allowed here
    try:
        result = await apply_bundle(session, bundle, "default", imported_by="import:prod")
    finally:
        s.marketplace_l2_registries = original
    await session.commit()

    assert (await session.execute(_select(BotDefinitionModel))).scalars().all() == []
    assert any("ghcr.io" in e for e in result["errors"]), result["errors"]


@pytest.mark.asyncio
async def test_an_imported_schedule_is_draft_and_fires_nothing(session: AsyncSession):
    from velaris_shared.db.models import BotDefinitionModel, ScheduleModel
    from case_service.hxdeploy.packager import apply_bundle
    from sqlalchemy import select as _select

    b = await _bot(session, "Sched Bot")
    sc = await _schedule(session, "Two AM", b.id)
    bundle = await build_bundle(session, "default")
    await session.delete(await session.get(ScheduleModel, sc.id))
    await session.delete(await session.get(BotDefinitionModel, b.id))
    await session.commit()

    original = _registry_allows_docker_io()
    try:
        await apply_bundle(session, bundle, "default", imported_by="import:prod")
    finally:
        from velaris_shared.config import get_settings
        get_settings().marketplace_l2_registries = original
    await session.commit()

    row = (await session.execute(_select(ScheduleModel))).scalars().one()
    assert row.status == "draft"
    assert row.next_run_at is None
    assert row.approved_by is None
    assert row.created_by == "import:prod"


@pytest.mark.asyncio
async def test_a_schedule_whose_bot_is_missing_is_refused_not_stored(session: AsyncSession):
    """A dangling reference must fail the import with a reason, never become a
    schedule that errors every night."""
    from velaris_shared.db.models import BotDefinitionModel, ScheduleModel
    from case_service.hxdeploy.packager import apply_bundle
    from sqlalchemy import select as _select

    b = await _bot(session, "Vanishing Bot")
    sc = await _schedule(session, "Orphan", b.id)
    bundle = await build_bundle(session, "default")
    bundle["bot_definitions"] = []                      # the bot does not travel
    await session.delete(await session.get(ScheduleModel, sc.id))
    await session.delete(await session.get(BotDefinitionModel, b.id))
    await session.commit()

    result = await apply_bundle(session, bundle, "default", imported_by="import:prod")
    await session.commit()

    assert (await session.execute(_select(ScheduleModel))).scalars().all() == []
    assert any("Orphan" in e for e in result["errors"]), result["errors"]


@pytest.mark.asyncio
async def test_a_marketplace_pack_may_not_ship_a_bot(session: AsyncSession):
    """Different trust question: HxDeploy moves artifacts between two
    environments one operator owns; a pack is a stranger's."""
    import io, json, zipfile
    from commerce_service.marketplace.packs import PackError, read_pack

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack.json", json.dumps({"name": "trojan"}))
        zf.writestr("case_types.json", json.dumps([]))
        zf.writestr("bot_definitions.json", json.dumps([{"name": "miner"}]))
    with pytest.raises(PackError) as exc:
        read_pack(buf.getvalue())
    assert "bot_definitions" in str(exc.value)


@pytest.mark.asyncio
async def test_pushing_over_plain_http_to_a_remote_host_is_refused(session: AsyncSession):
    """The push carries the target's import key in an Authorization header, and
    that key authorises writing container images into that environment."""
    from velaris_shared.db.models import EnvironmentRegistryModel
    from case_service.hxdeploy import service as deploy_service

    env = EnvironmentRegistryModel(
        id=uuid.uuid4(), tenant_id="default", name="cleartext", label="Cleartext",
        order_index=1, url="http://prod.example.com", import_api_key="k",
        delivery_method="push",
    )
    with pytest.raises(ValueError) as exc:
        deploy_service._assert_transport_safe(env.url)
    assert "clear text" in str(exc.value)

    # loopback and private networks stay usable, and https always is
    for ok in ("http://127.0.0.1:8306", "http://localhost:9", "http://10.1.2.3",
               "http://192.168.0.9:8200", "https://prod.example.com"):
        deploy_service._assert_transport_safe(ok)


@pytest.mark.asyncio
async def test_health_check_cannot_be_pointed_at_an_arbitrary_host(client: AsyncClient, session: AsyncSession):
    """It used to fetch whatever URL the caller passed and report the status
    code back — a server-side request forgery available to anyone who could read
    a run."""
    from velaris_shared.db.models import EnvironmentRegistryModel

    env = EnvironmentRegistryModel(
        id=uuid.uuid4(), tenant_id="default", name="probe-target", label="Probe",
        order_index=1, url="http://127.0.0.1:8306",
    )
    session.add(env)
    await session.commit()

    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": str(env.id), "package_manifest": {},
    })
    run_id = r.json()["id"]

    bad = await client.post(
        f"/api/v1/deploy/runs/{run_id}/health-check?check_url=http://169.254.169.254/latest/meta-data/")
    assert bad.status_code == 400
    assert "may only probe the environment" in bad.json()["detail"]


@pytest.mark.asyncio
async def test_an_imported_bot_must_still_satisfy_the_bot_rules(session: AsyncSession):
    """A bundle is an input like any other. A bot holding credentials and
    wildcard egress is an exfiltration tool, and the receiving environment says
    so rather than storing it for somebody to trip over at approval."""
    from velaris_shared.db.models import BotDefinitionModel
    from case_service.hxdeploy.packager import apply_bundle
    from sqlalchemy import select as _select

    b = await _bot(session, "Exfiltrator", credential_refs=["SUPPLIER"],
                   granted_domains=["*"])
    bundle = await build_bundle(session, "default")
    await session.delete(await session.get(BotDefinitionModel, b.id))
    await session.commit()

    original = _registry_allows_docker_io()
    try:
        result = await apply_bundle(session, bundle, "default", imported_by="import:prod")
    finally:
        from velaris_shared.config import get_settings
        get_settings().marketplace_l2_registries = original
    await session.commit()

    assert (await session.execute(_select(BotDefinitionModel))).scalars().all() == []
    assert any("wildcard egress" in e for e in result["errors"]), result["errors"]


@pytest.mark.asyncio
async def test_by_id_paths_do_not_cross_tenants(client: AsyncClient, session: AsyncSession):
    """Loading a row by primary key and acting on it is how ownership gets
    forgotten. Every by-id path answers 404 for another tenant's row."""
    from velaris_shared.db.models import (
        DeploymentRunModel, DeploymentWindowModel, EnvironmentRegistryModel,
    )

    env = EnvironmentRegistryModel(
        id=uuid.uuid4(), tenant_id="other-tenant", name="theirs", label="Theirs",
        order_index=1, url="https://theirs.example.com", import_api_key="their-key",
        delivery_method="push",
    )
    run = DeploymentRunModel(
        id=uuid.uuid4(), tenant_id="other-tenant", to_env_id=env.id,
        risk_level="low", risk_summary={}, status="awaiting_approval",
        initiated_by="them",
    )
    win = DeploymentWindowModel(
        id=uuid.uuid4(), tenant_id="other-tenant", env_id=env.id, name="Theirs",
        days_of_week=[0], start_hour_utc=0, end_hour_utc=23, enabled=True,
    )
    # The env first: nothing relates the three models, so the unit of work may
    # order them either way, and PostgreSQL enforces the runs' FK (SQLite does not).
    session.add(env)
    await session.flush()
    session.add_all([run, win])
    await session.commit()

    cases = [
        ("PATCH",  f"/api/v1/deploy/environments/{env.id}", {"name": "x", "label": "x", "order_index": 1}),
        ("DELETE", f"/api/v1/deploy/environments/{env.id}", None),
        ("GET",    f"/api/v1/deploy/environments/{env.id}/status", None),
        ("GET",    f"/api/v1/deploy/runs/{run.id}", None),
        ("POST",   f"/api/v1/deploy/runs/{run.id}/approve", {}),
        ("POST",   f"/api/v1/deploy/runs/{run.id}/reject", {"reason": "no"}),
        ("POST",   f"/api/v1/deploy/runs/{run.id}/health-check", None),
        ("PATCH",  f"/api/v1/deploy/runs/{run.id}", {"deploy_notes": "x"}),
        ("DELETE", f"/api/v1/deploy/runs/{run.id}", None),
        ("PATCH",  f"/api/v1/deploy/windows/{win.id}",
         {"env_id": str(env.id), "name": "x", "days_of_week": [0],
          "start_hour_utc": 0, "end_hour_utc": 23, "enabled": True}),
        ("DELETE", f"/api/v1/deploy/windows/{win.id}", None),
        ("POST",   f"/api/v1/deploy/package/delta?env_id={env.id}", {}),
    ]
    for method, url, body in cases:
        r = await client.request(method, url, json=body)
        assert r.status_code == 404, f"{method} {url} answered {r.status_code}"

    # …and their environment cannot be used as a promotion target either.
    promote = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": str(env.id), "package_manifest": {},
    })
    assert promote.status_code == 400
    assert promote.json()["detail"] == "Target environment not found"
