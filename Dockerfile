# syntax=docker/dockerfile:1
# Revel Gifts backend image for Cloud Run (build with --platform linux/amd64).
# Multi-stage uv build following https://docs.astral.sh/uv/guides/integration/docker/
# Settings come only from the environment; no .env file is copied (see .dockerignore).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:22.23.4-slim

FROM ghcr.io/astral-sh/uv:0.10.2 AS uv

# Human UI (F8): static Vite build. Runs on the build host's platform; output is plain files.
# pnpm version comes from frontend/package.json "packageManager" via corepack.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS frontend
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    PNPM_HOME=/pnpm
WORKDIR /app/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/.npmrc ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir /pnpm/store
COPY frontend/ ./
RUN pnpm build

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
# Built UI, root-owned and read-only for the revel user; served at /app/ by revel.api.ui_static.
COPY --from=frontend /app/frontend/dist /app/frontend/dist
ENV FRONTEND_DIST_DIR=/app/frontend/dist

WORKDIR /app/backend
USER revel:revel
EXPOSE 8080

# sh -c so Cloud Run's $PORT expands; exec so uvicorn is PID 1 and receives SIGTERM.
CMD ["sh", "-c", "exec uvicorn revel.app:app --host 0.0.0.0 --port \"${PORT:-8080}\" --proxy-headers --forwarded-allow-ips='*'"]
