"""Phase 18 tests — React Native App Codegen.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import io
import zipfile
import pytest


class TestAppConfig:
    def test_default_config(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig()
        assert c.app_name == "HELIX Mobile"
        assert c.app_slug == "helix-mobile"

    def test_slug_sanitization(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig(app_slug="My App!")
        # Should be sanitized
        assert "!" not in c.app_slug
        assert " " not in c.app_slug

    def test_empty_slug_fallback(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig(app_slug="@@@")
        assert c.app_slug == "helix-mobile"

    def test_color_validation(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig(primary_color="not-a-color")
        assert c.primary_color == "#4ecdc4"

    def test_valid_color_preserved(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig(primary_color="#ff6b35")
        assert c.primary_color == "#ff6b35"


class TestGenerator:
    def test_generate_app_files(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(app_name="Test App"))
        assert "package.json" in files
        assert "App.tsx" in files
        assert "src/api/ApiContext.tsx" in files
        assert "src/screens/CaseListScreen.tsx" in files

    def test_app_name_in_generated_files(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(app_name="MyCustomApp", app_slug="my-custom-app"))
        # App name should appear in package.json
        assert "my-custom-app" in files["package.json"]
        # App name should appear in app.json
        assert "MyCustomApp" in files["app.json"]

    def test_primary_color_in_generated(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(primary_color="#ff6b35"))
        # Color should be used in various screens
        assert "#ff6b35" in files["App.tsx"]
        assert "#ff6b35" in files["src/screens/CaseListScreen.tsx"]

    def test_api_url_in_context(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(default_api_url="https://helix.example.com"))
        assert "https://helix.example.com" in files["src/api/ApiContext.tsx"]

    def test_tenant_in_context(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(default_tenant="acme"))
        assert "acme" in files["src/api/ApiContext.tsx"]


class TestZipGeneration:
    def test_generate_zip(self):
        from case_service.codegen.generator import AppConfig, generate_zip
        zip_data = generate_zip(AppConfig(app_slug="zip-test"))
        assert isinstance(zip_data, bytes)
        assert len(zip_data) > 0

    def test_zip_contains_files(self):
        from case_service.codegen.generator import AppConfig, generate_zip
        zip_data = generate_zip(AppConfig(app_slug="zip-test"))
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            names = zf.namelist()
            assert any("package.json" in n for n in names)
            assert any("App.tsx" in n for n in names)
            assert any("CaseListScreen.tsx" in n for n in names)

    def test_zip_has_app_slug_prefix(self):
        from case_service.codegen.generator import AppConfig, generate_zip
        zip_data = generate_zip(AppConfig(app_slug="my-app"))
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            names = zf.namelist()
            assert all(n.startswith("my-app/") for n in names)


class TestCodegenAPI:
    async def test_list_platforms(self, client):
        resp = await client.get("/api/v1/codegen/platforms")
        assert resp.status_code == 200
        platforms = resp.json()["platforms"]
        assert len(platforms) >= 1
        assert platforms[0]["id"] == "react-native-expo"

    async def test_preview(self, client):
        resp = await client.post("/api/v1/codegen/preview", json={
            "app_name": "Test App",
            "app_slug": "test-app",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_count"] > 0
        assert "package.json" in data["files"]

    async def test_preview_with_color(self, client):
        resp = await client.post("/api/v1/codegen/preview", json={
            "app_name": "Colored App",
            "primary_color": "#ff00ff",
        })
        assert resp.status_code == 200
        assert "#ff00ff" in resp.json()["files"]["App.tsx"]

    async def test_generate_zip_endpoint(self, client):
        resp = await client.post("/api/v1/codegen/generate", json={
            "app_name": "Download Test",
            "app_slug": "download-test",
        })
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "download-test.zip" in resp.headers.get("content-disposition", "")

        # Verify it's a valid zip
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            assert len(zf.namelist()) > 0

    async def test_empty_app_name_rejected(self, client):
        resp = await client.post("/api/v1/codegen/preview", json={
            "app_name": "",
        })
        # Pydantic validation
        assert resp.status_code == 422
