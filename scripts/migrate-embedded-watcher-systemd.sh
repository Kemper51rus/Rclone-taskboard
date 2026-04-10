#!/usr/bin/env bash
set -euo pipefail

OLD_SERVICE="${OLD_SERVICE:-rclone-watch-hybrid.service}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
TARGET_ROOT="${1:-/opt/rclone-hybrid}"
OLD_UNIT_PATH="$SYSTEMD_DIR/$OLD_SERVICE"
OLD_TARGET_UNIT_PATH="$TARGET_ROOT/systemd/$OLD_SERVICE"
OLD_TARGET_SCRIPT_PATH="$TARGET_ROOT/scripts/rclone-watch-hybrid.sh"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

stop_if_active() {
  local service="$1"
  if systemctl is-active --quiet "$service" 2>/dev/null; then
    echo "stopping $service"
    systemctl stop "$service"
  fi
}

disable_if_enabled() {
  local service="$1"
  if systemctl is-enabled --quiet "$service" 2>/dev/null; then
    echo "disabling $service"
    systemctl disable "$service"
  fi
}

require_cmd systemctl

echo "Migrating old external watcher service to embedded backend watcher"

stop_if_active "$OLD_SERVICE"
disable_if_enabled "$OLD_SERVICE"

if [[ -f "$OLD_UNIT_PATH" ]]; then
  echo "removing obsolete unit file: $OLD_UNIT_PATH"
  rm -f "$OLD_UNIT_PATH"
fi

if [[ -f "$OLD_TARGET_UNIT_PATH" ]]; then
  echo "removing obsolete installed copy: $OLD_TARGET_UNIT_PATH"
  rm -f "$OLD_TARGET_UNIT_PATH"
fi

if [[ -f "$OLD_TARGET_SCRIPT_PATH" ]]; then
  echo "removing obsolete watcher script copy: $OLD_TARGET_SCRIPT_PATH"
  rm -f "$OLD_TARGET_SCRIPT_PATH"
fi

echo "reloading systemd daemon"
systemctl daemon-reload

cat <<EOF
Migration completed.

What changed:
  - old service '$OLD_SERVICE' is stopped and disabled
  - obsolete unit files are removed when present
  - embedded watcher now runs inside rclone-hybrid-web.service

Recommended next step:
  systemctl restart rclone-hybrid-web.service
EOF
