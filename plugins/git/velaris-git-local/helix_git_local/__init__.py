"""HELIX git plugin: local."""

class LocalBackend:
    """local implementation of the git Protocol."""

    def name(self) -> str:
        return "local"

    async def health_check(self) -> bool:
        # TODO: Implement actual health check
        return True

    async def initialize(self, config: dict) -> None:
        # TODO: Initialize with config from velaris.yaml
        pass

    async def shutdown(self) -> None:
        # TODO: Graceful shutdown
        pass
