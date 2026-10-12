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
RUN pip install ".[dev]"


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


FROM base AS app
# Run as an unprivileged user. /data holds the volumes (SQLite, second brain);
# owning it in the image makes new named volumes writable by that user.
RUN useradd --create-home --uid 10001 uva \
    && mkdir -p /data/sqlite /data/second-brain \
    && chown -R uva:uva /data
USER uva

CMD ["python", "-m", "uva.main"]
