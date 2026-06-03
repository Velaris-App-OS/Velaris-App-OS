"""HxSync — SyncDestinationProtocol: pluggable adapter interface.

Each destination type (duckdb, bigquery, snowflake, kafka, …) implements
this protocol.  The pipeline calls only these four methods.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

_REGISTRY: dict[str, type["SyncDestinationProtocol"]] = {}


def register_destination(dest_type: str):
    def decorator(cls):
        _REGISTRY[dest_type] = cls
        return cls
    return decorator


def get_destination(dest_type: str, config: dict) -> "SyncDestinationProtocol":
    cls = _REGISTRY.get(dest_type)
    if cls is None:
        raise ValueError(f"Unknown destination type: {dest_type!r}. Available: {list(_REGISTRY)}")
    return cls(config)


class SyncDestinationProtocol(ABC):
    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def health_check(self) -> dict:
        """Return {ok: bool, message: str, latency_ms: int}."""

    @abstractmethod
    def ensure_schema(self, table: str, columns: list[dict]) -> None:
        """Create or ALTER the target table to match columns."""

    @abstractmethod
    def push_rows(self, table: str, rows: list[dict]) -> int:
        """Upsert rows. Return count of rows written."""

    def schema_info(self) -> dict:
        return {"type": self.__class__.__name__, "config_keys": list(self.config)}
