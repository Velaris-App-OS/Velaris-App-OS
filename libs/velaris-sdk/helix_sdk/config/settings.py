"""Configuration loader — reads velaris.yaml and environment variables."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import yaml
from pydantic_settings import BaseSettings


def load_helix_yaml(path: str = "velaris.yaml") -> dict[str, Any]:
    """Load the velaris.yaml configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


class HelixSettings(BaseSettings):
    """Root settings — loaded from env vars + velaris.yaml."""
    service_name: str = "helix"
    environment: str = "development"
    debug: bool = True
    log_level: str = "INFO"
    config_path: str = "velaris.yaml"

    model_config = {"env_prefix": "HELIX_", "env_nested_delimiter": "__"}
