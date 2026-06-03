"""HELIX IR: Form definitions.

Declarative form layouts bound to data models.  The runtime renders
these via the Studio frontend or any channel adapter (mobile, email,
chatbot).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class FormFieldWidget(enum.Enum):
    """Available widget types for form fields."""

    TEXT_INPUT = "text_input"
    TEXT_AREA = "text_area"
    RICH_TEXT = "rich_text"
    NUMBER_INPUT = "number_input"
    DATE_PICKER = "date_picker"
    DATETIME_PICKER = "datetime_picker"
    DROPDOWN = "dropdown"
    RADIO_GROUP = "radio_group"
    CHECKBOX = "checkbox"
    CHECKBOX_GROUP = "checkbox_group"
    TOGGLE = "toggle"
    FILE_UPLOAD = "file_upload"
    SIGNATURE = "signature"
    AUTOCOMPLETE = "autocomplete"
    CASCADING_DROPDOWN = "cascading_dropdown"
    TABLE = "table"
    REPEATING_GROUP = "repeating_group"
    HIDDEN = "hidden"
    LABEL = "label"
    SECTION_HEADER = "section_header"
    DIVIDER = "divider"


class FormAction(enum.Enum):
    """Actions a form can trigger."""

    SUBMIT = "submit"
    SAVE_DRAFT = "save_draft"
    CANCEL = "cancel"
    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"
    CUSTOM = "custom"


class VisibilityCondition(enum.Enum):
    """When a field or section is visible."""

    ALWAYS = "always"
    WHEN_TRUE = "when_true"
    WHEN_ROLE = "when_role"
    WHEN_STAGE = "when_stage"
    NEVER = "never"


@dataclass(frozen=True)
class FormField:
    """A single field in a form layout."""

    id: str
    data_field_id: str
    widget: FormFieldWidget
    label: str | None = None
    placeholder: str = ""
    help_text: str = ""
    visibility: VisibilityCondition = VisibilityCondition.ALWAYS
    visibility_expression: str | None = None
    editable_expression: str | None = None
    column_span: int = 12
    row_order: int = 0
    section_id: str | None = None
    validations: list[str] = field(default_factory=list)
    on_change_rule_id: str | None = None
    ui_hints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FormSection:
    """Logical grouping of fields."""

    id: str
    title: str
    description: str = ""
    collapsible: bool = False
    collapsed_by_default: bool = False
    visibility: VisibilityCondition = VisibilityCondition.ALWAYS
    visibility_expression: str | None = None
    order: int = 0
    columns: int = 2


@dataclass(frozen=True)
class FormActionButton:
    """An action button rendered on the form."""

    action: FormAction
    label: str
    variant: str = "primary"
    confirmation_message: str | None = None
    visible_expression: str | None = None
    enabled_expression: str | None = None
    custom_action_id: str | None = None


@dataclass
class FormDefinition:
    """Complete form layout bound to a data model.

    Forms are reusable across steps and case types.
    """

    id: str
    name: str
    version: str
    data_model_id: str
    sections: list[FormSection] = field(default_factory=list)
    fields: list[FormField] = field(default_factory=list)
    actions: list[FormActionButton] = field(default_factory=list)
    description: str = ""
    layout: str = "vertical"
    read_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
