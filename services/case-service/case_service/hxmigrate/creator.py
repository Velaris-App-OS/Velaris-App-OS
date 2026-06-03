"""HxMigrate v2 — Creator module.

Takes a ValidatedPlan and POSTs to Velaris APIs in dependency order:
  1. Data Models  → POST /api/v1/data-models   (definition_json format)
  2. Forms        → POST /api/v1/forms          (definition_json format)
  3. Case Types   → POST /api/v1/case-types     (definition_json + tags)
  4. Rules        → POST /api/v1/rules          (definition_json format)
  5. SLAs         → Embedded in case type definition (no standalone SLA template API)
  6. Audit record → case_type_migrations table  (tracks who imported when)

Default mode is generate (dry_run=True) — user clicks 'Apply to Velaris' to create.
Creator supports dry-run mode (logs all calls without making them).

Security:
  SEC-5 (SSRF): base_url validated against allowlist — never user-supplied.
  SEC-6 (token): auth token never logged, never stored, redacted from errors.
  SEC-9 (DB): no raw SQL; all creation via Velaris REST APIs (parameterized).
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

import httpx

from case_service.hxmigrate.schemas import ValidatedPlan, VelarisForm, VelarisStage
from case_service.hxmigrate.security import sanitize_error

logger = logging.getLogger(__name__)

# ── SEC-5: SSRF allowlist ─────────────────────────────────────────────────────

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _validate_base_url(url: str) -> str:
    """SEC-5: validate that base_url points to an internal host only."""
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid base URL")
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Disallowed URL scheme: {parsed.scheme}")
    hostname = (parsed.hostname or "").lower()
    if hostname == "169.254.169.254":
        raise ValueError("Cloud metadata endpoint not allowed")
    if hostname.startswith("169.254."):
        raise ValueError("Link-local address not allowed")
    extra_hosts = {
        h.strip().lower()
        for h in os.getenv("HELIX_INTERNAL_HOSTS", "").split(",")
        if h.strip()
    }
    if hostname not in (_ALLOWED_HOSTS | extra_hosts):
        raise ValueError(f"Host not in internal allowlist: {hostname}")
    return url.rstrip("/")


# ── Creation result tracking ──────────────────────────────────────────────────

@dataclass
class CreationReport:
    created:  list[dict] = field(default_factory=list)
    failed:   list[dict] = field(default_factory=list)
    skipped:  list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    dry_run:  bool = False

    def summary(self) -> dict:
        return {
            "dry_run":   self.dry_run,
            "created":   len(self.created),
            "failed":    len(self.failed),
            "skipped":   len(self.skipped),
            "conflicts": len(self.conflicts),
        }


# ── Creator ───────────────────────────────────────────────────────────────────

class Creator:
    """Creates Velaris objects from a ValidatedPlan via REST API.

    dry_run=True  → logs all calls without making them (default).
    dry_run=False → actually POSTs to Velaris APIs.
    """

    def __init__(
        self,
        dry_run: bool = True,
        auth_token: str = "",
        base_url: str = "",
        imported_by_user_id: str = "",
        imported_by_email: str = "",
        run_id: str = "",
    ) -> None:
        self.dry_run = dry_run
        self._token  = auth_token   # SEC-6: never logged
        self._base   = _validate_base_url(
            base_url or os.getenv("HELIX_CASE_SERVICE_URL", "http://localhost:8200")
        )
        self._imported_by_user_id = imported_by_user_id
        self._imported_by_email   = imported_by_email
        self._run_id              = run_id
        self._created_ids: dict[str, list[str]] = {
            "data_models": [], "forms": [], "case_types": [], "rules": [],
        }

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _post(self, path: str, body: dict) -> dict | None:
        url = f"{self._base}{path}"
        if self.dry_run:
            logger.info("DRY-RUN POST %s — %s", path, list(body.keys()))
            return {"id": f"dry-run-{path.split('/')[-1]}", "dry_run": True}
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(url, headers=self._headers(), json=body)
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("Creator POST %s failed: HTTP %s", path, exc.response.status_code)
            return None
        except Exception as exc:
            logger.warning("Creator POST %s error: %s", path, type(exc).__name__)
            return None

    async def _check_exists(self, resource_type: str, name: str) -> bool:
        """Check if a resource with the given name already exists (SEC-9 conflict detection)."""
        paths = {"case_types": "/api/v1/case-types", "forms": "/api/v1/forms"}
        path = paths.get(resource_type)
        if not path or self.dry_run:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{self._base}{path}",
                    headers=self._headers(),
                    params={"limit": 200},
                )
                if r.status_code == 200:
                    data  = r.json()
                    items = data if isinstance(data, list) else data.get("items", data.get("data", []))
                    name_lower = name.lower().strip()
                    return any(
                        (item.get("name") or "").lower().strip() == name_lower
                        for item in items
                        if isinstance(item, dict)
                    )
        except Exception:
            pass
        return False

    # ── Public API ────────────────────────────────────────────────────────────

    async def create_all(self, plan: ValidatedPlan) -> CreationReport:
        """Create all resources from a ValidatedPlan in dependency order."""
        report = CreationReport(dry_run=self.dry_run)
        try:
            # 1. Data Models
            dm_id = await self._create_data_model(plan, report)

            # 2. Forms (no dependencies)
            form_id_map = await self._create_forms(plan, report)

            # 3. Case Type (needs form_ids + SLA config embedded)
            ct_id = await self._create_case_type(plan, form_id_map, report)

            # 4. Business Rules (scope-linked to case type)
            await self._create_rules(plan, ct_id, report)

            # 5. Write audit record (if case type was created)
            if ct_id and not self.dry_run:
                await self._write_audit_record(plan, ct_id, report)

        except Exception as exc:
            logger.error("Creator.create_all failed: %s", type(exc).__name__)
            report.failed.append({"resource": "pipeline", "error": sanitize_error(str(exc))})
            if not self.dry_run:
                await self.rollback()

        return report

    async def rollback(self) -> None:
        """Delete all created resources in reverse order (on failure)."""
        if self.dry_run:
            return
        _DELETE_PATHS = {
            "rules":       "/api/v1/rules/",
            "case_types":  "/api/v1/case-types/",
            "forms":       "/api/v1/forms/",
            "data_models": "/api/v1/data-models/",
        }
        for resource_type in ["rules", "case_types", "forms", "data_models"]:
            for rid in reversed(self._created_ids.get(resource_type, [])):
                try:
                    async with httpx.AsyncClient(timeout=10.0) as c:
                        await c.delete(f"{self._base}{_DELETE_PATHS[resource_type]}{rid}",
                                       headers=self._headers())
                    logger.info("Rollback: deleted %s %s", resource_type, rid)
                except Exception as exc:
                    logger.warning("Rollback delete failed %s %s: %s", resource_type, rid, type(exc).__name__)

    # ── Resource creators ─────────────────────────────────────────────────────

    async def _create_data_model(self, plan: ValidatedPlan, report: CreationReport) -> str | None:
        if not plan.data_model:
            return None
        body = {
            "name":    f"{plan.case_type_name} Data Model",
            "version": "1.0.0",
            "definition_json": {
                "fields": [
                    {"key": f.field_key, "label": f.label, "type": f.data_type, "required": f.required}
                    for f in plan.data_model
                ]
            },
        }
        result = await self._post("/api/v1/data-models", body)
        if result:
            rid = result.get("id", "")
            if rid and not result.get("dry_run"):
                self._created_ids["data_models"].append(rid)
            report.created.append({"resource": "data_model", "id": rid, "name": body["name"]})
            return rid
        report.failed.append({"resource": "data_model", "name": body["name"]})
        return None

    async def _create_forms(self, plan: ValidatedPlan, report: CreationReport) -> dict[str, str]:
        """Create all forms; return mapping of form_key → created form_id."""
        form_id_map: dict[str, str] = {}
        for form in plan.forms:
            body = {
                "name":    form.name,
                "version": "1.0.0",
                "definition_json": {
                    "sections": [
                        {
                            "title":  sec.title,
                            "fields": [
                                {
                                    "key":         f.field_key,
                                    "label":       f.label,
                                    "type":        f.field_type,
                                    "required":    f.required,
                                    "options":     f.options,
                                    "placeholder": f.placeholder,
                                }
                                for f in sec.fields
                            ],
                        }
                        for sec in form.sections
                    ]
                },
            }
            result = await self._post("/api/v1/forms", body)
            if result:
                rid = result.get("id", "")
                if rid and not result.get("dry_run"):
                    self._created_ids["forms"].append(rid)
                form_id_map[form.form_key] = rid
                report.created.append({"resource": "form", "id": rid, "name": form.name})
            else:
                report.failed.append({"resource": "form", "name": form.name})
        return form_id_map

    async def _create_case_type(
        self,
        plan: ValidatedPlan,
        form_id_map: dict[str, str],
        report: CreationReport,
    ) -> str | None:
        # SEC-9 conflict check — exact name match
        if await self._check_exists("case_types", plan.case_type_name):
            conflict = {
                "resource": "case_type", "name": plan.case_type_name,
                "conflict_type": "duplicate_name", "resolution": "fail",
                "message": (
                    f"Case type '{plan.case_type_name}' already exists in Velaris. "
                    "Resolve the conflict manually or rename before applying."
                ),
            }
            report.conflicts.append(conflict)
            report.failed.append({"resource": "case_type", "name": plan.case_type_name, "reason": "duplicate_name"})
            return None

        stages_payload = []
        for stage in plan.stages:
            steps_payload = []
            for step in stage.steps:
                sp: dict = {
                    "id":        step.step_key,
                    "name":      step.name,
                    "step_type": step.step_type,
                    "required":  True,
                }
                if step.form_key and step.form_key in form_id_map:
                    sp["form_id"] = form_id_map[step.form_key]
                steps_payload.append(sp)
            stages_payload.append({"id": stage.stage_key, "name": stage.name, "steps": steps_payload})

        # Embed SLA config in definition (no standalone SLA template API)
        slas_config = [
            {
                "name":             sla.name,
                "goal_seconds":     int(sla.goal_hours * 3600),
                "deadline_seconds": int(sla.deadline_hours * 3600),
                "escalation_to":    sla.escalation_to,
            }
            for sla in plan.slas
        ]

        body = {
            "name":            plan.case_type_name,
            "version":         plan.version,
            "definition_json": {"stages": stages_payload, "sla_policies": slas_config},
            "tags":            ["imported", "hxmigrate"],
            "portal_enabled":  False,
        }
        result = await self._post("/api/v1/case-types", body)
        if result:
            rid = result.get("id", "")
            if rid and not result.get("dry_run"):
                self._created_ids["case_types"].append(rid)
            report.created.append({"resource": "case_type", "id": rid, "name": plan.case_type_name})
            return rid
        report.failed.append({"resource": "case_type", "name": plan.case_type_name})
        return None

    async def _create_rules(self, plan: ValidatedPlan, case_type_id: str | None, report: CreationReport) -> None:
        for rule in plan.rules:
            body: dict = {
                "name":            rule.name,
                "version":         "1.0.0",
                "rule_type":       rule.rule_type,
                "scope":           "case_type" if case_type_id else "global",
                "scope_target_id": case_type_id,
                "definition_json": {
                    "expression":  rule.expression,
                    "description": rule.description,
                },
                "enabled": True,
            }
            result = await self._post("/api/v1/rules", body)
            if result:
                rid = result.get("id", "")
                if rid and not result.get("dry_run"):
                    self._created_ids["rules"].append(rid)
                report.created.append({"resource": "rule", "id": rid, "name": rule.name})
            else:
                report.failed.append({"resource": "rule", "name": rule.name})

    async def _write_audit_record(
        self,
        plan: ValidatedPlan,
        case_type_id: str,
        report: CreationReport,
    ) -> None:
        """Write import audit record to case_type_migrations table (SEC-3: direct DB write)."""
        try:
            from case_service.db.session import get_session_factory
            from sqlalchemy import text

            steps_count = sum(len(s.steps) for s in plan.stages)
            sf = get_session_factory()
            async with sf() as session:
                async with session.begin():
                    await session.execute(
                        text("""
                            INSERT INTO case_type_migrations
                                (id, case_type_id, run_id, source_platform, source_filename,
                                 imported_by_user_id, imported_by_email, imported_at,
                                 stages_count, steps_count, forms_count, rules_count, slas_count)
                            VALUES
                                (:id, :ct_id, :run_id, :platform, :filename,
                                 :user_id, :email, NOW(),
                                 :stages, :steps, :forms, :rules, :slas)
                        """),
                        {
                            "id":       str(uuid.uuid4()),
                            "ct_id":    case_type_id,
                            "run_id":   self._run_id or None,
                            "platform": plan.vendor or "unknown",
                            "filename": plan.source_filename or "",
                            "user_id":  self._imported_by_user_id or "",
                            "email":    self._imported_by_email or "",
                            "stages":   len(plan.stages),
                            "steps":    steps_count,
                            "forms":    len(plan.forms),
                            "rules":    len(plan.rules),
                            "slas":     len(plan.slas),
                        },
                    )
            report.created.append({"resource": "migration_record", "case_type_id": case_type_id})
        except Exception as exc:
            logger.warning("Audit record write failed: %s", type(exc).__name__)
