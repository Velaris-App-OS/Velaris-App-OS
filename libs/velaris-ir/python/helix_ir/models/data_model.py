"""HELIX IR: Data Model definitions.

Typed schemas for case data, form backing models, rule inputs/outputs,
and any structured data flowing through the system.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class FieldType(enum.Enum):
    """Supported field data types."""

    STRING = "string"
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    DURATION = "duration"
    ENUM = "enum"
    REFERENCE = "reference"
    LIST = "list"
    MAP = "map"
    EMBEDDED = "embedded"
    ATTACHMENT = "attachment"
    CURRENCY = "currency"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    LOCATION = "location"


class ValidationRule(enum.Enum):
    """Built-in validation rule types."""

    REQUIRED = "required"
    MIN_LENGTH = "min_length"
    MAX_LENGTH = "max_length"
    MIN_VALUE = "min_value"
    MAX_VALUE = "max_value"
    PATTERN = "pattern"
    UNIQUE = "unique"
    CUSTOM = "custom"


@dataclass(frozen=True)
class FieldValidation:
    """A single validation constraint on a field."""

    rule: ValidationRule
    value: Any = None
    message: str = ""


@dataclass(frozen=True)
class EnumOption:
    """One allowed value within an enum-typed field."""

    value: str
    label: str
    description: str = ""
    icon: str | None = None
    deprecated: bool = False


@dataclass(frozen=True)
class FieldDefinition:
    """Schema definition for a single data field."""

    id: str
    name: str
    field_type: FieldType
    description: str = ""
    default_value: Any = None
    validations: list[FieldValidation] = field(default_factory=list)
    enum_options: list[EnumOption] = field(default_factory=list)
    reference_model_id: str | None = None
    list_item_type: FieldType | None = None
    list_item_model_id: str | None = None
    searchable: bool = False
    indexed: bool = False
    encrypted: bool = False
    pii: bool = False
    read_only: bool = False
    computed: bool = False
    expression: str | None = None
    ui_hints: dict[str, Any] = field(default_factory=dict)


@dataclass
class DataModelDefinition:
    """A typed schema for structured data.

    Used by case data, form backing models, rule inputs/outputs,
    and API request/response bodies.
    """

    id: str
    name: str
    version: str
    fields: list[FieldDefinition] = field(default_factory=list)
    extends: str | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
