#!/bin/sh
# Runs the whole test suite (integration tests included) in a throwaway
# container attached to the compose network, so it can reach mongo/redis.
# Requires `docker compose up -d` to be running.
set -e
cd "$(dirname "$0")/.."
# Git Bash on Windows: keep container paths intact and use a Windows host path.
export MSYS_NO_PATHCONV=1
HOST_DIR=$(pwd -W 2>/dev/null || pwd)
docker run --rm \
  --network uva_default \
  -v "$HOST_DIR:/src" -w /src \
  -e MONGO_TEST_URI=mongodb://mongo:27017/uva_test \
  -e REDIS_TEST_URL=redis://redis:6379/15 \
  python:3.12-slim \
  sh -c 'cp -r /src /work && cd /work && pip install -q -e ".[dev]" && pytest -q "$@"' -- "$@"
