# ═══════════════════════════════════════════════════════════════════
# Velaris Case Service — Production Dockerfile
# Multi-stage build for minimal image size
# ═══════════════════════════════════════════════════════════════════

FROM python:3.12.15-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# No extra system dependencies needed

# ── Build stage ──────────────────────────────────────────────────
FROM base AS builder
# Database driver. The DB SDK is a BYO model: asyncpg (PostgreSQL) is a core
# dependency, every other backend is an extra. Build with
#     --build-arg DB_EXTRAS="[mysql]"
# for a MySQL or MariaDB install. Empty by default, so the image built
# today is byte-for-byte the image built yesterday.
ARG DB_EXTRAS=""

# Build context is the REPOSITORY ROOT, not services/case-service — since 13b
# the service depends on `velaris-shared` (services/_shared), a path dependency
# that a narrower context cannot see. Install the shared layer first so its own
# wheel set is cached independently of the service.
COPY services/_shared /app/services/_shared
RUN pip install --break-system-packages -e /app/services/_shared

# HxArbiter's evaluator, a workspace library rather than a PyPI package.
# 25 modules import `hxarbiter_core` — `agents/attribution.py` does it at
# IMPORT time, which `api/routers/hxnexus.py` pulls in — so without it the
# container does not start at all: `ModuleNotFoundError: No module named
# 'hxarbiter_core'` before the first request. It was never copied in, so every
# image built since HxArbiter shipped was dead on arrival; found by deploying
# the Helm chart to a cluster on 2026-09-17, because the host install runs from
# the workspace virtualenv where it is present as an editable package.
COPY libs/hxarbiter-core /app/libs/hxarbiter-core
RUN pip install --break-system-packages -e /app/libs/hxarbiter-core

COPY services/case-service/pyproject.toml /app/services/case-service/
RUN pip install --break-system-packages -e "/app/services/case-service[prod]" 2>/dev/null \
    || pip install --break-system-packages -e /app/services/case-service || true

COPY services/case-service /app/services/case-service
RUN pip install --break-system-packages -e "/app/services/case-service${DB_EXTRAS}"

# ── Runtime stage ────────────────────────────────────────────────
FROM base AS runtime

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# Non-root user
# A NUMERIC uid, not just a name. Kubernetes cannot verify that a named user is
# non-root — `runAsNonRoot: true` fails the pod with "image has non-numeric user"
# before the container ever starts — so the uid is fixed here and the image says
# so. Found by deploying the chart with pod hardening on (2026-09-17).
RUN groupadd -r -g 10001 helix && useradd -r -u 10001 -g helix -d /app helix
# The default document store is `./data/velaris-docs` — /app/data at this
# WORKDIR — and /app is root-owned, so an upload by the non-root user failed
# with a permission error the API reported as a 500. Create it and hand it to
# the user (2026-09-17).
RUN mkdir -p /app/data/velaris-docs && chown -R 10001:10001 /app/data

USER 10001:10001

EXPOSE 8200

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')" || exit 1

# Behind a reverse proxy, X-Forwarded-Proto is the only thing that tells the app
# it was reached over TLS — without it every server-built absolute URL (the SCIM
# base in `identity/scim.py`, the signed webhook URL in `api/routers/channels.py`)
# comes out as http:// on an https deployment. uvicorn only honours the header
# from a trusted source, and its default trust list is 127.0.0.1 — true for the
# host deployment, where the gateway is network_mode: host, but NOT here: in a
# bridge network the proxy arrives on a container address. Set
# FORWARDED_ALLOW_IPS to the gateway's address or network when running that way;
# it stays loopback-only by default so an untrusted client cannot claim https.
ENV FORWARDED_ALLOW_IPS=127.0.0.1

ENTRYPOINT ["uvicorn", "case_service.main:app", \
    "--host", "0.0.0.0", \
    "--port", "8200", \
    "--proxy-headers", \
    "--workers", "2", \
    "--limit-concurrency", "200", \
    "--timeout-keep-alive", "30"]
