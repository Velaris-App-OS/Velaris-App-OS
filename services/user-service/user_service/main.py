"""FastAPI entry point for user-service."""
from fastapi import FastAPI
from helix_sdk.config.brand import BRAND_NAME

app = FastAPI(title=f"{BRAND_NAME} user-service", version="0.1.0")

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "user-service"}
