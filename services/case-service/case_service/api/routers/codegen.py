"""App codegen API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from case_service.auth.dependencies import require_role
from case_service.codegen.generator import AppConfig, generate_app, generate_zip

router = APIRouter(prefix="/codegen", tags=["codegen"], dependencies=[Depends(require_role("designer", "admin"))])


class AppGenerateRequest(BaseModel):
    app_name: str = Field(default="Velaris Mobile", min_length=1, max_length=100)
    app_slug: str = Field(default="velaris-mobile")
    primary_color: str = "#4ecdc4"
    default_api_url: str = "http://localhost:8200"
    default_tenant: str = "default"
    case_type_ids: list[str] = Field(default_factory=list)
    app_version: str = "1.0.0"
    ios_bundle_id: str = "com.example.helixmobile"
    android_package: str = "com.example.helixmobile"
    app_description: str = ""


class AppGeneratePreview(BaseModel):
    files: dict[str, str]
    file_count: int
    total_size_bytes: int


@router.get("/platforms")
async def list_supported_platforms():
    """List platforms the codegen supports."""
    return {
        "platforms": [
            {
                "id": "react-native-expo",
                "name": "React Native (Expo)",
                "description": "Cross-platform mobile app for iOS, Android, and Web",
                "features": ["Case list", "Case detail", "Create case", "My work", "Settings"],
            },
        ]
    }


@router.post("/preview", response_model=AppGeneratePreview)
async def preview_app(body: AppGenerateRequest):
    """Preview generated files without downloading."""
    config = AppConfig(**body.model_dump())
    files = generate_app(config)
    return AppGeneratePreview(
        files=files,
        file_count=len(files),
        total_size_bytes=sum(len(c.encode("utf-8")) for c in files.values()),
    )


@router.post("/generate")
async def generate_app_endpoint(body: AppGenerateRequest):
    """Generate a downloadable ZIP containing the complete app."""
    config = AppConfig(**body.model_dump())
    data = generate_zip(config)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{config.app_slug}.zip"'},
    )
