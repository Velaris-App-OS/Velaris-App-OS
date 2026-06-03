"""Protocol interface for scanner backends.

Implement this Protocol and register via entry_points:

    [project.entry-points."helix.scanner"]
    my_impl = "my_package:MyImplementation"

Then install: pip install my-package
It appears in HELIX settings automatically.
"""

from __future__ import annotations
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ScannerBackend(Protocol):
    """scanner backend interface. All methods must be implemented."""

    def name(self) -> str:
        """Human-readable name for display in settings."""
        ...

    async def health_check(self) -> bool:
        """Return True if the backend is reachable and healthy."""
        ...

    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize the backend with configuration from velaris.yaml."""
        ...

    async def shutdown(self) -> None:
        """Gracefully shut down the backend."""
        ...
