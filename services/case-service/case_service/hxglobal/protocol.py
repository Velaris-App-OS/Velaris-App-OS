"""HxGlobal — RegionProtocol: pluggable region adapter interface."""
from __future__ import annotations

from abc import ABC, abstractmethod

_REGISTRY: dict[str, type["RegionProtocol"]] = {}


def register_region(provider: str):
    def decorator(cls):
        _REGISTRY[provider] = cls
        return cls
    return decorator


def get_region_adapter(provider: str, config: dict) -> "RegionProtocol":
    cls = _REGISTRY.get(provider)
    if cls is None:
        raise ValueError(f"Unknown region provider: {provider!r}. Available: {list(_REGISTRY)}")
    return cls(config)


class RegionProtocol(ABC):
    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def ping(self) -> dict:
        """Return {ok: bool, latency_ms: int, message: str}."""

    @abstractmethod
    def active_case_count(self) -> int:
        """Return approximate active case count in this region."""

    @abstractmethod
    def replication_lag_ms(self) -> int | None:
        """Return replication lag in ms, or None if primary."""
