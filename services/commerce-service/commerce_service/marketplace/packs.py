"""Accelerator Packs P1 — vertical content packs.

A pack is a whole working process for a domain (claims, HR onboarding, KYC …)
shipped as marketplace CONTENT: case types, stages, steps, forms, rules, SLAs,
and optionally some sample cases. A client installs one and has a running
process in minutes instead of authoring from scratch.

It carries no code and introduces no runtime. Provisioning delegates to
`hxdeploy.packager.apply_bundle` — the same importer that promotes artifacts
between customer environments — so a pack can never do more than a promotion
bundle can, and there is exactly one import path to reason about and secure.

Install and provision are separate on purpose:
  · install  — the tenant is entitled to this content (marketplace, gated,
               conformance-checked);
  · provision — the content actually appears in their Studio.

P1 provisions ONCE. `apply_bundle` upserts, so re-provisioning would overwrite
whatever the tenant edited afterwards; that is exactly what P2's delta/merge
exists to prevent. Blocking is reversible, an overwritten workflow is not.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PACK_TYPE = "accelerator_pack"

# Sections a pack may carry. Deliberately a SUBSET of what apply_bundle accepts:
# a content pack has no business shipping email accounts or connector
# credentials, and silently importing those from the marketplace would turn a
# content install into a credentials install.
PACK_SECTIONS = (
    "data_models", "business_calendars", "access_roles", "access_groups",
    "form_definitions", "rule_definitions", "process_definitions",
    "case_types", "stages", "steps", "escalation_trees", "portals",
)

# Sections that are refused outright if a pack tries to ship them, with the
# reason the operator sees. These are the credential- and egress-bearing ones.
FORBIDDEN_SECTIONS = {
    "connectors": "connectors carry credentials and outbound endpoints",
    "email_accounts": "email accounts carry SMTP credentials",
    "webhook_subscriptions": "webhook subscriptions define outbound egress",
    "outbound_connector_rules": "outbound rules define egress",
    "webhook_receiver_rules": "receiver rules expose an inbound surface",
    # A bot is a container image, a command and an egress allowlist; a schedule
    # is an unattended action on a timer. Both are exactly the class this list
    # exists to keep out of a marketplace install — HxDeploy promotes them
    # between two environments one operator owns, which is a different trust
    # question from installing a stranger's pack.
    "bot_definitions": "bot definitions name a container image and its egress",
    "schedules": "schedules run unattended actions on a timer",
}

_MAX_SECTION_BYTES = 8 * 1024 * 1024
#: Oct item 5 — a pack's rep-desk rails. Not a bundle section: apply_bundle
#: never sees it; `engage.rail_config` validates and stores it.
RAILS_FILE = "engage_rails.json"
_MAX_SAMPLE_CASES = 200


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


class PackError(Exception):
    """Raised when a pack is malformed or asks for something it may not have."""


def read_pack(hxapp_bytes: bytes) -> dict[str, Any]:
    """Parse a `.hxapp` into {meta, bundle}.

    Section files are the same per-section layout `apps/packager.py` writes and
    the conformance gate already reads, so a pack is an ordinary bundle plus a
    `pack.json` describing it.
    """
    meta: dict[str, Any] = {}
    bundle: dict[str, Any] = {}
    rails: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(hxapp_bytes)) as zf:
            names = set(zf.namelist())

            if "pack.json" in names:
                info = zf.getinfo("pack.json")
                if info.file_size > _MAX_SECTION_BYTES:
                    raise PackError("pack.json is implausibly large")
                try:
                    meta = json.loads(zf.read("pack.json"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise PackError(f"pack.json is not valid JSON: {exc}")

            # Refuse credential/egress-bearing sections before reading anything
            # else, so the reason is precise rather than "import failed".
            for bad, why in FORBIDDEN_SECTIONS.items():
                if f"{bad}.json" in names:
                    raise PackError(
                        f"Accelerator packs may not ship '{bad}' — {why}. "
                        f"Install a connector package separately if the process needs one."
                    )

            for section in PACK_SECTIONS:
                fname = f"{section}.json"
                if fname not in names:
                    continue
                # Declared size checked before decompression — the zip-bomb guard.
                if zf.getinfo(fname).file_size > _MAX_SECTION_BYTES:
                    raise PackError(f"{fname} exceeds the {_MAX_SECTION_BYTES // (1024*1024)}MB section limit")
                try:
                    parsed = json.loads(zf.read(fname))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise PackError(f"{fname} is not valid JSON: {exc}")
                if not isinstance(parsed, list):
                    raise PackError(f"{fname} must be a JSON array")
                bundle[section] = parsed

            # Oct item 5: industry action rails for the rep desk. Configuration,
            # validated to the same closed vocabulary a tenant edit is — a pack
            # can order and word the rail's actions and RAISE a tier, never add an
            # action or lower one. Kept out of `bundle`: apply_bundle has no
            # business with it.
            if RAILS_FILE in names:
                if zf.getinfo(RAILS_FILE).file_size > _MAX_SECTION_BYTES:
                    raise PackError(f"{RAILS_FILE} exceeds the section limit")
                try:
                    raw_rails = json.loads(zf.read(RAILS_FILE))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise PackError(f"{RAILS_FILE} is not valid JSON: {exc}")
                # Validated by `validate_pack_rails` (async — the rail rules
                # live in case-service, R5e step 7), which every caller runs next.
                rails = raw_rails
    except zipfile.BadZipFile:
        raise PackError("pack is not a valid .hxapp archive")

    if not bundle and not rails:
        raise PackError("pack contains no provisionable content")

    if bundle:
        _validate_expressions(bundle)
    return {"meta": meta, "bundle": bundle, "rails": rails}


async def validate_pack_rails(pack: dict[str, Any], *, session=None) -> dict[str, Any]:
    """Validate the pack's rep-desk rails (engage rail rules, case-service) and
    keep the validated copy. Raises PackError as `read_pack` did before."""
    if not pack.get("rails"):
        return pack
    from velaris_shared.internal import client as internal

    try:
        answer = await internal.call("engage/rails/validate", {"rails": pack["rails"]},
                                     session=session)
    except internal.InternalCallError as exc:
        if exc.status == 422:
            raise PackError(f"{RAILS_FILE}: {exc.detail}") from exc
        raise PackError(f"{RAILS_FILE}: could not be checked ({exc.detail})") from exc
    return {**pack, "rails": answer["rails"]}


# Rule types whose `expression` must satisfy the strict HxSandbox grammar.
_EXPRESSION_RULE_TYPES = ("expression", "declare_expression")


def _validate_expressions(bundle: dict[str, Any]) -> None:
    """Refuse a pack carrying non-CONFORMING expression rules.

    `POST /rules` validates expressions at write time, but `apply_bundle` — the
    importer packs provision through — does NOT. That is defensible for HxDeploy,
    where a bundle is the operator's own artifact being promoted between their
    own environments. It is not defensible for a marketplace pack, which is
    third-party content: without this, a pack could ship an expression the rules
    API would reject outright and have it land straight in the database, where it
    would then run on the hardened `eval` fallback rather than the safe evaluator.

    Checked before anything is imported, so a bad pack is refused whole rather
    than part-applied.
    """
    from velaris_shared.core.safe_expression import Classification, classify_expression

    offenders: list[str] = []
    for rule in bundle.get("rule_definitions") or []:
        if rule.get("rule_type") not in _EXPRESSION_RULE_TYPES:
            continue
        expression = (rule.get("definition_json") or {}).get("expression", "")
        classification, reason = classify_expression(expression)
        if classification is not Classification.CONFORMING:
            offenders.append(
                f"{rule.get('name') or rule.get('id')}: {reason or classification.value}")

    if offenders:
        raise PackError(
            "Pack contains expression rules rejected by HxSandbox — "
            + "; ".join(offenders[:5])
        )

    # Oct 8b: a process's scriptTask is code in the same sense. apply_bundle
    # skips such a process on its own; a pack is refused whole instead.
    from velaris_shared.hxfusion.script_guard import check_bpmn

    for proc in bundle.get("process_definitions") or []:
        for r in check_bpmn(proc.get("bpmn_xml") or ""):
            offenders.append(f"{proc.get('name') or proc.get('id')}/{r['node_id']}: {r['reason']}")
    if offenders:
        raise PackError(
            "Pack contains process scripts rejected by the safe-script check — "
            + "; ".join(offenders[:5])
        )


def preview(pack: dict[str, Any]) -> dict[str, Any]:
    """What provisioning WOULD create, without touching anything.

    Counts come from the pack itself, so this is honest about scope but cannot
    predict upserts — an existing case type of the same name would be updated,
    not added. `apply_bundle`'s own return value is the source of truth for what
    actually happened, and that is what gets recorded.
    """
    meta = pack.get("meta") or {}
    bundle = pack.get("bundle") or {}
    contents = {s: len(bundle.get(s) or []) for s in PACK_SECTIONS if bundle.get(s)}
    if pack.get("rails"):
        contents["engage_rails"] = len(pack["rails"])
    return {
        "name": meta.get("name"),
        "vertical": meta.get("vertical"),
        "version": meta.get("version"),
        "summary": meta.get("summary"),
        "contents": contents,
        "total_items": sum(contents.values()),
        "sample_data_available": bool(meta.get("sample_data")),
        "sample_data_count": min(int(meta.get("sample_data", {}).get("count", 0) or 0), _MAX_SAMPLE_CASES)
                             if isinstance(meta.get("sample_data"), dict) else 0,
    }


async def already_provisioned(session: AsyncSession, tenant_id: str, package_id: str):
    """The successful provision row for this pack, if there is one.

    This is the P1 re-provision block. It lives in code because the real rule —
    one row with status='provisioned' — is a partial unique constraint, which
    MySQL/MariaDB cannot express; an index would pass on Postgres and quietly do
    nothing on the other two engines, which is worse than no index at all
    because it would look enforced.
    """
    from velaris_shared.db.models import MarketplacePackInstallModel as M
    r = await session.execute(
        select(M).where(M.tenant_id == tenant_id, M.package_id == package_id,
                        M.status == "provisioned").limit(1)
    )
    return r.scalar_one_or_none()


async def provision(session: AsyncSession, *, tenant_id: str, package_id: str,
                    pack: dict[str, Any], provisioned_by: str,
                    with_sample_data: bool = False) -> dict[str, Any]:
    """Provision a pack into a tenant. Refuses if it was already provisioned.

    Sample data is opt-in and off by default: creating demo cases in a live
    tenant unasked is not a reasonable default.
    """
    from velaris_shared.db.models import MarketplacePackInstallModel

    existing = await already_provisioned(session, tenant_id, package_id)
    if existing is not None:
        raise PackError(
            f"'{package_id}' is already provisioned for this tenant "
            f"(on {existing.provisioned_at:%Y-%m-%d}). Re-provisioning would overwrite "
            f"any changes made to its case types since. Use upgrade (P2) instead."
        )

    # A FAILED attempt leaves a row, and (tenant, package) is unique since mig
    # 136, so a retry must reuse that row rather than insert alongside it.
    prior = (await session.execute(
        select(MarketplacePackInstallModel).where(
            MarketplacePackInstallModel.tenant_id == tenant_id,
            MarketplacePackInstallModel.package_id == package_id,
        ).limit(1)
    )).scalar_one_or_none()

    meta = pack.get("meta") or {}
    bundle = dict(pack.get("bundle") or {})
    # apply_bundle enforces its own schema-skew guard (#14); a pack declares v1.
    bundle.setdefault("bundle_schema_version", str(meta.get("bundle_schema_version", "1")))

    has_sections = any(bundle.get(s) for s in PACK_SECTIONS)
    result = (await _apply_bundle(session, bundle, tenant_id) if has_sections
              else {"imported": 0, "skipped": 0, "errors": [], "needs_configuration": []})
    rails_result = await _write_rails(session, tenant_id=tenant_id, package_id=package_id,
                                      meta=meta, rails=pack.get("rails") or [])
    if rails_result:
        result = {**result, "imported": int(result.get("imported", 0))
                  + len(rails_result["created"]) + len(rails_result["updated"])}

    sample_count = 0
    if with_sample_data:
        sample_count = await _provision_sample_data(session, tenant_id=tenant_id,
                                                    pack=pack, meta=meta)

    errors = list(result.get("errors") or [])
    row = MarketplacePackInstallModel(
        tenant_id=tenant_id, package_id=package_id,
        pack_version=str(meta.get("version", "0.0.0")),
        vertical=meta.get("vertical"),
        # A pack that imported nothing did not succeed, whatever apply_bundle
        # returned — recording it as provisioned would block the retry.
        status="provisioned" if (not errors and result.get("imported", 0) > 0) else "failed",
        imported_count=int(result.get("imported", 0)),
        skipped_count=int(result.get("skipped", 0)),
        needs_config=list(result.get("needs_configuration") or []),
        errors_json=errors,
        sample_data=bool(with_sample_data),
        sample_count=sample_count,
        provisioned_by=provisioned_by,
        # The merge base for every future upgrade (P2). Captured here because
        # this is the only moment the pack's own version of these artifacts is
        # known to match what is in the database — after the first tenant edit
        # it is unrecoverable.
        snapshot_json=snapshot_of(bundle),
    )
    if prior is not None:
        # Retry of a failed provision: overwrite the old attempt in place.
        for field in ("pack_version", "vertical", "status", "imported_count",
                      "skipped_count", "needs_config", "errors_json",
                      "sample_data", "sample_count", "provisioned_by",
                      "snapshot_json"):
            setattr(prior, field, getattr(row, field))
        prior.provisioned_at = _now()
        row = prior
    else:
        session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # Lost the race to a concurrent provision of the same pack. The winner's
        # row stands; this call reports the conflict rather than double-applying.
        await session.rollback()
        raise PackError(
            f"'{package_id}' was provisioned concurrently by another request. "
            f"Use upgrade (P2) if you need to advance it."
        )

    logger.info("Pack %s provisioned for tenant %s: %d imported, %d skipped, %d errors",
                package_id, tenant_id, row.imported_count, row.skipped_count, len(errors))
    return {
        "package_id": package_id,
        "status": row.status,
        "imported": row.imported_count,
        "skipped": row.skipped_count,
        "errors": errors,
        "needs_configuration": row.needs_config,
        "sample_cases": sample_count,
        "rails": rails_result,
        "provisioned_at": row.provisioned_at.isoformat() if row.provisioned_at else None,
    }


async def _write_rails(session: AsyncSession, *, tenant_id: str, package_id: str,
                       meta: dict, rails: list) -> dict[str, Any] | None:
    """Store a pack's rep-desk rails for this tenant (Oct item 5).

    The pack paths carry the tenant as its TOKEN; the rail table is keyed by the
    tenant's id, so it is resolved here — and refused if it does not resolve,
    rather than written against nobody.
    """
    if not rails:
        return None
    from velaris_shared.internal import client as internal
    from velaris_shared.tenancy.identity import resolve_tenant

    resolved = await resolve_tenant(session, str(tenant_id))
    if resolved is None:
        raise PackError(f"tenant '{tenant_id}' does not resolve; rails not written")
    try:
        return await internal.call("engage/rails/upsert-from-pack", {
            "tenant_uuid": str(resolved[0]), "package_id": package_id,
            "version": str(meta.get("version", "0.0.0")), "rails": rails}, session=session)
    except internal.InternalCallError as exc:
        raise PackError(f"rails not written: {exc.detail}") from exc


async def _apply_bundle(session: AsyncSession, bundle: dict[str, Any], tenant_id: str) -> dict:
    """`hxdeploy.packager.apply_bundle`, run by case-service (it writes case
    types, forms, rules — case-service's own). Upserts, so a retry is harmless."""
    from velaris_shared.internal import client as internal

    try:
        return await internal.call("deploy/apply-bundle",
                                   {"tenant_id": str(tenant_id), "bundle": bundle},
                                   session=session, timeout=120)
    except internal.InternalCallError as exc:
        raise PackError(f"the pack's bundle could not be applied: {exc.detail}") from exc


async def _provision_sample_data(session: AsyncSession, *, tenant_id: str,
                                 pack: dict, meta: dict) -> int:
    """Create real sample cases against the pack's first case type.

    These are actual `CaseInstanceModel` rows — a flag that only generated a
    dict in memory and reported a count would be lying about what it did.

    Every record is marked synthetic in its metadata so a demo case can always
    be told apart from a real one, and can be found and deleted later.

    Best-effort and non-fatal: sample data is a convenience, and failing to
    create demo rows must never undo a successful content provision.
    """
    spec = meta.get("sample_data")
    if not isinstance(spec, dict):
        return 0
    count = min(int(spec.get("count", 0) or 0), _MAX_SAMPLE_CASES)
    if count <= 0:
        return 0

    case_types = (pack.get("bundle") or {}).get("case_types") or []
    if not case_types:
        return 0
    ct_name = case_types[0].get("name") or "SyntheticCase"

    try:
        from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
        from velaris_shared.apps.synthetic_data import generate_dataset
        from velaris_shared.tenancy.identity import resolve_tenant

        # Marketplace tenant ids are slugs/strings; CaseInstanceModel.tenant_id
        # is a UUID FK. Never compare or assign the raw value — resolve it.
        resolved = await resolve_tenant(session, tenant_id)
        if resolved is None:
            logger.warning("Sample data skipped — tenant %r does not resolve", tenant_id)
            return 0
        tenant_uuid, _slug = resolved

        # Looked up by NAME only, matching how apply_bundle itself resolves case
        # types. It creates them with no tenant_id (CaseTypeModel is globally
        # unique on name+version), so filtering by tenant here would match
        # nothing and sample data would silently always be zero.
        #
        # The CASES below are still tenant-scoped — those rows do carry a real
        # tenant_id, so demo data never leaks across tenants even though the
        # case type it points at is shared.
        ct = (await session.execute(
            select(CaseTypeModel).where(
                CaseTypeModel.name == ct_name,
                CaseTypeModel.is_deleted.is_(False),
            ).limit(1)
        )).scalar_one_or_none()
        if ct is None:
            logger.warning("Sample data skipped — case type %r not found after import", ct_name)
            return 0

        dataset = generate_dataset(case_type_name=ct_name, record_count=count)
        made = 0
        for rec in dataset.get("cases", []):
            session.add(CaseInstanceModel(
                case_type_id=ct.id,
                case_type_version=str(getattr(ct, "version", "1") or "1"),
                tenant_id=tenant_uuid,
                status="new",
                priority=rec.get("priority", "medium"),
                data=rec,
                extra_metadata={
                    "synthetic": True,                 # findable + deletable later
                    "source": "accelerator_pack",
                    "pack": meta.get("name") or meta.get("id"),
                },
                created_by="accelerator-pack",
            ))
            made += 1
        await session.flush()
        return made
    except Exception as exc:
        logger.warning("Sample data for %s skipped (%s)", ct_name, exc)
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# P2 — versioning + upgrade (three-way merge)
# ══════════════════════════════════════════════════════════════════════════════
#
# An upgrade must not clobber tenant edits. That needs three inputs, not two:
#
#   BASE    what the pack shipped when it was provisioned (snapshot_json)
#   INCOMING what the new pack version ships
#   CURRENT  what is in the tenant's database right now
#
# With only INCOMING and CURRENT the platform cannot distinguish "the tenant
# customised this" from "the pack changed this", so it would have to either
# overwrite the customer's work or refuse every upgrade. The snapshot is what
# makes the third case — both sides changed — visible as a conflict instead of
# silently resolved.

# Sections merged artifact-by-artifact, keyed by name. The rest ride along
# whole: they are structural (stages/steps belong to a case type) or rarely
# customised, and a false conflict on them would block the whole upgrade.
MERGED_SECTIONS = ("case_types", "form_definitions", "rule_definitions")

# Per-artifact comparison field. Everything the pack meaningfully controls lives
# in definition_json for these three; comparing whole rows would flag noise like
# updated_at as a tenant edit.
_COMPARE_FIELD = "definition_json"


def _key(item: dict) -> str:
    return str(item.get("name") or item.get("id") or "")


def _by_key(items: list[dict] | None) -> dict[str, dict]:
    return {_key(i): i for i in (items or []) if _key(i)}


def snapshot_of(bundle: dict[str, Any]) -> dict[str, Any]:
    """The merge base: only the sections and fields the merge compares.

    Storing the whole bundle would bloat the row and, worse, make every
    incidental field difference look like a change.
    """
    snap: dict[str, Any] = {}
    for section in MERGED_SECTIONS:
        snap[section] = {
            _key(i): i.get(_COMPARE_FIELD) for i in (bundle.get(section) or []) if _key(i)
        }
    return snap


async def _current_state(session: AsyncSession, bundle: dict[str, Any]) -> dict[str, dict]:
    """What the tenant's database holds today for the artifacts this pack owns."""
    from velaris_shared.db.models import (
        CaseTypeModel, FormDefinitionModel, RuleDefinitionModel,
    )
    models = {
        "case_types": CaseTypeModel,
        "form_definitions": FormDefinitionModel,
        "rule_definitions": RuleDefinitionModel,
    }
    out: dict[str, dict] = {}
    for section, model in models.items():
        names = [_key(i) for i in (bundle.get(section) or []) if _key(i)]
        if not names:
            out[section] = {}
            continue
        rows = (await session.execute(
            select(model).where(model.name.in_(names))
        )).scalars().all()
        out[section] = {r.name: getattr(r, _COMPARE_FIELD, None) for r in rows}
    return out


def merge_plan(base: dict, incoming: dict, current: dict) -> dict[str, Any]:
    """Classify every artifact into apply / skip / conflict.

      · pack unchanged                  → nothing to do
      · pack changed, tenant untouched  → apply
      · pack unchanged, tenant changed  → keep the tenant's version
      · both changed                    → CONFLICT, reported, never resolved
      · new in this version             → apply
    """
    apply_keys: dict[str, list[str]] = {}
    conflicts: list[dict] = []
    skipped: list[dict] = []

    for section in MERGED_SECTIONS:
        base_s = (base or {}).get(section) or {}
        inc_s = _by_key(incoming.get(section))
        cur_s = (current or {}).get(section) or {}
        keep: list[str] = []

        for name, item in inc_s.items():
            inc_def = item.get(_COMPARE_FIELD)
            base_def = base_s.get(name)
            cur_def = cur_s.get(name)

            if name not in base_s:
                keep.append(name)                       # new artifact in this version
                continue

            pack_changed = inc_def != base_def
            tenant_changed = (name in cur_s) and (cur_def != base_def)

            if not pack_changed:
                if tenant_changed:
                    skipped.append({"section": section, "name": name,
                                    "reason": "unchanged by the pack; tenant edit kept"})
                continue
            if tenant_changed:
                conflicts.append({
                    "section": section, "name": name,
                    "reason": "changed by both the pack and this tenant — not applied",
                })
                continue
            keep.append(name)

        apply_keys[section] = keep

    return {"apply": apply_keys, "conflicts": conflicts, "skipped": skipped,
            "apply_count": sum(len(v) for v in apply_keys.values())}


def _filter_bundle(bundle: dict, plan: dict) -> dict:
    """Reduce a bundle to only the artifacts the plan says are safe to apply.

    Non-merged sections pass through whole — stages and steps are structural
    parts of a case type, and importing a case type without them would leave a
    lifecycle referencing stages that do not exist.
    """
    out: dict[str, Any] = {}
    for section, items in bundle.items():
        if section in MERGED_SECTIONS:
            allowed = set(plan["apply"].get(section) or [])
            if allowed:
                out[section] = [i for i in items if _key(i) in allowed]
        else:
            out[section] = items
    return out


async def upgrade_preview(session: AsyncSession, *, tenant_id: str, package_id: str,
                          pack: dict[str, Any]) -> dict[str, Any]:
    """What an upgrade would apply, keep, and flag. Changes nothing."""
    install = await already_provisioned(session, tenant_id, package_id)
    if install is None:
        raise PackError(f"'{package_id}' is not provisioned for this tenant — install it first")

    bundle = pack.get("bundle") or {}
    meta = pack.get("meta") or {}
    plan = merge_plan(install.snapshot_json or {}, bundle,
                      await _current_state(session, bundle))
    return {
        "package_id": package_id,
        "from_version": install.pack_version,
        "to_version": str(meta.get("version", "0.0.0")),
        "will_apply": plan["apply_count"],
        "conflicts": plan["conflicts"],
        "kept_tenant_edits": plan["skipped"],
        "up_to_date": plan["apply_count"] == 0 and not plan["conflicts"],
    }


async def upgrade(session: AsyncSession, *, tenant_id: str, package_id: str,
                  pack: dict[str, Any], upgraded_by: str) -> dict[str, Any]:
    """Apply a pack upgrade, merging around tenant edits.

    Conflicts are reported and skipped, never auto-resolved. The upgrade still
    proceeds for everything that is not conflicted — refusing the whole upgrade
    over one customised form would make packs un-upgradable in practice.
    """
    from velaris_shared.db.models import MarketplacePackUpgradeModel

    install = await already_provisioned(session, tenant_id, package_id)
    if install is None:
        raise PackError(f"'{package_id}' is not provisioned for this tenant — install it first")

    meta = pack.get("meta") or {}
    bundle = dict(pack.get("bundle") or {})
    to_version = str(meta.get("version", "0.0.0"))
    from_version = install.pack_version

    plan = merge_plan(install.snapshot_json or {}, bundle,
                      await _current_state(session, bundle))

    result = {"imported": 0, "skipped": 0, "errors": []}
    if plan["apply_count"] > 0:
        filtered = _filter_bundle(bundle, plan)
        filtered.setdefault("bundle_schema_version",
                            str(meta.get("bundle_schema_version", "1")))
        result = await _apply_bundle(session, filtered, tenant_id)
    # Rails merge the same way: a rail the tenant edited is kept and reported.
    rails_result = await _write_rails(session, tenant_id=tenant_id, package_id=package_id,
                                      meta=meta, rails=pack.get("rails") or [])

    errors = list(result.get("errors") or [])
    session.add(MarketplacePackUpgradeModel(
        tenant_id=tenant_id, package_id=package_id,
        from_version=from_version, to_version=to_version,
        status="failed" if errors else "applied",
        applied_count=int(result.get("imported", 0)),
        skipped_count=len(plan["skipped"]) + len(plan["conflicts"]),
        conflicts_json=plan["conflicts"], errors_json=errors,
        upgraded_by=upgraded_by,
    ))

    if not errors:
        # The snapshot advances only for what actually landed. Advancing it
        # wholesale would tell the NEXT upgrade that a conflicted artifact had
        # been accepted, and quietly overwrite the tenant next time round.
        new_snap = dict(install.snapshot_json or {})
        incoming_snap = snapshot_of(bundle)
        for section in MERGED_SECTIONS:
            applied = set(plan["apply"].get(section) or [])
            merged = dict(new_snap.get(section) or {})
            for name, definition in (incoming_snap.get(section) or {}).items():
                if name in applied:
                    merged[name] = definition
            new_snap[section] = merged
        install.snapshot_json = new_snap
        install.pack_version = to_version

    await session.commit()
    logger.info("Pack %s upgraded %s → %s for tenant %s: %d applied, %d conflicts",
                package_id, from_version, to_version, tenant_id,
                result.get("imported", 0), len(plan["conflicts"]))
    return {
        "package_id": package_id, "from_version": from_version, "to_version": to_version,
        "applied": int(result.get("imported", 0)),
        "conflicts": plan["conflicts"], "kept_tenant_edits": plan["skipped"],
        "rails": rails_result,
        "errors": errors,
        "status": "failed" if errors else "applied",
    }


# ══════════════════════════════════════════════════════════════════════════════
# P3 — fork a provisioned pack into an editable pack of your own
# ══════════════════════════════════════════════════════════════════════════════

async def fork(session: AsyncSession, *, tenant_id: str, package_id: str,
               new_id: str, new_name: str) -> dict[str, Any]:
    """Export a provisioned pack's CURRENT state as a new, editable pack.

    Exports what the tenant actually has now — customisations included — which
    is the point: forking exists because someone changed the pack enough that
    they want to own it rather than keep merging around the original.

    A fork is severed deliberately. It gets a new id and carries no upgrade
    relationship to its parent, because a fork that still tracked upstream would
    reintroduce exactly the conflicts it was created to escape.
    """
    from velaris_shared.db.models import (
        CaseTypeModel, FormDefinitionModel, RuleDefinitionModel,
    )

    install = await already_provisioned(session, tenant_id, package_id)
    if install is None:
        raise PackError(f"'{package_id}' is not provisioned for this tenant — nothing to fork")
    if not new_id or "/" not in new_id:
        raise PackError("fork id must look like '<publisher>/<pack-name>'")

    snapshot = install.snapshot_json or {}
    bundle: dict[str, list] = {}

    ct_names = list((snapshot.get("case_types") or {}).keys())
    cts = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.name.in_(ct_names))
    )).scalars().all() if ct_names else []
    bundle["case_types"] = [{
        "id": f"ct-{c.name.lower().replace(' ', '-')}", "name": c.name, "version": c.version,
        "description": c.description or "", "icon": c.icon, "color": c.color,
        "tags": list(c.tags or []), "default_priority": c.default_priority,
        "portal_enabled": bool(c.portal_enabled), "definition_json": c.definition_json,
    } for c in cts]

    fm_names = list((snapshot.get("form_definitions") or {}).keys())
    fms = (await session.execute(
        select(FormDefinitionModel).where(FormDefinitionModel.name.in_(fm_names))
    )).scalars().all() if fm_names else []
    bundle["form_definitions"] = [{
        "id": f"fm-{f.name.lower().replace(' ', '-')}", "name": f.name,
        "version": f.version, "definition_json": f.definition_json,
    } for f in fms]

    rl_names = list((snapshot.get("rule_definitions") or {}).keys())
    rls = (await session.execute(
        select(RuleDefinitionModel).where(RuleDefinitionModel.name.in_(rl_names))
    )).scalars().all() if rl_names else []
    bundle["rule_definitions"] = [{
        "id": f"rl-{r.name.lower().replace(' ', '-')}", "name": r.name, "version": r.version,
        "rule_type": r.rule_type, "scope": r.scope, "scope_target_id": r.scope_target_id,
        "enabled": bool(r.enabled), "priority": r.priority,
        "definition_json": r.definition_json,
    } for r in rls]

    bundle = {k: v for k, v in bundle.items() if v}

    # The pack's rep-desk rails, as this tenant has them now (edits included).
    from velaris_shared.db.models import EngageRailConfigModel
    from velaris_shared.tenancy.identity import resolve_tenant

    rails: list[dict] = []
    resolved = await resolve_tenant(session, str(tenant_id))
    if resolved is not None:
        rails = [r.config for r in (await session.execute(select(EngageRailConfigModel).where(
            EngageRailConfigModel.tenant_id == resolved[0],
            EngageRailConfigModel.package_id == package_id))).scalars().all()]
    if not bundle and not rails:
        raise PackError("nothing to fork — the pack's artifacts are no longer present")

    # A fork must clear the same bar as any other pack. Catching a
    # non-CONFORMING expression here means the author is told at fork time,
    # not when a customer tries to install what they published.
    if bundle:
        _validate_expressions(bundle)

    return {
        "meta": {
            "id": new_id, "name": new_name, "version": "1.0.0",
            "vertical": install.vertical, "bundle_schema_version": "1",
            "summary": f"Forked from {package_id} v{install.pack_version}.",
            "forked_from": {"package_id": package_id, "version": install.pack_version},
        },
        "bundle": bundle,
        "rails": rails,
    }


def fork_to_hxapp(forked: dict[str, Any]) -> bytes:
    """Package a fork as an installable `.hxapp`, same layout as build-pack.py."""
    meta = forked["meta"]
    manifest = {
        "id": meta["id"], "name": meta["name"], "version": meta["version"],
        "type": PACK_TYPE, "publisher": meta["id"].split("/")[0],
        # A fork is community tier by construction: official tier is derived from
        # the publisher org and the baked-in registry, and must never be
        # inherited from a parent pack.
        "publisher_tier": "community",
        "outbound_domains": [], "manifest_version": 1, "runtime": "python",
        "description": meta.get("summary", ""),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("pack.json", json.dumps(meta, indent=2))
        for section, items in (forked.get("bundle") or {}).items():
            zf.writestr(f"{section}.json", json.dumps(items, indent=2, default=str))
        if forked.get("rails"):
            zf.writestr(RAILS_FILE, json.dumps(forked["rails"], indent=2))
    return buf.getvalue()
