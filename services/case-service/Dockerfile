# ═══════════════════════════════════════════════════════════════════
# HELIX Case Service — Production Dockerfile
# Multi-stage build for minimal image size
# ═══════════════════════════════════════════════════════════════════

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# No extra system dependencies needed

# ── Build stage ──────────────────────────────────────────────────
FROM base AS builder

COPY pyproject.toml ./
RUN pip install --break-system-packages -e ".[prod]" 2>/dev/null || pip install --break-system-packages -e . || true

COPY . .
RUN pip install --break-system-packages -e .

# ── Runtime stage ────────────────────────────────────────────────
FROM base AS runtime

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# Non-root user
RUN groupadd -r helix && useradd -r -g helix -d /app helix
USER helix

EXPOSE 8200

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')" || exit 1

ENTRYPOINT ["uvicorn", "case_service.main:app", \
    "--host", "0.0.0.0", \
    "--port", "8200", \
    "--workers", "2", \
    "--limit-concurrency", "200", \
    "--timeout-keep-alive", "30"]
