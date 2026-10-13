#!/usr/bin/env bash
# Drop every session and restore the inventory snapshot.
# Removes session workspaces, imports, snapshot history, and the SQLite log,
# strips packages written into dummy-repo, and copies the inventory tree back
# to data/cow/base. Ctrl+C ./scripts/start.sh first so the backend is not
# writing the database. The Docker hosts can keep running.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "No .env in $(pwd)." >&2
  exit 1
fi

backend_port="$(grep -E '^BACKEND_PORT=' .env | tail -1 | cut -d= -f2-)"
backend_port="${backend_port:-8080}"
supervisor_port="$(grep -E '^SUPERVISOR_PORT=' .env | tail -1 | cut -d= -f2-)"
supervisor_port="${supervisor_port:-8090}"

if curl -sf -m 1 "http://127.0.0.1:${backend_port}/api/hosts" >/dev/null \
  || curl -sf -m 1 "http://127.0.0.1:${supervisor_port}/status" >/dev/null; then
  echo "The backend is still running. Ctrl+C ./scripts/start.sh, then run this again." >&2
  exit 1
fi

cow_root="$(grep -E '^COW_ROOT=' .env | tail -1 | cut -d= -f2-)"
cow_root="${cow_root:-data/cow}"
db_path="$(grep -E '^DB_PATH=' .env | tail -1 | cut -d= -f2-)"
db_path="${db_path:-data/agent.db}"
imports_root="$(dirname "$cow_root")/imports"

echo "Removing written packages from dummy-repo..."
find dummy-repo -mindepth 1 -maxdepth 1 \
  ! -name README.md ! -name TASK.md ! -name inventory ! -name tests \
  -exec rm -rf {} +
find dummy-repo/tests -mindepth 1 \
  ! -name __init__.py ! -name test_cli.py ! -name test_report.py ! -name test_store.py \
  -exec rm -rf {} +
find dummy-repo -type d -name __pycache__ -exec rm -rf {} +

echo "Removing sessions, imports, and snapshot history..."
rm -rf "$cow_root/sessions" "$cow_root/history" "$cow_root/base" "$imports_root"
find "$cow_root" -mindepth 1 -maxdepth 1 -name '.tmp-*' -exec rm -rf {} +
mkdir -p "$cow_root" "$imports_root"

echo "Removing the session database..."
rm -f "$db_path" "${db_path}-wal" "${db_path}-shm" \
  data/live-step6.db data/live-step6.db-wal data/live-step6.db-shm

echo "Seeding the inventory snapshot..."
py=".venv/bin/python"
if [[ ! -x "$py" ]]; then
  py="python3"
fi
PYTHONPATH="." "$py" - <<PY
from pathlib import Path
from daemon.workspace import seed_base
seed_base(Path("${cow_root}"), Path("dummy-repo"))
PY

echo
echo "Base snapshot:"
find "$cow_root/base/dummy-repo" -type f ! -name '.*' | sort
echo
echo "Sessions, imports, and history are gone. Start again with ./scripts/start.sh."
echo "That rebuilds the daemon image so a later reseed uses this inventory tree."
