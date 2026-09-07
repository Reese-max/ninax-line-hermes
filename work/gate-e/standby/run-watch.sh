#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source /home/box/irisx-failover-restore/20260906/gate-e/standby/env.sh
export IRISX_PIDFILE=/home/box/irisx-failover-restore/20260906/profile-irisx/gateway.pid
exec /usr/bin/python3 /home/box/irisx-failover-restore/20260906/gate-e/bin/irisx_guard.py watch
