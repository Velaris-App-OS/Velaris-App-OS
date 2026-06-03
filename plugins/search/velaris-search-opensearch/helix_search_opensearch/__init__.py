"""HELIX search plugin: opensearch."""

class OpensearchBackend:
    """opensearch implementation of the search Protocol."""

    def name(self) -> str:
        return "opensearch"

    async def health_check(self) -> bool:
        # TODO: Implement actual health check
        return True

    async def initialize(self, config: dict) -> None:
        # TODO: Initialize with config from velaris.yaml
        pass

    async def shutdown(self) -> None:
        # TODO: Graceful shutdown
        pass
