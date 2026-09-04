#!/usr/bin/env bash
# Install (or remove) the systemd service that imports and publishes sessions.
# Target: Linux with systemd (tested on Rocky Linux 9.8). Requires sudo.
#
#   bash scripts/systemd/install.sh              # install and start
#   bash scripts/systemd/install.sh --status     # state + recent runs
#   bash scripts/systemd/install.sh --logs       # service journal
#   bash scripts/systemd/install.sh --remove     # stop and uninstall
#
# SYSTEM-LEVEL units running as the current user: no need for
# `loginctl enable-linger`, and the service survives reboots and SSH
# disconnections alike.
#
# Type=oneshot + timer: no process resident in memory between passes.
# Persistent=true catches up a missed pass if the machine was off.

set -euo pipefail

NAME="running-coach-publish"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_USER="$(id -un)"
INTERVAL="${INTERVAL:-15min}"
DAYS="${DAYS:-5}"
UNIT="/etc/systemd/system/$NAME.service"
TIMER="/etc/systemd/system/$NAME.timer"

case "${1:-}" in
  --status)
    systemctl status "$NAME.timer" --no-pager 2>&1 | head -12
    echo
    echo "Next runs:"
    systemctl list-timers "$NAME.timer" --no-pager 2>&1 | head -4
    echo
    echo "Last lines of the application log:"
    tail -n 12 "$REPO/logs/strava_publish.log" 2>/dev/null || echo "  (empty log)"
    exit 0
    ;;
  --logs)
    journalctl -u "$NAME.service" -n "${2:-50}" --no-pager
    exit 0
    ;;
  --remove)
    sudo systemctl disable --now "$NAME.timer" 2>/dev/null || true
    sudo rm -f "$UNIT" "$TIMER"
    sudo systemctl daemon-reload
    echo "Service $NAME removed."
    exit 0
    ;;
esac

# Resolve the REAL binary: `command -v python3` may return a pyenv shim, which
# depends on an environment (PATH, PYENV_ROOT) that systemd does not provide.
# This is the single most common reason the unit works by hand and fails as a
# service.
PYTHON="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || { echo "python3 not found." >&2; exit 1; }
case "$PYTHON" in
  */shims/*) echo "  ! pyenv shim detected, falling back to /usr/bin/python3"
             PYTHON=/usr/bin/python3 ;;
esac
[[ -f "$REPO/.env" ]] || { echo "$REPO/.env missing: see scripts/README.md." >&2; exit 1; }

mkdir -p "$REPO/logs"

sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=running-performance-coach — import and publish Strava sessions
Documentation=file://$REPO/scripts/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO
ExecStart=$PYTHON $REPO/scripts/strava_publish.py --once --days $DAYS --push
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=false
UNITEOF

sudo tee "$TIMER" >/dev/null <<TIMEREOF
[Unit]
Description=running-performance-coach — trigger the import every $INTERVAL

[Timer]
OnBootSec=2min
OnUnitActiveSec=$INTERVAL
AccuracySec=1min
Persistent=true
Unit=$NAME.service

[Install]
WantedBy=timers.target
TIMEREOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$NAME.timer"

echo
echo "Service $NAME installed."
echo "  python   : $PYTHON"
echo "  repo     : $REPO"
echo "  user     : $RUN_USER"
echo "  interval : $INTERVAL (catch-up window: $DAYS days)"
echo
echo "Check : bash scripts/systemd/install.sh --status"
echo "Logs  : bash scripts/systemd/install.sh --logs"
echo "Remove: bash scripts/systemd/install.sh --remove"
