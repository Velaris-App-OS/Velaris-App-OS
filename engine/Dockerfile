FROM python:3.12-slim AS base
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY libs/ /app/libs/
COPY engine/ /app/engine/
COPY pyproject.toml uv.lock /app/
COPY velaris.yaml /app/
RUN uv sync --frozen --no-dev --package velaris-engine
EXPOSE 8100
CMD ["uv", "run", "uvicorn", "helix_engine.main:app", "--host", "0.0.0.0", "--port", "8100"]
