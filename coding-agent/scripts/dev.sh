#!/usr/bin/env bash
# Start the two daemon hosts and the supervisor (which owns the ReAct backend).
# From the repo: coding-agent/scripts/dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="${HOME}/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:${PATH}"

mkdir -p data/cow

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r backend/requirements.txt
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Wrote .env from .env.example. Set CURSOR_API_KEY, then run scripts/dev.sh again." >&2
  exit 1
fi

if ! grep -Eq '^CURSOR_API_KEY=.+' .env; then
  echo "Set CURSOR_API_KEY in coding-agent/.env before starting." >&2
  exit 1
fi

docker compose up -d --build
exec .venv/bin/python -m backend.supervisor
