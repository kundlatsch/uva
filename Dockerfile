FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# git: the bot commits changes to its own repository (mounted at /repo).
# ffmpeg: converts Telegram voice notes (OGG/Opus) for transcription.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

COPY pyproject.toml ./
COPY src ./src
# Dev extras (pytest) are needed at runtime: self-coded plugins are tested in the worker.
# The mcp extra is the gateway's SDK (also used by the tests, which run in the app image).
RUN pip install ".[dev,mcp]"


# Worker for /projeto: Claude Code builds NEW projects in /data/projects.
# It mounts only that folder (no bot repository, no second brain) and starts as
# root just to run Claude Code as the unprivileged "builder" user, who can't
# read this process's environment (the bot's secrets). Claude Code gets a closed
# list of permissions (uva/projects/runner.py), never full permissions.
FROM node:22.20.0-bookworm-slim AS node

FROM base AS projects
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g @anthropic-ai/claude-code@2.1.283 \
    && npm cache clean --force \
    && useradd --create-home --uid 10002 builder \
    && mkdir -p /data/projects \
    && chown builder:builder /data/projects
CMD ["celery", "-A", "uva.tasks.app", "worker", "-Q", "projects", "--concurrency=1", "--hostname=projects@%h", "--loglevel=INFO"]


# MCP gateway: runs the MCP servers (third-party code) away from the bot. Only
# data/mcp.json, the vault (read-only) and mcp.env reach it; see docker-compose.yml.
# Local server packages are installed here at pinned versions, never fetched at
# runtime (no npx -y).
FROM base AS mcp
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm     && npm install -g @modelcontextprotocol/server-filesystem@2026.8.31     && npm cache clean --force     && rm /usr/local/bin/npm     && useradd --no-create-home --uid 10030 mcp     && mkdir -p /data/mcp     && chown mcp:mcp /data/mcp
USER mcp
CMD ["python", "-m", "uva.mcp.gateway"]


# /sh and /py: the bot's own folder /uva. Only /uva and /snapshots are mounted
# (no .env, repository, second brain or projects); see docker-compose.yml for the
# read-only filesystem, dropped capabilities and resource limits. The executor
# starts as root just to run every command as the unprivileged "uva" user.
FROM base AS shell
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates unzip jq procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --home-dir /uva --no-create-home --uid 10020 --shell /bin/bash uva \
    && mkdir -p /uva /snapshots
CMD ["python", "-m", "uva.shell.executor"]


FROM base AS app
# Run as an unprivileged user. /data holds the volumes (SQLite, second brain);
# owning it in the image makes new named volumes writable by that user.
RUN useradd --create-home --uid 10001 uva \
    && mkdir -p /data/sqlite /data/second-brain \
    && chown -R uva:uva /data
USER uva

CMD ["python", "-m", "uva.main"]
