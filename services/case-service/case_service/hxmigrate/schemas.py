"""HxMigrate v2 — Pydantic schemas for the structured pipeline.

All AI-generated output is validated against these models (extra=forbid)
so injected fields from prompt attacks are silently dropped.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Base config — forbid extra fields (SEC-4: drop injected keys) ─────────────

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Field-level models ────────────────────────────────────────────────────────

class VelarisField(_Strict):
    field_key:   str = Field(max_length=200)
    label:       str = Field(max_length=500)
    field_type:  str = Field(default="text", max_length=50)
    required:    bool = False
    options:     list[str] = Field(default_factory=list, max_length=200)
    placeholder: str = Field(default="", max_length=500)


class VelarisFormSection(_Strict):
    title:  str = Field(max_length=500)
    fields: list[VelarisField] = Field(default_factory=list, max_length=200)


class VelarisForm(_Strict):
    form_key:   str = Field(max_length=200)
    name:       str = Field(max_length=500)
    sections:   list[VelarisFormSection] = Field(default_factory=list, max_length=50)
    source_ref: str = Field(default="", max_length=500)


# ── Step / Stage models ───────────────────────────────────────────────────────

ALLOWED_STEP_TYPES = {
    "form", "user_task", "automated", "approval", "subprocess",
    "routing", "payment_request", "payment_disbursement",
    "crm_sync", "invoice_generate", "sms_send", "slack_notify",
    "identity_verify", "esign", "doc_extract", "doc_store",
}


class VelarisStep(_Strict):
    step_key:        str = Field(max_length=200)
    name:            str = Field(max_length=500)
    step_type:       str = Field(default="user_task", max_length=50)
    order:           int = Field(default=0, ge=0, le=10000)
    form_key:        str | None = Field(default=None, max_length=200)
    form_definition: VelarisForm | None = None
    source_ref:      str = Field(default="", max_length=500)
    confidence:      float = Field(default=1.0, ge=0.0, le=1.0)
    conditions:      list[str] = Field(default_factory=list, max_length=20)
    assignee_type:   Literal["user", "queue", "auto"] = "user"
    connector_hint:  str = Field(default="", max_length=200)

    @field_validator("step_type")
    @classmethod
    def validate_step_type(cls, v: str) -> str:
        return v if v in ALLOWED_STEP_TYPES else "user_task"


class VelarisStage(_Strict):
    stage_key: str = Field(max_length=200)
    name:      str = Field(max_length=500)
    order:     int = Field(default=0, ge=0, le=1000)
    steps:     list[VelarisStep] = Field(default_factory=list, max_length=500)


# ── Rule / SLA models ─────────────────────────────────────────────────────────

class VelarisRule(_Strict):
    rule_key:    str = Field(max_length=200)
    name:        str = Field(max_length=500)
    rule_type:   Literal["expression", "decision_table", "condition", "script", "other"] = "condition"
    expression:  str = Field(default="", max_length=5000)
    description: str = Field(default="", max_length=2000)
    confidence:  float = Field(default=0.5, ge=0.0, le=1.0)


class VelarisSLA(_Strict):
    sla_key:        str = Field(max_length=200)
    name:           str = Field(max_length=500)
    goal_hours:     float = Field(default=24.0, ge=0.0, le=87600.0)
    deadline_hours: float = Field(default=48.0, ge=0.0, le=87600.0)
    escalation_to:  str = Field(default="", max_length=200)
    confidence:     float = Field(default=1.0, ge=0.0, le=1.0)


# ── Data model ────────────────────────────────────────────────────────────────

class VelarisDataField(_Strict):
    field_key:  str = Field(max_length=200)
    label:      str = Field(max_length=500)
    data_type:  str = Field(default="string", max_length=50)
    required:   bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# ── Top-level blueprint (one per artifact) ────────────────────────────────────

class VelarisBlueprint(_Strict):
    """Structured output produced by Stage 2 AI analysis for one BPM artifact."""
    case_type_name: str = Field(max_length=500)
    version:        str = Field(default="1.0.0", max_length=50)
    stages:         list[VelarisStage] = Field(default_factory=list, max_length=100)
    forms:          list[VelarisForm] = Field(default_factory=list, max_length=500)
    rules:          list[VelarisRule] = Field(default_factory=list, max_length=200)
    slas:           list[VelarisSLA] = Field(default_factory=list, max_length=50)
    data_model:     list[VelarisDataField] = Field(default_factory=list, max_length=500)
    source_file:    str = Field(default="", max_length=500)
    confidence:     float = Field(default=0.5, ge=0.0, le=1.0)
    vendor:         str = Field(default="", max_length=100)

    @classmethod
    def from_ai_output(cls, data: dict, source_file: str = "") -> "VelarisBlueprint":
        """Parse AI output dict into a validated blueprint; drop any injected fields."""
        try:
            obj = cls.model_validate(data)
            if source_file:
                object.__setattr__(obj, "source_file", source_file[:500])
            return obj
        except Exception:
            return cls(case_type_name="Unknown", source_file=source_file)


# ── Merged plan (all artifacts combined) ─────────────────────────────────────

class ConflictRecord(BaseModel):
    artifact:      str
    conflict_type: Literal["duplicate_name", "form_collision", "sla_collision"]
    description:   str
    resolution:    Literal["fail", "pending_review"] = "fail"


class MigrationPlan(BaseModel):
    """All blueprints merged into a single plan after Stage 2."""
    blueprints:     list[VelarisBlueprint] = Field(default_factory=list)
    artifact_count: int = 0
    conflicts:      list[ConflictRecord] = Field(default_factory=list)
    vendor:         str = ""
    source_filename: str = ""


# ── Validated plan (post-resolution, ready for Creator) ──────────────────────

class ValidatedPlan(BaseModel):
    """Fully resolved and validated plan — Stage 3 output, Stage 4 input."""
    case_type_name:  str
    version:         str = "1.0.0"
    stages:          list[VelarisStage]
    forms:           list[VelarisForm]
    rules:           list[VelarisRule]
    slas:            list[VelarisSLA]
    data_model:      list[VelarisDataField]
    review_items:    list[str] = Field(default_factory=list)
    source_filename: str = ""
    vendor:          str = ""
    is_valid:        bool = True
    validation_errors: list[str] = Field(default_factory=list)

    def has_conflicts(self) -> bool:
        return bool(self.validation_errors)
