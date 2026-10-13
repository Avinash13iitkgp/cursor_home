#!/usr/bin/env bash
# Build the two local daemon containers and check copy-on-write plus host restart.
# Usage: coding-agent/scripts/test_daemon_docker.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Docker Desktop's credential helper is not always on a login PATH.
export PATH="${HOME}/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:${PATH}"

HOST_A=http://127.0.0.1:7701
HOST_B=http://127.0.0.1:7702
SESSION="s_$(date +%s)"
OP_ID="${SESSION}.0.0"

mkdir -p data/cow

say() {
  printf '\n== %s\n' "$1"
}

json_field() {
  # json_field <python-expression>  reads JSON on stdin
  python3 -c "import json,sys; data=json.load(sys.stdin); print($1)"
}

wait_health() {
  local url="$1"
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if curl -sf --max-time 2 "${url}/health" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "timed out waiting for ${url}/health" >&2
  docker compose ps >&2 || true
  docker compose logs --tail=40 >&2 || true
  return 1
}

post() {
  local url="$1"
  local body="$2"
  curl -sf --max-time 10 -X POST "$url" -H 'Content-Type: application/json' -d "$body"
}

say "build and start host-a and host-b"
# Failover starts a container with `docker run --name`. Compose does not own that
# container, so the next `compose up` hits a name conflict unless it is removed.
docker rm -f coding-agent-host-a coding-agent-host-b >/dev/null 2>&1 || true
docker compose up -d --build
wait_health "$HOST_A"
wait_health "$HOST_B"

say "health"
HEALTH_A="$(curl -sf --max-time 2 "${HOST_A}/health")"
HEALTH_B="$(curl -sf --max-time 2 "${HOST_B}/health")"
echo "$HEALTH_A"
echo "$HEALTH_B"
BOOT_BEFORE="$(printf '%s' "$HEALTH_A" | json_field 'data["boot_id"]')"
test "$(printf '%s' "$HEALTH_A" | json_field 'data["host_id"]')" = "host-a"
test "$(printf '%s' "$HEALTH_A" | json_field 'data["base_ready"]')" = "True"
test "$(printf '%s' "$HEALTH_B" | json_field 'data["host_id"]')" = "host-b"
test -f data/cow/base/dummy-repo/TASK.md

say "create workspace ${SESSION}"
CREATED="$(post "${HOST_A}/workspaces" "{\"session_id\":\"${SESSION}\"}")"
echo "$CREATED"
test "$(printf '%s' "$CREATED" | json_field 'data["created"]')" = "True"

say "write_file, then the same op_id again"
CALL="$(cat <<EOF
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"write_file","arguments":{"path":"note.txt","content":"hello"},"_meta":{"session_id":"${SESSION}","op_id":"${OP_ID}"}}}
EOF
)"
FIRST="$(post "${HOST_A}/mcp" "$CALL")"
SECOND="$(post "${HOST_A}/mcp" "$CALL")"
echo "$FIRST"
echo "$SECOND"
test "$(printf '%s' "$FIRST" | json_field 'data["result"]["_meta"]["replayed"]')" = "False"
test "$(printf '%s' "$SECOND" | json_field 'data["result"]["_meta"]["replayed"]')" = "True"
test "$(cat "data/cow/sessions/${SESSION}/workspace/note.txt")" = "hello"
test ! -e data/cow/base/dummy-repo/note.txt

say "kill host-a; the Mac file must remain"
docker kill coding-agent-host-a
if curl -sf --max-time 2 "${HOST_A}/health" >/dev/null; then
  echo "host-a was still serving after docker kill" >&2
  exit 1
fi
test "$(cat "data/cow/sessions/${SESSION}/workspace/note.txt")" = "hello"
echo "host-a is down; note.txt is still on disk"

say "start a replacement on the same port and the same directory"
docker compose up -d --no-deps host-a
wait_health "$HOST_A"
HEALTH_AFTER="$(curl -sf --max-time 2 "${HOST_A}/health")"
echo "$HEALTH_AFTER"
BOOT_AFTER="$(printf '%s' "$HEALTH_AFTER" | json_field 'data["boot_id"]')"
test "$BOOT_AFTER" != "$BOOT_BEFORE"
AGAIN="$(post "${HOST_A}/workspaces" "{\"session_id\":\"${SESSION}\"}")"
echo "$AGAIN"
test "$(printf '%s' "$AGAIN" | json_field 'data["created"]')" = "False"
REPLAY="$(post "${HOST_A}/mcp" "$CALL")"
echo "$REPLAY"
test "$(printf '%s' "$REPLAY" | json_field 'data["result"]["_meta"]["replayed"]')" = "True"

say "daemon docker check passed"
echo "session ${SESSION}"
echo "boot_id ${BOOT_BEFORE} -> ${BOOT_AFTER}"
