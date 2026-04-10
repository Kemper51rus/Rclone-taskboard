#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
MODE="${1:-}"
TARGET_ROOT="${2:-/opt/rclone-hybrid}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT="${BACKUP_ROOT:-$TARGET_ROOT/migration-backups/$STAMP}"

LEGACY_UNITS=(
  rclone-backup.service
  rclone-backup.timer
  rclone-watch.service
  rclone-web.service
)

LEGACY_FILES=(
  /usr/local/bin/rclone-backup.sh
  /usr/local/bin/rclone-backup-status.sh
  /usr/local/bin/rclone-watch.sh
  /etc/rclone-backup.gotify
  /var/lib/rclone-backup
  /var/log/rclone-backup.log
)

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

backup_path() {
  local source="$1"
  local target="$BACKUP_ROOT${source}"
  if [[ -e "$source" ]]; then
    install -d "$(dirname "$target")"
    cp -a "$source" "$target"
  fi
}

disable_unit_if_present() {
  local unit="$1"
  if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -qF "$unit"; then
    systemctl disable --now "$unit" || true
  fi
}

require_cmd systemctl

if [[ "$MODE" != "systemd" && "$MODE" != "docker" ]]; then
  echo "usage: $0 <systemd|docker> [target-root]" >&2
  exit 1
fi

install -d "$BACKUP_ROOT"

for unit in "${LEGACY_UNITS[@]}"; do
  systemctl cat "$unit" > "$BACKUP_ROOT/${unit}.systemctl-cat.txt" 2>/dev/null || true
  systemctl status "$unit" --no-pager > "$BACKUP_ROOT/${unit}.status.txt" 2>/dev/null || true
done

for path in "${LEGACY_FILES[@]}"; do
  backup_path "$path"
done

for unit in "${LEGACY_UNITS[@]}"; do
  disable_unit_if_present "$unit"
done

case "$MODE" in
  systemd)
    "$ROOT_DIR/scripts/install-hybrid-systemd.sh" "$TARGET_ROOT"
    systemctl enable --now rclone-hybrid-web.service
    systemctl enable --now rclone-watch-hybrid.service
    ;;
  docker)
    "$ROOT_DIR/scripts/install-hybrid-docker.sh" "$TARGET_ROOT"
    ;;
esac

cat <<EOF
legacy migration completed

mode: $MODE
target root: $TARGET_ROOT
backup snapshot: $BACKUP_ROOT
EOF
