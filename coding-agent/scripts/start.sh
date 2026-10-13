#!/usr/bin/env bash
# Start the daemon hosts, the supervisor, and the backend that serves the UI.
# From the repo: ./scripts/start.sh
# Ctrl+C stops the backend. The two Docker hosts keep running.
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="${HOME}/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:${PATH}"

mkdir -p data/cow data/imports

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r backend/requirements.txt
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Wrote .env from .env.example. Set CURSOR_API_KEY, then run ./scripts/start.sh again." >&2
  exit 1
fi

if ! grep -Eq '^CURSOR_API_KEY=.+' .env; then
  echo "Set CURSOR_API_KEY in .env before starting." >&2
  exit 1
fi

backend_port="$(grep -E '^BACKEND_PORT=' .env | tail -1 | cut -d= -f2-)"
backend_port="${backend_port:-8080}"
supervisor_port="$(grep -E '^SUPERVISOR_PORT=' .env | tail -1 | cut -d= -f2-)"
supervisor_port="${supervisor_port:-8090}"

echo "Starting host-a and host-b..."
# A replaced host is started with docker run, so it keeps the name but is not a
# Compose container. The next `up` then tries to create that name and stops here.
if ! docker compose up -d --build; then
  echo "Compose could not create the host containers. Starting the ones already named host-a and host-b."
  docker start coding-agent-host-a coding-agent-host-b
fi

echo "Waiting for the daemons..."
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  if curl -sf -m 2 http://127.0.0.1:7701/health >/dev/null && curl -sf -m 2 http://127.0.0.1:7702/health >/dev/null; then
    break
  fi
  sleep 1
done
curl -sf -m 2 http://127.0.0.1:7701/health >/dev/null
curl -sf -m 2 http://127.0.0.1:7702/health >/dev/null

echo
echo "UI:         http://127.0.0.1:${backend_port}/"
echo "Supervisor: http://127.0.0.1:${supervisor_port}/status"
echo "Open a session, then press Live code to watch files change."
echo

exec .venv/bin/python -m backend.supervisor
