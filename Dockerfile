# syntax=docker/dockerfile:1
# Revel Gifts backend image for Cloud Run (build with --platform linux/amd64).
# Multi-stage uv build following https://docs.astral.sh/uv/guides/integration/docker/
# Settings come only from the environment; no .env file is copied (see .dockerignore).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.10.2 AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/backend/.venv
WORKDIR /app/backend

# Dependencies first (cached layer), without the project itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=backend/uv.lock,target=uv.lock \
    --mount=type=bind,source=backend/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project --no-editable

# Then the project, installed non-editable into the venv.
COPY backend/ /app/backend/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 revel \
    && useradd --system --uid 10001 --gid revel --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin revel

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/backend/.venv/bin:${PATH}"

# backend/ (alembic.ini and migrations/ for the migrate job) and authorization/ (OpenFGA model).
COPY backend/ /app/backend/
COPY authorization/ /app/authorization/
COPY --from=builder /app/backend/.venv /app/backend/.venv

WORKDIR /app/backend
USER revel:revel
EXPOSE 8080

# sh -c so Cloud Run's $PORT expands; exec so uvicorn is PID 1 and receives SIGTERM.
CMD ["sh", "-c", "exec uvicorn revel.app:app --host 0.0.0.0 --port \"${PORT:-8080}\" --proxy-headers --forwarded-allow-ips='*'"]
