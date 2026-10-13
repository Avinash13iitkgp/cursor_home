#!/usr/bin/env bash
# Ask the backend to kill the host. The API marks it down for about 20 seconds
# before starting a replacement. A raw `docker kill` skips that hold.
set -euo pipefail
host="${1:?usage: kill_host.sh a|b}"
port="${BACKEND_PORT:-8080}"
exec curl -sf -X POST "http://127.0.0.1:${port}/api/hosts/host-${host}/kill"
