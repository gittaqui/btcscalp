#!/usr/bin/env bash
set -euo pipefail
# Read-only readiness check; changes to the host remain operator-controlled.
test "$(id -u)" -ne 0 || { echo 'Run the application as a non-root user'; exit 1; }
test "$(timedatectl show -p NTPSynchronized --value)" = yes || { echo 'Clock is not synchronized'; exit 1; }
python3 --version
df -h .
free -m
docker compose version
echo 'Host basics passed. Complete docs/SECURITY.md before enabling live mode.'
