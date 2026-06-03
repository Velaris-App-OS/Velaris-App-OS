"""HELIX ai plugin: ollama."""

class OllamaBackend:
    """ollama implementation of the ai Protocol."""

    def name(self) -> str:
        return "ollama"

    async def health_check(self) -> bool:
        # TODO: Implement actual health check
        return True

    async def initialize(self, config: dict) -> None:
        # TODO: Initialize with config from velaris.yaml
        pass

    async def shutdown(self) -> None:
        # TODO: Graceful shutdown
        pass
