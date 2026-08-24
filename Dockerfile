# syntax=docker/dockerfile:1
#
# Agent Backend — production image.
#
#   docker build -t fibrecase-agent-backend .
#   docker run --rm --env-file .env.docker -v agent-data:/app/data fibrecase-agent-backend
#
# The app only makes *outbound* connections (Telegram long polling + the
# OpenAI-compatible API). There is nothing to expose inbound, so no EXPOSE.
# Config comes from the environment (or a .env in the working dir) — see
# .env.docker.example. Secrets must never be baked into the image.
#
# Build & run are verified to be equivalent to `uv sync` + the console script;
# `.dockerignore` keeps .venv/, data/, .env* and tests/ out of the build.

FROM python:3.14-slim

# uv (from PyPI, pinned to the lockfile generator) installs Python *and* the
# exact locked deps. UV_PROJECT_ENVIRONMENT pins uv to a dedicated venv, so the
# interpreter never clashes with the system one and the console script lands in
# /opt/venv/bin. (uv's `sync` ignores --python for venv selection — it honors
# UV_PROJECT_ENVIRONMENT instead.)
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir uv==0.12.1

WORKDIR /app
# Metadata first: creates the venv and installs only the locked third-party
# deps. Kept before the source copy so edits to code don't bust this cache.
COPY pyproject.toml uv.lock .python-version ./
RUN uv venv && uv sync --frozen --no-dev --no-install-project

# Source + config, then install the project itself (builds & links it).
COPY . .
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
# Small hardening: no leftover apt cache, run as an unprivileged user.
# --user-group guarantees the "agent" group exists (independent of the image's
# /etc/default/useradd) so the chown below never fails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --user-group --uid 10001 --shell /usr/sbin/nologin agent

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# /app/data holds the SQLite DB. It's the only path the app writes to at
# runtime, so only it needs to be owned by the agent user; the rest of /app
# (source, config, /opt/venv) is just read, and root's COPY/uv output is
# world-readable by default.
RUN mkdir -p /app/data && chown agent:agent /app/data
USER agent

# Long polling only needs to reach api.telegram.org + the LLM host. No ports.
CMD ["fibrecase-agent-backend"]
