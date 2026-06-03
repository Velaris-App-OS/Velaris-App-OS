"""HxBridge — ConnectorProtocol and self-registering connector registry.

To add a new connector type:
  1. Create case_service/hxbridge/connectors/my_connector.py
  2. Implement the ConnectorProtocol
  3. Decorate the class with @register_connector("my_type")
  4. Import the module in connectors/__init__.py
  Zero other changes needed.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ── Registry ──────────────────────────────────────────────────────────────────

CONNECTOR_REGISTRY: dict[str, type] = {}


def register_connector(connector_type: str):
    """Class decorator that registers a connector implementation."""
    def decorator(cls):
        CONNECTOR_REGISTRY[connector_type] = cls
        logger.debug("Registered connector: %s → %s", connector_type, cls.__name__)
        return cls
    return decorator


def get_connector(connector_type: str, config: dict, credentials: dict):
    """Instantiate a connector by type. Raises KeyError if unknown."""
    cls = CONNECTOR_REGISTRY.get(connector_type)
    if cls is None:
        raise KeyError(f"Unknown connector type: '{connector_type}'. "
                       f"Available: {sorted(CONNECTOR_REGISTRY)}")
    return cls(config=config, credentials=credentials)


def list_connector_types() -> list[dict]:
    """Return metadata for all registered connector types."""
    result = []
    for ctype, cls in CONNECTOR_REGISTRY.items():
        result.append({
            "connector_type": ctype,
            "display_name":   getattr(cls, "display_name", ctype),
            "description":    getattr(cls, "description", ""),
            "config_schema":  getattr(cls, "config_schema", {}),
            "credential_schema": getattr(cls, "credential_schema", {}),
        })
    return result


# ── Protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class ConnectorProtocol(Protocol):
    """Every HxBridge connector implements this interface.

    Class-level attributes (for Studio metadata):
        display_name:       Human-readable name shown in Studio
        description:        One-line description
        config_schema:      JSONSchema for config fields (non-secret)
        credential_schema:  JSONSchema for credential fields (secret, encrypted)
    """

    display_name:      str
    description:       str
    config_schema:     dict
    credential_schema: dict

    def __init__(self, config: dict, credentials: dict) -> None: ...

    async def execute(self, input_data: dict) -> dict:
        """Run the connector. Returns output dict. Raises on unrecoverable failure."""
        ...

    async def test(self) -> bool:
        """Validate credentials and connectivity. Returns True if healthy."""
        ...
