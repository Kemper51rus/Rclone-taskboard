#!/usr/bin/env bash
set -euo pipefail

DEFAULT_GIT_URL="${DEFAULT_GIT_URL:-https://github.com/Kemper51rus/Rclone-taskboard-web-gui.git}"
DEFAULT_GIT_REF="${DEFAULT_GIT_REF:-main}"
TARGET_ROOT="${TARGET_ROOT:-/opt/rclone-taskboard}"
SOURCE_ROOT="${SOURCE_ROOT:-}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="${SERVICE_NAME:-rclone-taskboard.service}"
SOURCE_CHECKOUT_DEFAULT="${SOURCE_CHECKOUT_DEFAULT:-/opt/rclone-taskboard-src}"
DOCKER_CONTAINER_NAME="${DOCKER_CONTAINER_NAME:-rclone-taskboard}"
STATE_DIR="${STATE_DIR:-/var/lib/rclone-taskboard-installer}"
APT_INSTALLED_RECORD="$STATE_DIR/apt-installed-by-install-sh.txt"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
SCRIPT_ARGS=("$@")

LEGACY_UNITS=(
  rclone-backup.service
  rclone-backup.timer
  rclone-watch.service
)

LEGACY_FILES=(
  /usr/local/bin/rclone-backup.sh
  /usr/local/bin/rclone-watch.sh
  /usr/local/bin/rclone-backup-status.sh
)

log() {
  printf '%s\n' "$*"
}

setup_colors() {
  if [[ -t 1 ]] && command_exists tput; then
    local colors
    colors="$(tput colors 2>/dev/null || echo 0)"
    if [[ "$colors" =~ ^[0-9]+$ ]] && (( colors >= 8 )); then
      C_RESET="$(tput sgr0)"
      C_BOLD="$(tput bold)"
      C_RED="$(tput setaf 1)"
      C_GREEN="$(tput setaf 2)"
      C_YELLOW="$(tput setaf 3)"
      C_BLUE="$(tput setaf 4)"
      C_CYAN="$(tput setaf 6)"
      return
    fi
  fi
  C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""
}

log_section() {
  printf '%b\n' "${C_BLUE}${C_BOLD}== $* ==${C_RESET}"
}

log_ok() {
  printf '%b\n' "${C_GREEN}OK${C_RESET}  $*"
}

log_warn() {
  printf '%b\n' "${C_YELLOW}WARN${C_RESET} $*"
}

log_err() {
  printf '%b\n' "${C_RED}ERR${C_RESET} $*"
}

die() {
  log_err "ERROR: $*"
  exit 1
}

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    if command_exists sudo; then
      local sudo_copy
      sudo_copy="$(mktemp /tmp/rclone-taskboard-install.XXXXXX)"
      cat "$0" > "$sudo_copy"
      chmod 755 "$sudo_copy"
      if [[ -n "$SCRIPT_REPO_ROOT" && -z "$SOURCE_ROOT" ]]; then
        export SOURCE_ROOT="$SCRIPT_REPO_ROOT"
      fi
      exec sudo -E bash "$sudo_copy" "${SCRIPT_ARGS[@]}"
    fi
    die "Запустите скрипт от root."
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

confirm() {
  local prompt="$1"
@@ -72,115 +109,151 @@ confirm() {
      y|yes|д|да) return 0 ;;
      n|no|н|нет) return 1 ;;
      *) log "Ответьте yes/no или да/нет." ;;
    esac
  done
}

ask_value() {
  local prompt="$1"
  local default="$2"
  local answer
  read -r -p "$prompt [$default]: " answer
  printf '%s\n' "${answer:-$default}"
}

safe_rm_rf() {
  local path="$1"
  [[ -n "$path" ]] || die "refusing to remove empty path"
  [[ "$path" != "/" ]] || die "refusing to remove /"
  [[ "$path" != "/opt" ]] || die "refusing to remove /opt"
  [[ "$path" != "/usr" ]] || die "refusing to remove /usr"
  [[ "$path" != "/etc" ]] || die "refusing to remove /etc"
  rm -rf --one-file-system -- "$path"
}

record_apt_packages() {
  local packages=("$@")
  [[ "${#packages[@]}" -gt 0 ]] || return 0
  install -d "$STATE_DIR"
  touch "$APT_INSTALLED_RECORD"
  printf '%s\n' "${packages[@]}" >> "$APT_INSTALLED_RECORD"
  sort -u "$APT_INSTALLED_RECORD" -o "$APT_INSTALLED_RECORD"
}

install_packages() {
  local packages=("$@")
  [[ "${#packages[@]}" -gt 0 ]] || return 0
  if command_exists apt-get; then
    log_section "Установка зависимостей через apt"
    log "Пакеты к установке: ${packages[*]}"
    local to_record=() pkg
    for pkg in "${packages[@]}"; do
      if dpkg-query -W -f='${Status}\n' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
        log_ok "$pkg уже установлен"
      else
        log_warn "$pkg будет установлен"
        to_record+=("$pkg")
      fi
    done
    log "Выполняю: apt-get update"
    if ! apt-get update; then
      log_warn "apt-get update завершился с ошибкой. Продолжаю установку по текущему кэшу APT."
    fi
    log "Выполняю: apt-get install -y ${packages[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
    if [[ "${#to_record[@]}" -gt 0 ]]; then
      record_apt_packages "${to_record[@]}"
      log_ok "Сохранён список новых apt-пакетов в $APT_INSTALLED_RECORD"
    fi
  elif command_exists dnf; then
    log_section "Установка зависимостей через dnf"
    log "Выполняю: dnf install -y ${packages[*]}"
    dnf install -y "${packages[@]}"
  elif command_exists yum; then
    log_section "Установка зависимостей через yum"
    log "Выполняю: yum install -y ${packages[*]}"
    yum install -y "${packages[@]}"
  elif command_exists zypper; then
    log_section "Установка зависимостей через zypper"
    log "Выполняю: zypper --non-interactive install ${packages[*]}"
    zypper --non-interactive install "${packages[@]}"
  elif command_exists pacman; then
    log_section "Установка зависимостей через pacman"
    log "Выполняю: pacman -Sy --noconfirm ${packages[*]}"
    pacman -Sy --noconfirm "${packages[@]}"
  else
    die "Не найден поддерживаемый package manager. Установите вручную: ${packages[*]}"
  fi
}

package_for_command() {
  local command_name="$1"
  case "$command_name" in
    git) printf 'git\n' ;;
    curl) printf 'curl\n' ;;
    rclone) printf 'rclone\n' ;;
    python3) printf 'python3\n' ;;
    install) printf 'coreutils\n' ;;
    docker) printf 'docker.io\n' ;;
    *) printf '%s\n' "$command_name" ;;
  esac
}

check_python_venv() {
  "$PYTHON_BIN" -m venv --help >/dev/null 2>&1
}

ensure_dependencies() {
  local mode="$1"
  local missing_packages=()
  local required_commands=(git install systemctl "$PYTHON_BIN" rclone curl)

  if [[ "$mode" == "docker" ]]; then
    required_commands=(git install docker curl)
  fi

  for command_name in "${required_commands[@]}"; do
    if ! command_exists "$command_name"; then
      missing_packages+=("$(package_for_command "$command_name")")
    fi
  done

  if [[ "$mode" == "systemd" ]] && command_exists "$PYTHON_BIN" && ! check_python_venv; then
    missing_packages+=(python3-venv)
  fi

  if [[ "$mode" == "docker" ]] && command_exists docker && ! docker compose version >/dev/null 2>&1 && ! command_exists docker-compose; then
    missing_packages+=(docker-compose-plugin)
  fi

  if [[ "${#missing_packages[@]}" -eq 0 ]]; then
    log_ok "Зависимости для режима '$mode' выглядят установленными."
    return 0
  fi

  log_warn "Не хватает зависимостей для режима '$mode': ${missing_packages[*]}"
  if confirm "Доустановить зависимости автоматически?" "yes"; then
    install_packages "${missing_packages[@]}"
  else
    die "Установка остановлена: не хватает зависимостей."
  fi
}

docker_compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command_exists docker-compose; then
    docker-compose "$@"
  else
    die "Не найден docker compose."
  fi
}

default_source_root() {
  if [[ -n "$SCRIPT_REPO_ROOT" && -d "$SCRIPT_REPO_ROOT/.git" ]]; then
    printf '%s\n' "$SCRIPT_REPO_ROOT"
  else
    printf '%s\n' "$SOURCE_CHECKOUT_DEFAULT"
  fi
}

@@ -420,90 +493,141 @@ uninstall_taskboard() {
  fi

  if [[ -f "$TARGET_ROOT/taskboard/docker-compose.yml" ]]; then
    if confirm "Остановить docker compose stack в $TARGET_ROOT/taskboard?" "yes"; then
      (
        cd "$TARGET_ROOT/taskboard"
        docker_compose --env-file .env.docker down || true
      )
    fi
  fi

  if [[ -d "$TARGET_ROOT" ]]; then
    if confirm "Удалить runtime-каталог $TARGET_ROOT включая data/jobs/env?" "no"; then
      safe_rm_rf "$TARGET_ROOT"
    else
      log "Runtime-каталог сохранён: $TARGET_ROOT"
    fi
  fi

  if [[ -n "$SOURCE_ROOT" && -d "$SOURCE_ROOT/.git" ]]; then
    if confirm "Удалить source checkout $SOURCE_ROOT?" "no"; then
      safe_rm_rf "$SOURCE_ROOT"
    fi
  fi

  if command_exists apt-get && [[ -f "$APT_INSTALLED_RECORD" ]]; then
    local purge_packages=()
    while IFS= read -r pkg; do
      [[ -n "$pkg" ]] || continue
      purge_packages+=("$pkg")
    done < "$APT_INSTALLED_RECORD"
    if [[ "${#purge_packages[@]}" -gt 0 ]]; then
      log "Найден список apt-пакетов, установленных этим скриптом: ${purge_packages[*]}"
      if confirm "Попробовать apt purge этих пакетов?" "no"; then
        apt-get purge -y "${purge_packages[@]}" || true
        apt-get autoremove -y || true
      fi
    fi
    if confirm "Удалить файл состояния установленных пакетов $APT_INSTALLED_RECORD?" "yes"; then
      rm -f "$APT_INSTALLED_RECORD"
    fi
  fi

  log "Uninstall завершён."
}

print_dependency_status() {
  local command_name package_name status_line
  log "  зависимости:"
  for command_name in git curl install systemctl "$PYTHON_BIN" rclone docker; do
    package_name="$(package_for_command "$command_name")"
    if command_exists "$command_name"; then
      status_line="${C_GREEN}ok${C_RESET}"
    else
      status_line="${C_RED}missing${C_RESET}"
    fi
    printf '    - %-14s : %b (pkg: %s)\n' "$command_name" "$status_line" "$package_name"
  done
}

print_docker_status() {
  if ! command_exists docker; then
    log "  docker: команда docker не найдена"
    return
  fi
  local container_state
  container_state="$(docker inspect -f '{{.State.Status}}' "$DOCKER_CONTAINER_NAME" 2>/dev/null || true)"
  [[ -n "$container_state" ]] \
    && log "  docker: контейнер '$DOCKER_CONTAINER_NAME' ${C_GREEN}найден${C_RESET} (state=$container_state)" \
    || log "  docker: контейнер '$DOCKER_CONTAINER_NAME' ${C_RED}не найден${C_RESET}"
}

print_status() {
  log ""
  log_section "Текущий статус"
  if systemctl list-unit-files "$SERVICE_NAME" --no-legend >/dev/null 2>&1; then
    log "  systemd: $SERVICE_NAME ${C_GREEN}найден${C_RESET}"
    systemctl is-active --quiet "$SERVICE_NAME" && log "  active: yes" || log_warn "active: no"
  fi
  if ! systemctl list-unit-files "$SERVICE_NAME" --no-legend >/dev/null 2>&1; then
    log "  systemd: $SERVICE_NAME ${C_RED}не найден${C_RESET}"
  fi
  [[ -d "$TARGET_ROOT" ]] \
    && log "  runtime: $TARGET_ROOT ${C_GREEN}найден${C_RESET}" \
    || log "  runtime: $TARGET_ROOT ${C_RED}не найден${C_RESET}"
  print_docker_status
  print_dependency_status
  [[ -n "$SCRIPT_REPO_ROOT" ]] && log "  current git checkout: $SCRIPT_REPO_ROOT"
  log ""
}

main_menu() {
  while true; do
    print_status
    cat <<'MENU'
Выберите действие:
  1) Установить/обновить через systemd
  2) Установить/обновить через Docker
  3) Только переход с legacy: backup + удалить старые legacy-скрипты и unit'ы
  4) Удалить taskboard-установку
  5) Выйти
MENU
    local choice
    read -r -p "Номер действия [1-5]: " choice
    case "$choice" in
      1) install_or_update_systemd ;;
      2) install_or_update_docker ;;
      3) TARGET_ROOT="$(ask_value "Каталог для migration-backups" "$TARGET_ROOT")"; cleanup_legacy ;;
      4) uninstall_taskboard ;;
      5|q|quit|exit) exit 0 ;;
      *) log "Неизвестный выбор: $choice" ;;
    esac
  done
}

setup_colors

case "${1:-}" in
  systemd) install_or_update_systemd ;;
  docker) install_or_update_docker ;;
  legacy-cleanup|migrate-legacy) TARGET_ROOT="$(ask_value "Каталог для migration-backups" "$TARGET_ROOT")"; cleanup_legacy ;;
  uninstall|remove) uninstall_taskboard ;;
  ""|menu) main_menu ;;
  *)
    cat <<EOF
Usage:
  $0                 # interactive menu
  $0 systemd         # install/update systemd deployment
  $0 docker          # install/update docker deployment
  $0 migrate-legacy  # backup and remove legacy scripts and units
  $0 uninstall       # remove taskboard deployment

Environment:
  TARGET_ROOT=$TARGET_ROOT
  SOURCE_ROOT=${SOURCE_ROOT:-auto}
  DEFAULT_GIT_URL=$DEFAULT_GIT_URL
  DEFAULT_GIT_REF=$DEFAULT_GIT_REF
EOF
    exit 2
    ;;
esac
