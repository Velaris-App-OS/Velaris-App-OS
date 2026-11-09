FROM python:3.12.15-slim AS base
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY libs/ /app/libs/
COPY engine/ /app/engine/
# velaris-engine depends on velaris-shared, a workspace member: uv.lock
# names it as an editable path, so `uv sync --frozen` cannot build an
# installation plan without the directory actually being here.
COPY services/_shared/ /app/services/_shared/
COPY pyproject.toml uv.lock /app/
COPY velaris.yaml /app/
# Database driver, as above. The engine declares no mysql extra and adding
# one would invalidate uv.lock, which --frozen rejects, so the drivers go
# in beside the synced environment instead.
ARG DB_EXTRAS=""
RUN uv sync --frozen --no-dev --package velaris-engine \
 && if [ -n "$DB_EXTRAS" ]; then uv pip install aiomysql pymysql; fi
# Non-root, like every other Velaris image (case-service `helix`, the extracted
# services `velaris`). The engine ran as ROOT until 2026-09-17 — the one image
# that did — so the Helm chart's `runAsNonRoot` could not be switched on without
# the engine pods refusing to start. `/app` is owned by the user because uv
# keeps its virtualenv there and writes a lock/cache on start.
# A NUMERIC uid, not just a name. Kubernetes cannot verify that a named user is
# non-root — `runAsNonRoot: true` fails the pod with "image has non-numeric user"
# before the container ever starts — so the uid is fixed here and the image says
# so. Found by deploying the chart with pod hardening on (2026-09-17).
RUN groupadd -r -g 10001 velaris && useradd -r -u 10001 -g velaris -d /app velaris \
 && chown -R velaris:velaris /app
USER 10001:10001

EXPOSE 8100
CMD ["uv", "run", "uvicorn", "helix_engine.main:app", "--host", "0.0.0.0", "--port", "8100"]
