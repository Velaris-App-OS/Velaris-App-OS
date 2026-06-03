"""P44 — Five-pass import pipeline runner.

Runs as a FastAPI BackgroundTask so the upload endpoint returns immediately.
Updates import_jobs.status at each pass so the UI can show real-time progress.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from case_service.bpm_importer.extractor import extract
from case_service.bpm_importer.mapper import map_rules
from case_service.bpm_importer.generator import generate
from case_service.bpm_importer.reporter import build_report
from case_service.bpm_importer.parsers import pega, appian, servicenow
from case_service.bpm_importer.parsers import bpmn2, power_automate, salesforce_flow, nintex
from case_service.db.models import ImportJobModel

logger = logging.getLogger(__name__)

# All BPMN 2.0 vendors share one deep parser; Tier 2+ have dedicated parsers
_PARSERS: dict[str, object] = {
    "pega":           pega.parse_files,
    "camunda":        bpmn2.parse_files,
    "jbpm":           bpmn2.parse_files,
    "flowable":       bpmn2.parse_files,
    "ibm":            bpmn2.parse_files,
    "oracle":         bpmn2.parse_files,
    "bizagi":         bpmn2.parse_files,
    "appian":         appian.parse_files,
    "servicenow":     servicenow.parse_files,
    "power_automate": power_automate.parse_files,
    "salesforce":     salesforce_flow.parse_files,
    "nintex":         nintex.parse_files,
}


async def run_pipeline(
    job_id: uuid.UUID,
    tool: str,
    filename: str,
    file_bytes: bytes,
    session_factory: async_sessionmaker,
    blueprint: dict | None = None,
) -> None:
    """Run all five passes, persisting progress after each.

    blueprint: pre-analysed VelarisBlueprint dict from Stage 2 AI (Stage 3 uses it
               to skip round-robin mapping and use AI-inferred ordering instead).
    """
    try:
        await _run(job_id, tool, filename, file_bytes, session_factory, blueprint=blueprint)
    except Exception as exc:
        logger.error("Import pipeline top-level error for job %s: %s", job_id, type(exc).__name__)


async def _run(
    job_id: uuid.UUID,
    tool: str,
    filename: str,
    file_bytes: bytes,
    session_factory: async_sessionmaker,
    blueprint: dict | None = None,
) -> None:
    async with session_factory() as session:
        job = await session.get(ImportJobModel, job_id)
        if not job:
            return

        try:
            # Pass 1 — Extract (skip if no file bytes provided; blueprints carry the data)
            await _set_status(session, job, "extracting")
            pass1 = extract(tool, file_bytes, filename) if file_bytes else {"files": [], "tool": tool, "filename": filename, "total": 0, "skipped": 0}
            job.pass1_result = pass1
            await session.commit()

            # Pass 2 — Parse
            await _set_status(session, job, "parsing")
            parse_fn = _PARSERS.get(tool.lower())
            pass2 = parse_fn(pass1.get("files", [])) if parse_fn else {}
            job.pass2_result = pass2
            await session.commit()

            # Pass 3 — Map (use AI blueprint ordering if available)
            await _set_status(session, job, "mapping")
            pass3 = await map_rules(tool, pass2, session, ai_blueprint=blueprint)
            job.pass3_result = pass3
            await session.commit()

            # Pass 4 — Generate
            await _set_status(session, job, "generating")
            pass4 = generate(pass3)
            job.pass4_result = pass4
            await session.commit()

            # Pass 5 — Report
            report = build_report(tool, filename, pass1, pass2, pass3, pass4)
            job.report = report
            job.status = "complete"
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()

        except Exception as exc:
            logger.exception("Import pipeline failed for job %s", job_id)
            job.status = "failed"
            job.error = str(exc)[:500]
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()


async def _set_status(session: AsyncSession, job: ImportJobModel, status: str) -> None:
    job.status = status
    await session.commit()
