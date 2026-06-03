"""Generate a React Native (Expo) mobile app from HELIX case types.

Produces a complete, runnable mobile app scaffold that connects
back to the case-service API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from case_service.codegen.templates import (
    PACKAGE_JSON, APP_JSON, EAS_JSON, APP_TSX, API_CONTEXT,
    CASE_LIST_SCREEN, CASE_DETAIL_SCREEN, CREATE_CASE_SCREEN,
    MY_WORK_SCREEN, SETTINGS_SCREEN, BABEL_CONFIG, TSCONFIG,
    GITIGNORE, README,
)


@dataclass
class AppConfig:
    app_name: str = "Velaris Mobile"
    app_slug: str = "velaris-mobile"
    primary_color: str = "#4ecdc4"
    default_api_url: str = "http://localhost:8200"
    default_tenant: str = "default"
    case_type_ids: list[str] = field(default_factory=list)
    # Store metadata
    app_version: str = "1.0.0"
    ios_bundle_id: str = "com.example.helixmobile"
    android_package: str = "com.example.helixmobile"
    app_description: str = ""

    def __post_init__(self):
        self.app_slug = re.sub(r"[^a-z0-9-]", "-", self.app_slug.lower()).strip("-") or "helix-mobile"
        if not re.match(r"^#[0-9a-fA-F]{6}$", self.primary_color):
            self.primary_color = "#4ecdc4"
        if not re.match(r"^\d+\.\d+\.\d+$", self.app_version):
            self.app_version = "1.0.0"


def render(template: str, config: AppConfig) -> str:
    """Render a template by replacing __PLACEHOLDER__ tokens.

    Uses token replacement instead of .format() to avoid conflicts
    with {} braces in JSX/CSS/JS code.
    """
    # First un-escape the doubled braces (legacy templates use {{ and }})
    result = template.replace("{{", "{").replace("}}", "}")
    # Then replace the named placeholders (which were written as {app_name} etc)
    result = result.replace("{app_name}", config.app_name)
    result = result.replace("{app_slug}", config.app_slug)
    result = result.replace("{primary_color}", config.primary_color)
    result = result.replace("{default_api_url}", config.default_api_url)
    result = result.replace("{default_tenant}", config.default_tenant)
    result = result.replace("{app_version}", config.app_version)
    result = result.replace("{ios_bundle_id}", config.ios_bundle_id)
    result = result.replace("{android_package}", config.android_package)
    result = result.replace("{app_description}", config.app_description or config.app_name)
    return result


def generate_app(config: AppConfig) -> dict[str, str]:
    """Generate all files for the app.

    Returns a dict of path → content.
    """
    files = {
        "package.json": render(PACKAGE_JSON, config),
        "app.json": render(APP_JSON, config),
        "eas.json": render(EAS_JSON, config),
        "App.tsx": render(APP_TSX, config),
        "src/api/ApiContext.tsx": render(API_CONTEXT, config),
        "src/screens/CaseListScreen.tsx": render(CASE_LIST_SCREEN, config),
        "src/screens/CaseDetailScreen.tsx": render(CASE_DETAIL_SCREEN, config),
        "src/screens/CreateCaseScreen.tsx": render(CREATE_CASE_SCREEN, config),
        "src/screens/MyWorkScreen.tsx": render(MY_WORK_SCREEN, config),
        "src/screens/SettingsScreen.tsx": render(SETTINGS_SCREEN, config),
        "babel.config.js": BABEL_CONFIG,
        "tsconfig.json": TSCONFIG,
        ".gitignore": GITIGNORE,
        "README.md": render(README, config),
    }
    return files


def generate_zip(config: AppConfig) -> bytes:
    """Generate the app as a zip file for download."""
    files = generate_app(config)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(f"{config.app_slug}/{path}", content)
    return buf.getvalue()
