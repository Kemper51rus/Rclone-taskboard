#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
TARGET_ROOT="${1:-/opt/rclone-hybrid}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

warn_missing_runtime() {
  local name="$1"
  command -v "$name" >/dev/null 2>&1 || echo "warning: runtime dependency '$name' is not installed"
}

require_cmd "$PYTHON_BIN"
require_cmd install
require_cmd systemctl

warn_missing_runtime rclone
warn_missing_runtime curl

install -d "$TARGET_ROOT" "$TARGET_ROOT/hybrid" "$TARGET_ROOT/hybrid/backend" "$TARGET_ROOT/hybrid/backend/app" \
  "$TARGET_ROOT/hybrid/data" "$TARGET_ROOT/systemd" "$TARGET_ROOT/scripts"

cp -a "$REPO_ROOT/hybrid/backend/app/." "$TARGET_ROOT/hybrid/backend/app/"
find "$TARGET_ROOT/hybrid/backend/app" \( -type d -name __pycache__ -o -type f -name '*.pyc' \) -exec rm -rf {} +
install -m 0644 "$REPO_ROOT/hybrid/backend/requirements.txt" "$TARGET_ROOT/hybrid/backend/requirements.txt"
install -m 0644 "$REPO_ROOT/hybrid/backend/app/jobs/default_jobs.example.json" "$TARGET_ROOT/hybrid/backend/app/jobs/default_jobs.example.json"
install -m 0644 "$REPO_ROOT/systemd/rclone-hybrid-web.service" "$TARGET_ROOT/systemd/rclone-hybrid-web.service"
install -m 0644 "$REPO_ROOT/hybrid/.env.systemd.example" "$TARGET_ROOT/hybrid/.env.systemd.example"
install -m 0755 "$REPO_ROOT/scripts/migrate-embedded-watcher-systemd.sh" "$TARGET_ROOT/scripts/migrate-embedded-watcher-systemd.sh"

if [[ ! -f "$TARGET_ROOT/hybrid/.env" ]]; then
  install -m 0644 "$REPO_ROOT/hybrid/.env.systemd.example" "$TARGET_ROOT/hybrid/.env"
fi

if [[ ! -f "$TARGET_ROOT/hybrid/backend/app/jobs/default_jobs.json" ]]; then
  install -m 0644 \
    "$REPO_ROOT/hybrid/backend/app/jobs/default_jobs.example.json" \
    "$TARGET_ROOT/hybrid/backend/app/jobs/default_jobs.json"
fi

"$PYTHON_BIN" -m venv "$TARGET_ROOT/hybrid/.venv"
"$TARGET_ROOT/hybrid/.venv/bin/pip" install --upgrade pip
"$TARGET_ROOT/hybrid/.venv/bin/pip" install -r "$TARGET_ROOT/hybrid/backend/requirements.txt"

install -m 0644 "$REPO_ROOT/systemd/rclone-hybrid-web.service" "$SYSTEMD_DIR/rclone-hybrid-web.service"
systemctl daemon-reload

if systemctl is-active --quiet rclone-watch-hybrid.service 2>/dev/null; then
  systemctl stop rclone-watch-hybrid.service
fi

if systemctl is-enabled --quiet rclone-watch-hybrid.service 2>/dev/null; then
  systemctl disable rclone-watch-hybrid.service
fi

if [[ -f "$SYSTEMD_DIR/rclone-watch-hybrid.service" ]]; then
  rm -f "$SYSTEMD_DIR/rclone-watch-hybrid.service"
  systemctl daemon-reload
fi

if [[ -f "$TARGET_ROOT/systemd/rclone-watch-hybrid.service" ]]; then
  rm -f "$TARGET_ROOT/systemd/rclone-watch-hybrid.service"
fi

if [[ -f "$TARGET_ROOT/scripts/rclone-watch-hybrid.sh" ]]; then
  rm -f "$TARGET_ROOT/scripts/rclone-watch-hybrid.sh"
fi

cat <<EOF
systemd deployment bundle installed into: $TARGET_ROOT

Next steps:
  1. Review and edit $TARGET_ROOT/hybrid/.env
  2. If you are upgrading from the old external watcher, run:
     $TARGET_ROOT/scripts/migrate-embedded-watcher-systemd.sh $TARGET_ROOT
  3. systemctl enable --now rclone-hybrid-web.service
EOF
