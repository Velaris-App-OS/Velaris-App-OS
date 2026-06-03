"""FastAPI entry point for connect-service."""
from fastapi import FastAPI

app = FastAPI(title="HELIX connect-service", version="0.1.0")

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "connect-service"}
