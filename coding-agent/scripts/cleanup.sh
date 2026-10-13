#!/usr/bin/env bash
# Drop every session and restore the inventory snapshot.
# Stops the backend and supervisor when they are still up, so SQLite is not
# holding the session log open. Then removes session workspaces, imports,
# snapshot history, and the database, strips packages written into dummy-repo,
# and copies the inventory tree back to data/cow/base.
# The Docker hosts can keep running.
set -euo pipefail
cd "$(dirname "$0")/.."
shopt -s nullglob

if [[ ! -f .env ]]; then
  echo "No .env in $(pwd)." >&2
  exit 1
fi

env_value() {
  local key="$1"
  local fallback="$2"
  local value
  value="$(grep -E "^${key}=" .env | tail -1 | cut -d= -f2- || true)"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  if [[ -z "$value" ]]; then
    value="$fallback"
  fi
  printf '%s' "$value"
}

abs_path() {
  local path="$1"
  if [[ "$path" != /* ]]; then
    path="$(pwd)/${path#./}"
  fi
  printf '%s' "$path"
}

backend_port="$(env_value BACKEND_PORT 8080)"
supervisor_port="$(env_value SUPERVISOR_PORT 8090)"
cow_root="$(abs_path "$(env_value COW_ROOT data/cow)")"
db_path="$(abs_path "$(env_value DB_PATH data/agent.db)")"
imports_root="$(dirname "$cow_root")/imports"

backend_pids() {
  # Listeners, plus a backend the supervisor already orphaned. That process
  # keeps the SQLite file open and keeps serving browser tabs that are already
  # connected, even after it drops its listen socket.
  local pid command
  while read -r pid command; do
    case "$command" in
      *backend.supervisor*|*uvicorn\ backend.api:app*)
        printf '%s\n' "$pid"
        ;;
    esac
  done < <(ps -axo pid=,command=)
  lsof -nP -iTCP:"$backend_port" -sTCP:LISTEN -t 2>/dev/null || true
  lsof -nP -iTCP:"$supervisor_port" -sTCP:LISTEN -t 2>/dev/null || true
}

stop_backend() {
  local pids pid
  pids="$(backend_pids)"
  if [[ -z "$pids" ]]; then
    return 0
  fi
  echo "Stopping the backend and supervisor..."
  # Supervisor first: if it is still alive it restarts the backend.
  for pid in $pids; do
    if ps -p "$pid" -o command= 2>/dev/null | grep -q 'backend.supervisor'; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  for pid in $pids; do
    kill -KILL "$pid" 2>/dev/null || true
  done
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    pids="$(backend_pids)"
    if [[ -z "$pids" ]]; then
      return 0
    fi
    for pid in $pids; do
      kill -KILL "$pid" 2>/dev/null || true
    done
    sleep 0.2
  done
  echo "The backend is still running." >&2
  exit 1
}

stop_backend

echo "Removing written packages from dummy-repo..."
keep_root=" README.md TASK.md inventory tests "
for path in dummy-repo/*; do
  name="$(basename "$path")"
  if [[ "$keep_root" != *" $name "* ]]; then
    rm -rf "$path"
  fi
done
keep_tests=" __init__.py test_cli.py test_report.py test_store.py "
for path in dummy-repo/tests/*; do
  name="$(basename "$path")"
  if [[ "$keep_tests" != *" $name "* ]]; then
    rm -rf "$path"
  fi
done
find dummy-repo -type d -name __pycache__ -prune -exec rm -rf {} + || true

echo "Removing sessions, imports, and snapshot history..."
rm -rf "$cow_root/sessions" "$cow_root/history" "$cow_root/base" "$imports_root"
for path in "$cow_root"/.tmp-*; do
  rm -rf "$path"
done
mkdir -p "$cow_root" "$imports_root"

echo "Removing the session database..."
rm -f "$db_path" "${db_path}-wal" "${db_path}-shm" \
  data/live-step6.db data/live-step6.db-wal data/live-step6.db-shm

echo "Seeding the inventory snapshot..."
py=".venv/bin/python"
if [[ ! -x "$py" ]]; then
  py="python3"
fi
COW_ROOT_PATH="$cow_root" PYTHONPATH="." "$py" - <<'PY'
import os
from pathlib import Path
from daemon.workspace import seed_base
seed_base(Path(os.environ["COW_ROOT_PATH"]), Path("dummy-repo"))
PY

echo
echo "Base snapshot:"
find "$cow_root/base/dummy-repo" -type f ! -name '.*' | sort
echo
echo "Sessions, imports, and history are gone. Start again with ./scripts/start.sh."
echo "That rebuilds the daemon image so a later reseed uses this inventory tree."
