#!/usr/bin/env bash
set -euo pipefail
host="${1:?usage: kill_host.sh a|b}"
exec docker kill "coding-agent-host-${host}"
