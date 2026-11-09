# ═══════════════════════════════════════════════════════════════════
# Velaris hxgraph-service — extracted from case-service (roadmap 13c-e)
# ═══════════════════════════════════════════════════════════════════

FROM python:3.12.15-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ── Build stage ──────────────────────────────────────────────────
FROM base AS builder
# Database driver. The DB SDK is a BYO model: asyncpg (PostgreSQL) is a core
# dependency, every other backend is an extra. Build with
#     --build-arg DB_EXTRAS="[mysql]"
# for a MySQL or MariaDB install. Empty by default, so the image built
# today is byte-for-byte the image built yesterday.
ARG DB_EXTRAS=""

# Build context is the REPOSITORY ROOT: this service depends on
# `velaris-shared` (services/_shared), a path dependency a narrower context
# cannot see. Install it first so pip resolves the requirement from what is
# already installed rather than reaching for an index.
COPY services/_shared /app/services/_shared
RUN pip install --break-system-packages -e /app/services/_shared

COPY services/hxgraph-service /app/services/hxgraph-service
RUN pip install --break-system-packages -e "/app/services/hxgraph-service${DB_EXTRAS}"

# ── Runtime stage ────────────────────────────────────────────────
FROM base AS runtime

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# A NUMERIC uid, not just a name. Kubernetes cannot verify that a named user is
# non-root — `runAsNonRoot: true` fails the pod with "image has non-numeric user"
# before the container ever starts — so the uid is fixed here and the image says
# so. Found by deploying the chart with pod hardening on (2026-09-17).
RUN groupadd -r -g 10001 velaris && useradd -r -u 10001 -g velaris -d /app velaris
USER 10001:10001

EXPOSE 8203

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8203/health')" || exit 1

# Same reasoning as case-service: uvicorn honours X-Forwarded-Proto only from a
# trusted source, and its default trust list is loopback. In a bridge network
# the gateway arrives on a container address, so set FORWARDED_ALLOW_IPS to it.
ENV FORWARDED_ALLOW_IPS=127.0.0.1

ENTRYPOINT ["uvicorn", "hxgraph_service.main:app", \
    "--host", "0.0.0.0", \
    "--port", "8203", \
    "--proxy-headers", \
    "--workers", "2", \
    "--limit-concurrency", "200", \
    "--timeout-keep-alive", "30"]
