# syntax=docker/dockerfile:1
#
# Agent Backend — production image.
#
#   docker build -t fibrecase-agent-backend .
#   docker run --rm --env-file .env --user "$(id -u):$(id -g)" -v ./data:/app/data fibrecase-agent-backend
#
# The app only makes *outbound* connections (Telegram long polling + the
# OpenAI-compatible API). There is nothing to expose inbound by default, so no
# EXPOSE. (The one conditional inbound listener — the MCP user-level OAuth
# callback, phase 4.x — is published by the default-commented `ports:` entry in
# docker-compose.yaml, not baked into the image.)
# Config is the SAME `.env` the local `uv run fibrecase-agent-backend` uses
# (see .env.example). It is passed at runtime and never baked into the image —
# .dockerignore keeps .env, .venv/ and data/ out of the build.

FROM python:3.14-slim

# ---------------------------------------------------------------------------
# base tools layer — kept FIRST so source edits never bust this cache
# ---------------------------------------------------------------------------
# A small set of commonly-expected command-line tools on top of the slim image,
# plus ca-certificates (system TLS) and the unprivileged "agent" user.
#
# Putting the `apt-get` install + `useradd` in the very first layer means this
# step is rebuilt only when THIS list changes — not on every source edit.
# Previously the apt step ran *after* `COPY . .`, so any code change invalidated
# it (and re-ran the slow apt install on every build). `--no-install-recommends`
# keeps the image slim. The tools are for interactive use and for the opt-in
# `exec` shell tool; they are NOT required by the app itself (the app is pure
# Python and needs only the venv below).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        less \
        nano \
        file \
        tree \
        unzip \
        zip \
        rsync \
        iproute2 \
        dnsutils \
        netcat-openbsd \
        openssh-client \
        procps \
        htop \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --user-group --uid 1000 --shell /usr/sbin/nologin agent

# ---------------------------------------------------------------------------
# project environment — uv + the exact locked deps
# ---------------------------------------------------------------------------
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
# Small hardening: no leftover apt cache (already pruned in the tools layer
# above), run as an unprivileged user.
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# /app/data holds the SQLite DB. It's the only path the app writes to at
# runtime, so only it needs to be owned by the agent user; the rest of /app
# (source, config, /opt/venv) is just read, and root's COPY/uv output is
# world-readable by default.
#
# For a *named volume* this pre-creates the dir with the right owner. For a
# *bind mount* (compose mounts ./data over it) the host dir's own ownership
# wins, so run the container as the host uid (compose `user:` / see README).
RUN mkdir -p /app/data && chown agent:agent /app/data
USER agent

# Long polling only needs to reach api.telegram.org + the LLM host. No EXPOSE.
# (OAuth callback port, if enabled, is published by uncommenting the `ports:`
# entry in docker-compose.yaml.)
CMD ["fibrecase-agent-backend"]
