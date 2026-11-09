"""Protocol interface for generator backends.

Implement this Protocol and register via entry_points:

    [project.entry-points."helix.generator"]
    my_impl = "my_package:MyImplementation"

Then install: pip install my-package
It appears in Velaris settings automatically.
"""

from __future__ import annotations
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GeneratorBackend(Protocol):
    """generator backend interface. All methods must be implemented."""

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

# The name `velaris_sdk.protocols` imports. This module defines it as
# `GeneratorBackend`, so `from velaris_sdk.protocols import ...` raised ImportError
# on the FIRST mismatch and took every later protocol with it — which is
# how the engine came to run with Temporal permanently "unavailable".
# Both names are kept: nothing that imports `GeneratorBackend` today changes.
CodeGenerator = GeneratorBackend
