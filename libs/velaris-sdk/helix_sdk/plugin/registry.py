"""Universal plugin registry. Discovers plugins via entry_points.

Usage:
    registry = PluginRegistry("helix.git")
    backends = registry.list_all()  # {"github": GitHubBackend, "local": LocalBackend}
    backend = registry.get("github")
    instance = backend()
    await instance.initialize(config)
"""
from __future__ import annotations
import importlib.metadata
from typing import Any
import structlog

logger = structlog.get_logger()


class PluginRegistry:
    def __init__(self, group: str) -> None:
        self.group = group
        self._plugins: dict[str, Any] = {}
        self._discovered = False

    def discover(self) -> None:
        if self._discovered:
            return
        eps = importlib.metadata.entry_points()
        group_eps = eps.select(group=self.group) if hasattr(eps, "select") else eps.get(self.group, [])
        for ep in group_eps:
            try:
                self._plugins[ep.name] = ep.load()
                logger.info("plugin_discovered", group=self.group, name=ep.name)
            except Exception as exc:
                logger.error("plugin_load_failed", group=self.group, name=ep.name, error=str(exc))
        self._discovered = True

    def get(self, name: str) -> Any | None:
        self.discover()
        return self._plugins.get(name)

    def list_all(self) -> dict[str, Any]:
        self.discover()
        return dict(self._plugins)

    def register(self, name: str, plugin: Any) -> None:
        self._plugins[name] = plugin
