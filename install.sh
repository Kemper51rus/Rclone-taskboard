#!/usr/bin/env bash
set -euo pipefail

DEFAULT_GIT_URL="${DEFAULT_GIT_URL:-https://github.com/Kemper51rus/Rclone-taskboard.git}"
DEFAULT_GIT_REF="${DEFAULT_GIT_REF:-main}"
TARGET_ROOT="${TARGET_ROOT:-/opt/rclone-taskboard}"
SOURCE_ROOT="${SOURCE_ROOT:-}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="${SERVICE_NAME:-rclone-taskboard.service}"
SOURCE_CHECKOUT_DEFAULT="${SOURCE_CHECKOUT_DEFAULT:-/opt/rclone-taskboard-src}"
DOCKER_CONTAINER_NAME="${DOCKER_CONTAINER_NAME:-rclone-taskboard}"
RCLONE_WEB_SERVICE_NAME="${RCLONE_WEB_SERVICE_NAME:-rclone-web.service}"
RCLONE_WEB_ADDR="${RCLONE_WEB_ADDR:-:3000}"
RCLONE_WEB_NO_AUTH="${RCLONE_WEB_NO_AUTH:-yes}"
JOBS_TEMPLATE_MODE="${JOBS_TEMPLATE_MODE:-}"
STATE_DIR="${STATE_DIR:-/var/lib/rclone-taskboard-installer}"
APT_INSTALLED_RECORD="$STATE_DIR/apt-installed-by-install-sh.txt"
RCLONE_WEB_INSTALLED_MARKER="$STATE_DIR/rclone-web-service-installed-by-install-sh"

USE_STANDARD_SETTINGS="${USE_STANDARD_SETTINGS:-}"
STANDARD_SETTINGS_INITIALIZED=0
PYTHON_VENV_CHECK_CACHE=""

invalidate_runtime_caches() {
  PYTHON_VENV_CHECK_CACHE=""
  hash -r 2>/dev/null || true
}

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
      C_DIM="$(tput dim 2>/dev/null || true)"
      return
    fi
  fi
  C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""; C_DIM=""
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


print_separator() {
  printf '%b\n' "${C_CYAN}------------------------------------------------------------${C_RESET}"
}

package_installed_by_script() {
  local package_name="$1"
  [[ -f "$APT_INSTALLED_RECORD" ]] || return 1
  grep -Fxq "$package_name" "$APT_INSTALLED_RECORD"
}

get_local_ipv4s() {
  local ips=() ip

  if command_exists hostname; then
    while read -r ip; do
      [[ -n "$ip" ]] || continue
      [[ "$ip" == 127.* ]] && continue
      ips+=("$ip")
    done < <(hostname -I 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+(\.[0-9]+){3}$/')
  fi

  if [[ "${#ips[@]}" -eq 0 ]] && command_exists ip; then
    while read -r ip; do
      [[ -n "$ip" ]] || continue
      [[ "$ip" == 127.* ]] && continue
      ips+=("$ip")
    done < <(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
  fi

  [[ "${#ips[@]}" -gt 0 ]] || return 0
  printf '%s\n' "${ips[@]}" | awk '!seen[$0]++'
}

first_local_ipv4() {
  local ip
  ip="$(get_local_ipv4s | head -n1 || true)"
  printf '%s\n' "$ip"
}

print_access_summary() {
  local mode="$1"
  local dashboard_port="8080"
  local primary_ip dashboard_url rclone_url rclone_web_port

  primary_ip="$(first_local_ipv4)"
  if [[ -n "$primary_ip" ]]; then
    dashboard_url="http://${primary_ip}:${dashboard_port}/"
  else
    dashboard_url="http://<local-ip>:${dashboard_port}/"
  fi

  log ""
  print_separator
  printf '%b\n' "${C_GREEN}${C_BOLD}Rclone taskboard установлен${C_RESET}"
  printf '%b\n' "${C_CYAN}Режим:${C_RESET} $mode"
  printf '%b\n' "${C_CYAN}Runtime:${C_RESET} $TARGET_ROOT"
  printf '%b\n' "${C_CYAN}Сервис:${C_RESET} $SERVICE_NAME"
  printf '%b\n' "${C_CYAN}Taskboard LAN:${C_RESET} $dashboard_url"

  if has_working_systemd && systemctl list-unit-files "$SERVICE_NAME" --no-legend >/dev/null 2>&1; then
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      printf '%b\n' "${C_GREEN}Systemd service active: yes${C_RESET}"
    else
      printf '%b\n' "${C_YELLOW}Systemd service active: no${C_RESET}"
    fi
  fi

  rclone_web_port="$(rclone_web_port)"
  if has_working_systemd && systemctl list-unit-files "$RCLONE_WEB_SERVICE_NAME" --no-legend >/dev/null 2>&1; then
    if [[ -n "$primary_ip" ]]; then
      rclone_url="http://${primary_ip}:${rclone_web_port}/"
    else
      rclone_url="http://<local-ip>:${rclone_web_port}/"
    fi
    printf '%b\n' "${C_CYAN}Rclone Web GUI LAN:${C_RESET} $rclone_url"
    if systemctl is-active --quiet "$RCLONE_WEB_SERVICE_NAME"; then
      printf '%b\n' "${C_GREEN}Rclone Web GUI service active: yes${C_RESET}"
    else
      printf '%b\n' "${C_YELLOW}Rclone Web GUI service active: no${C_RESET}"
    fi
    if [[ "${RCLONE_WEB_NO_AUTH,,}" =~ ^(1|y|yes|true|on)$ ]]; then
      printf '%b\n' "${C_YELLOW}Rclone Web GUI работает без авторизации; ограничьте доступ сетевыми правилами при необходимости.${C_RESET}"
    fi
  fi

  print_separator
  printf '%b\n' "${C_GREEN}Успешного применения Rclone taskboard в ваших задачах.${C_RESET}"
}

print_failure_summary() {
  local exit_code="$1"
  local failed_command="$2"

  log ""
  print_separator
  printf '%b\n' "${C_RED}${C_BOLD}Установка завершилась с ошибкой${C_RESET}"
  printf '%b\n' "${C_CYAN}Код выхода:${C_RESET} $exit_code"
  [[ -n "$failed_command" ]] && printf '%b\n' "${C_CYAN}Команда:${C_RESET} $failed_command"
  printf '%b\n' "${C_CYAN}Runtime:${C_RESET} $TARGET_ROOT"
  [[ -n "$SOURCE_ROOT" ]] && printf '%b\n' "${C_CYAN}Source checkout:${C_RESET} $SOURCE_ROOT"
  printf '%b\n' "${C_YELLOW}Установка остановилась до финального запуска сервиса. Проверьте ошибки выше.${C_RESET}"
  print_separator
}

on_error() {
  local exit_code=$?
  local failed_command="${BASH_COMMAND:-unknown}"
  trap - ERR
  print_failure_summary "$exit_code" "$failed_command"
  exit "$exit_code"
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

use_standard_settings() {
  case "${USE_STANDARD_SETTINGS,,}" in
    1|y|yes|true|on) return 0 ;;
    *) return 1 ;;
  esac
}

initialize_install_preferences() {
  (( STANDARD_SETTINGS_INITIALIZED == 1 )) && return 0
  STANDARD_SETTINGS_INITIALIZED=1

  if [[ -n "$USE_STANDARD_SETTINGS" ]]; then
    if use_standard_settings; then
      log_ok "Выбран режим стандартной установки: пути и зависимости будут обработаны автоматически."
    else
      log "Выбран режим ручной настройки."
    fi
    return 0
  fi

  if confirm "Использовать стандартные настройки установки?" "yes"; then
    USE_STANDARD_SETTINGS="yes"
    log_ok "Стандартные настройки включены."
  else
    USE_STANDARD_SETTINGS="no"
    log "Будет использован режим ручной настройки."
  fi
}

ask_value_maybe_auto() {
  local prompt="$1"
  local default="$2"
  if use_standard_settings; then
    printf '%s\n' "$prompt [$default]: auto" >&2
    printf '%s\n' "$default"
    return 0
  fi
  ask_value "$prompt" "$default"
}

ask_path_value_maybe_auto() {
  local prompt="$1"
  local default="$2"
  if use_standard_settings; then
    printf '%s\n' "$prompt [$default]: auto" >&2
    printf '%s\n' "$default"
    return 0
  fi
  ask_path_value "$prompt" "$default"
}

confirm_maybe_auto() {
  local prompt="$1"
  local default="${2:-no}"
  if use_standard_settings; then
    local suffix="[y/N]"
    [[ "$default" == "yes" ]] && suffix="[Y/n]"
    printf '%s\n' "$prompt $suffix auto: $default" >&2
    [[ "$default" == "yes" ]]
    return
  fi
  confirm "$prompt" "$default"
}

confirm() {
  local prompt="$1"
  local default="${2:-no}"
  local suffix="[y/N]"
  local answer
  [[ "$default" == "yes" ]] && suffix="[Y/n]"
  while true; do
    read -r -p "$prompt $suffix " answer
    answer="${answer,,}"
    if [[ -z "$answer" ]]; then
      [[ "$default" == "yes" ]]
      return
    fi
    case "$answer" in
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

ask_path_value() {
  local prompt="$1"
  local default="$2"
  local answer
  while true; do
    answer="$(ask_value "$prompt" "$default")"
    case "${answer,,}" in
      y|yes|д|да|n|no|н|нет)
        log "Для этого шага нужен путь, а не yes/no. Нажмите Enter для значения по умолчанию или укажите каталог."
        ;;
      *)
        printf '%s\n' "$answer"
        return 0
        ;;
    esac
  done
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
    invalidate_runtime_caches
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

rclone_web_port() {
  local addr="$RCLONE_WEB_ADDR"
  local port="${addr##*:}"
  if [[ "$port" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$port"
  else
    printf '3000\n'
  fi
}

rclone_binary_path() {
  if [[ -x /opt/rclone/rclone ]]; then
    printf '/opt/rclone/rclone\n'
    return 0
  fi
  command -v rclone
}

normalize_jobs_template_mode() {
  local mode="${1,,}"
  case "$mode" in
    examples|example|demo|samples|sample|шаблон|примеры)
      printf 'examples\n'
      ;;
    empty|blank|none|no-template|without-template|без-шаблона|пустой)
      printf 'empty\n'
      ;;
    *)
      return 1
      ;;
  esac
}

choose_jobs_template_mode() {
  local choice normalized
  if [[ -n "$JOBS_TEMPLATE_MODE" ]]; then
    normalized="$(normalize_jobs_template_mode "$JOBS_TEMPLATE_MODE")" \
      || die "Неизвестный JOBS_TEMPLATE_MODE=$JOBS_TEMPLATE_MODE. Допустимо: examples или empty."
    JOBS_TEMPLATE_MODE="$normalized"
    return 0
  fi

  if use_standard_settings; then
    JOBS_TEMPLATE_MODE="examples"
    log_ok "Каталог задач: будут установлены примеры из default_jobs.example.json."
    return 0
  fi

  log "Выберите начальный каталог задач:"
  log "  1) С примерами из taskboard/backend/app/jobs/default_jobs.example.json"
  log "  2) Без шаблона: пустой список задач"
  while true; do
    read -r -p "Номер варианта [1]: " choice
    case "${choice:-1}" in
      1) JOBS_TEMPLATE_MODE="examples"; return 0 ;;
      2) JOBS_TEMPLATE_MODE="empty"; return 0 ;;
      *) log "Введите 1 или 2." ;;
    esac
  done
}

check_python_venv() {
  if [[ -n "$PYTHON_VENV_CHECK_CACHE" ]]; then
    [[ "$PYTHON_VENV_CHECK_CACHE" == "ok" ]]
    return
  fi

  local tmpdir
  tmpdir="$(mktemp -d /tmp/rclone-taskboard-venv-check.XXXXXX)"
  if "$PYTHON_BIN" -m venv "$tmpdir/venv" >/dev/null 2>&1; then
    PYTHON_VENV_CHECK_CACHE="ok"
    rm -rf "$tmpdir"
    return 0
  fi

  PYTHON_VENV_CHECK_CACHE="missing"
  rm -rf "$tmpdir"
  return 1
}


has_working_systemd() {
  command_exists systemctl || return 1
  [[ -d /run/systemd/system ]] || return 1
  systemctl show-environment >/dev/null 2>&1
}

ensure_dependencies() {
  local mode="$1"
  local missing_packages=()
  local required_commands=(git install systemctl "$PYTHON_BIN" rclone curl)

  if [[ "$mode" == "docker" ]]; then
    required_commands=(git install docker curl rclone)
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
  if confirm_maybe_auto "Доустановить зависимости автоматически?" "yes"; then
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

prepare_source_checkout() {
  local chosen_source git_url git_ref
  chosen_source="$(ask_path_value_maybe_auto "Git checkout с исходниками" "${SOURCE_ROOT:-$(default_source_root)}")"
  SOURCE_ROOT="$chosen_source"

  if use_standard_settings; then
    git_url="$DEFAULT_GIT_URL"
    git_ref="$DEFAULT_GIT_REF"
  else
    git_url="$(ask_value "Git URL репозитория" "$DEFAULT_GIT_URL")"
    git_ref="$(ask_value "Git branch/tag" "$DEFAULT_GIT_REF")"
  fi

  if [[ -d "$SOURCE_ROOT/.git" ]]; then
    log "Используется существующий Git checkout: $SOURCE_ROOT"
    if confirm_maybe_auto "Обновить checkout из Git перед установкой?" "yes"; then
      git -C "$SOURCE_ROOT" fetch --all --prune
      git -C "$SOURCE_ROOT" checkout "$git_ref"
      git -C "$SOURCE_ROOT" pull --ff-only
    fi
  else
    if [[ -e "$SOURCE_ROOT" ]]; then
      die "$SOURCE_ROOT уже существует, но это не Git checkout. Укажите другой SOURCE_ROOT или удалите каталог вручную."
    fi
    install -d "$(dirname "$SOURCE_ROOT")"
    git clone --branch "$git_ref" "$git_url" "$SOURCE_ROOT"
  fi

  [[ -f "$SOURCE_ROOT/taskboard/backend/app/main.py" ]] || die "В $SOURCE_ROOT не найден taskboard/backend/app/main.py"
}

copy_runtime_bundle() {
  local source_root="$1"
  local target_root="$2"
  local target_jobs_file preserved_jobs_file=""

  target_jobs_file="$target_root/taskboard/backend/app/jobs/default_jobs.json"
  if [[ -f "$target_jobs_file" ]]; then
    preserved_jobs_file="$(mktemp /tmp/rclone-taskboard-jobs.XXXXXX)"
    cp -a "$target_jobs_file" "$preserved_jobs_file"
  fi

  install -d \
    "$target_root" \
    "$target_root/taskboard" \
    "$target_root/taskboard/backend" \
    "$target_root/taskboard/backend/app" \
    "$target_root/taskboard/data"

  cp -a "$source_root/taskboard/backend/app/." "$target_root/taskboard/backend/app/"
  find "$target_root/taskboard/backend/app" \( -type d -name __pycache__ -o -type f -name '*.pyc' \) -exec rm -rf {} +
  if [[ -n "$preserved_jobs_file" ]]; then
    install -m 0644 "$preserved_jobs_file" "$target_jobs_file"
    rm -f "$preserved_jobs_file"
  else
    rm -f "$target_jobs_file"
  fi

  install -m 0644 "$source_root/taskboard/backend/requirements.txt" "$target_root/taskboard/backend/requirements.txt"
  install -m 0644 "$source_root/taskboard/backend/app/jobs/default_jobs.example.json" "$target_root/taskboard/backend/app/jobs/default_jobs.example.json"
  if [[ -f "$source_root/taskboard/backend/app/jobs/default_jobs.empty.json" ]]; then
    install -m 0644 "$source_root/taskboard/backend/app/jobs/default_jobs.empty.json" "$target_root/taskboard/backend/app/jobs/default_jobs.empty.json"
  fi
  install -m 0755 "$source_root/install.sh" "$target_root/install.sh"
  rm -f \
    "$target_root/scripts/install-taskboard-systemd.sh" \
    "$target_root/scripts/install-taskboard-docker.sh" \
    "$target_root/scripts/migrate-embedded-watcher-systemd.sh" \
    "$target_root/systemd/${SERVICE_NAME%.service}-web.service" \
    "$target_root/systemd/rclone-taskboard.service"
  rmdir "$target_root/scripts" 2>/dev/null || true
  rmdir "$target_root/systemd" 2>/dev/null || true
  if [[ -f "$source_root/taskboard/backend/Dockerfile" ]]; then
    install -m 0644 "$source_root/taskboard/backend/Dockerfile" "$target_root/taskboard/backend/Dockerfile"
  fi
  if [[ -f "$source_root/taskboard/docker-compose.yml" ]]; then
    install -m 0644 "$source_root/taskboard/docker-compose.yml" "$target_root/taskboard/docker-compose.yml"
  fi
  if [[ -f "$source_root/taskboard/.env.docker.example" ]]; then
    install -m 0644 "$source_root/taskboard/.env.docker.example" "$target_root/taskboard/.env.docker.example"
  fi
  if [[ -f "$source_root/taskboard/.env.systemd.example" ]]; then
    install -m 0644 "$source_root/taskboard/.env.systemd.example" "$target_root/taskboard/.env.systemd.example"
  fi

}

install_initial_jobs_catalog() {
  local source_root="$1"
  local target_root="$2"
  local source_template target_jobs_file

  target_jobs_file="$target_root/taskboard/backend/app/jobs/default_jobs.json"
  if [[ -f "$target_jobs_file" ]]; then
    log "Рабочий каталог задач сохранён без изменений: $target_jobs_file"
    return 0
  fi

  case "$JOBS_TEMPLATE_MODE" in
    empty)
      source_template="$source_root/taskboard/backend/app/jobs/default_jobs.empty.json"
      ;;
    examples|"")
      source_template="$source_root/taskboard/backend/app/jobs/default_jobs.example.json"
      ;;
    *)
      die "Неизвестный режим каталога задач: $JOBS_TEMPLATE_MODE"
      ;;
  esac

  [[ -f "$source_template" ]] || die "Не найден шаблон каталога задач: $source_template"
  install -m 0644 "$source_template" "$target_jobs_file"
  log_ok "Создан рабочий каталог задач из шаблона: $(basename "$source_template")"
}

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

install_systemd_unit() {
  local source_root="$1"
  local target_root="$2"
  local escaped_target
  escaped_target="$(escape_sed_replacement "$target_root")"
  sed "s|/opt/rclone-taskboard|$escaped_target|g" \
    "$source_root/rclone-taskboard.service" > "$target_root/rclone-taskboard.service"
  install -m 0644 "$target_root/rclone-taskboard.service" "$SYSTEMD_DIR/$SERVICE_NAME"
  systemctl daemon-reload
}

remove_obsolete_taskboard_units() {
  local old_service
  for old_service in "${SERVICE_NAME%.service}-web.service"; do
    [[ "$old_service" == "$SERVICE_NAME" ]] && continue
    if systemctl is-active --quiet "$old_service" 2>/dev/null; then
      systemctl stop "$old_service" || true
    fi
    if systemctl is-enabled --quiet "$old_service" 2>/dev/null; then
      systemctl disable "$old_service" || true
    fi
    rm -f "$SYSTEMD_DIR/$old_service"
  done
  systemctl daemon-reload
}

remove_obsolete_embedded_watcher_unit() {
  local old_service="${OLD_WATCHER_SERVICE:-rclone-watch-taskboard.service}"
  if systemctl is-active --quiet "$old_service" 2>/dev/null; then
    systemctl stop "$old_service" || true
  fi
  if systemctl is-enabled --quiet "$old_service" 2>/dev/null; then
    systemctl disable "$old_service" || true
  fi
  rm -f "$SYSTEMD_DIR/$old_service"
  systemctl daemon-reload
}

install_rclone_web_service() {
  if ! has_working_systemd; then
    log_warn "systemd недоступен: настройка $RCLONE_WEB_SERVICE_NAME пропущена."
    return 0
  fi
  if systemctl list-unit-files "$RCLONE_WEB_SERVICE_NAME" --no-legend >/dev/null 2>&1; then
    log "Найден существующий $RCLONE_WEB_SERVICE_NAME: installer не меняет и не перезапускает уже настроенный rclone."
    return 0
  fi
  if [[ -f /root/.config/rclone/rclone.conf ]] && grep -Eq '^\[[^]]+\]$' /root/.config/rclone/rclone.conf; then
    log "Найден существующий /root/.config/rclone/rclone.conf: installer не меняет уже настроенный rclone."
    return 0
  fi
  if ! command_exists rclone && [[ ! -x /opt/rclone/rclone ]]; then
    log_warn "rclone не найден: настройка $RCLONE_WEB_SERVICE_NAME пропущена."
    return 0
  fi
  if ! confirm_maybe_auto "Настроить rclone Web GUI на ${RCLONE_WEB_ADDR} так же, как текущий rclone-web.service?" "yes"; then
    log "Настройка rclone Web GUI пропущена."
    return 0
  fi

  local rclone_bin rc_auth_args
  rclone_bin="$(rclone_binary_path)"
  rc_auth_args=(--rc-no-auth)

  if [[ ! "${RCLONE_WEB_NO_AUTH,,}" =~ ^(1|y|yes|true|on)$ ]]; then
    log_warn "RCLONE_WEB_NO_AUTH=$RCLONE_WEB_NO_AUTH пока не задаёт htpasswd автоматически; будет использован режим без авторизации."
  fi

  cat > "$SYSTEMD_DIR/$RCLONE_WEB_SERVICE_NAME" <<EOF
[Unit]
Description=Rclone Web GUI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=$rclone_bin rcd --rc-web-gui --rc-web-gui-no-open-browser --rc-addr $RCLONE_WEB_ADDR ${rc_auth_args[*]}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  install -d "$STATE_DIR"
  touch "$RCLONE_WEB_INSTALLED_MARKER"
  systemctl daemon-reload
  systemctl enable "$RCLONE_WEB_SERVICE_NAME"
  systemctl restart "$RCLONE_WEB_SERVICE_NAME"
  log_ok "Настроен $RCLONE_WEB_SERVICE_NAME: rclone rcd --rc-web-gui --rc-addr $RCLONE_WEB_ADDR --rc-no-auth"
}

install_or_update_systemd() {
  initialize_install_preferences
  need_root
  if ! has_working_systemd; then
    die "Режим systemd недоступен: в этой системе нет рабочего systemd (PID 1). Используйте Docker или ручной запуск backend."
  fi
  TARGET_ROOT="$(ask_path_value_maybe_auto "Каталог установки runtime" "$TARGET_ROOT")"
  ensure_dependencies systemd
  prepare_source_checkout
  choose_jobs_template_mode

  if confirm_maybe_auto "Выполнить переход с legacy и удалить старые скрипты/unit'ы?" "no"; then
    cleanup_legacy
  fi

  copy_runtime_bundle "$SOURCE_ROOT" "$TARGET_ROOT"
  install_initial_jobs_catalog "$SOURCE_ROOT" "$TARGET_ROOT"
  if [[ ! -f "$TARGET_ROOT/taskboard/.env" ]]; then
    install -m 0644 "$SOURCE_ROOT/taskboard/.env.systemd.example" "$TARGET_ROOT/taskboard/.env"
  fi

  "$PYTHON_BIN" -m venv "$TARGET_ROOT/taskboard/.venv"
  "$TARGET_ROOT/taskboard/.venv/bin/pip" install --upgrade pip
  "$TARGET_ROOT/taskboard/.venv/bin/pip" install -r "$TARGET_ROOT/taskboard/backend/requirements.txt"

  install_systemd_unit "$SOURCE_ROOT" "$TARGET_ROOT"
  remove_obsolete_taskboard_units
  remove_obsolete_embedded_watcher_unit
  install_rclone_web_service
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"

  log "Systemd установка/обновление завершены."
  print_access_summary "systemd"
}

install_or_update_docker() {
  initialize_install_preferences
  need_root
  TARGET_ROOT="$(ask_path_value_maybe_auto "Каталог установки runtime" "$TARGET_ROOT")"
  ensure_dependencies docker
  prepare_source_checkout
  choose_jobs_template_mode

  if confirm_maybe_auto "Выполнить переход с legacy и удалить старые скрипты/unit'ы?" "no"; then
    cleanup_legacy
  fi

  copy_runtime_bundle "$SOURCE_ROOT" "$TARGET_ROOT"
  install_initial_jobs_catalog "$SOURCE_ROOT" "$TARGET_ROOT"
  if [[ ! -f "$TARGET_ROOT/taskboard/.env.docker" ]]; then
    install -m 0644 "$SOURCE_ROOT/taskboard/.env.docker.example" "$TARGET_ROOT/taskboard/.env.docker"
  fi

  install_rclone_web_service

  (
    cd "$TARGET_ROOT/taskboard"
    docker_compose --env-file .env.docker up -d --build
  )

  log "Docker установка/обновление завершены."
  print_access_summary "docker"
}

backup_path() {
  local backup_root="$1"
  local source="$2"
  local target="$backup_root$source"
  if [[ -e "$source" || -L "$source" ]]; then
    install -d "$(dirname "$target")"
    cp -a "$source" "$target"
  fi
}

expand_purge_packages() {
  local packages=("$@")
  local expanded=("${packages[@]}")
  local pkg

  if printf '%s\n' "${packages[@]}" | grep -qx 'python3-venv'; then
    while IFS= read -r pkg; do
      [[ -n "$pkg" ]] || continue
      expanded+=("$pkg")
    done < <(
      dpkg-query -W -f='${binary:Package}\n' 'python3.*-venv' 2>/dev/null         | grep -E '^python3(\.[0-9]+)?-venv$' || true
    )
  fi

  printf '%s\n' "${expanded[@]}" | awk 'NF && !seen[$0]++'
}

cleanup_legacy() {
  need_root
  local stamp backup_root legacy_backups=()
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup_root="${BACKUP_ROOT:-$TARGET_ROOT/migration-backups/$stamp}"

  shopt -s nullglob
  legacy_backups=(/usr/local/bin/rclone-backup.sh.bak.*)
  shopt -u nullglob

  log "Будет сделан backup legacy-файлов в: $backup_root"
  log "Legacy unit'ы: ${LEGACY_UNITS[*]}"
  log "Legacy scripts: ${LEGACY_FILES[*]}"
  if [[ "${#legacy_backups[@]}" -gt 0 ]]; then
    log "Backup-файлы старого rclone-backup.sh: ${legacy_backups[*]}"
  fi

  if ! confirm "Продолжить backup + остановку + удаление legacy?" "no"; then
    log "Legacy migration пропущена."
    return 0
  fi

  install -d "$backup_root"
  for unit in "${LEGACY_UNITS[@]}"; do
    if has_working_systemd; then
      systemctl cat "$unit" > "$backup_root/${unit}.systemctl-cat.txt" 2>/dev/null || true
      systemctl status "$unit" --no-pager > "$backup_root/${unit}.status.txt" 2>/dev/null || true
    fi
    backup_path "$backup_root" "$SYSTEMD_DIR/$unit"
  done
  for path in "${LEGACY_FILES[@]}" "${legacy_backups[@]}"; do
    backup_path "$backup_root" "$path"
  done

  for unit in "${LEGACY_UNITS[@]}"; do
    if has_working_systemd; then
      systemctl disable --now "$unit" 2>/dev/null || true
    else
      log_warn "systemd недоступен: unit $unit будет только удалён с диска."
    fi
    rm -f "$SYSTEMD_DIR/$unit"
  done
  for path in "${LEGACY_FILES[@]}" "${legacy_backups[@]}"; do
    rm -f -- "$path"
  done
  if has_working_systemd; then
    systemctl daemon-reload
  fi

  log "Legacy migration завершена."
  log "Backup snapshot: $backup_root"
}

uninstall_taskboard() {
  need_root
  TARGET_ROOT="$(ask_path_value "Каталог установленного runtime" "$TARGET_ROOT")"

  log "Будет остановлен и отключен $SERVICE_NAME, если он установлен."
  if confirm "Продолжить удаление taskboard-служб?" "no"; then
    if has_working_systemd; then
      systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
      rm -f "$SYSTEMD_DIR/$SERVICE_NAME"
      if [[ -f "$RCLONE_WEB_INSTALLED_MARKER" ]]; then
        systemctl disable --now "$RCLONE_WEB_SERVICE_NAME" 2>/dev/null || true
        rm -f "$SYSTEMD_DIR/$RCLONE_WEB_SERVICE_NAME"
        rm -f "$RCLONE_WEB_INSTALLED_MARKER"
      else
        log "Существующий $RCLONE_WEB_SERVICE_NAME не удаляется: он не был создан этим installer."
      fi
      systemctl daemon-reload || true
      systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
      [[ -f "$RCLONE_WEB_INSTALLED_MARKER" ]] && systemctl reset-failed "$RCLONE_WEB_SERVICE_NAME" 2>/dev/null || true
    else
      log_warn "systemd недоступен: service не может быть остановлен через systemctl, будет только удалён unit-файл."
      rm -f "$SYSTEMD_DIR/$SERVICE_NAME"
      if [[ -f "$RCLONE_WEB_INSTALLED_MARKER" ]]; then
        rm -f "$SYSTEMD_DIR/$RCLONE_WEB_SERVICE_NAME" "$RCLONE_WEB_INSTALLED_MARKER"
      else
        log "Существующий $RCLONE_WEB_SERVICE_NAME не удаляется: он не был создан этим installer."
      fi
    fi
  fi

  if [[ -f "$TARGET_ROOT/taskboard/docker-compose.yml" ]]; then
    if command_exists docker && { docker compose version >/dev/null 2>&1 || command_exists docker-compose; }; then
      if confirm "Остановить docker compose stack в $TARGET_ROOT/taskboard?" "yes"; then
        (
          cd "$TARGET_ROOT/taskboard"
          docker_compose --env-file .env.docker down || true
        )
      fi
    else
      log_warn "Docker/Compose недоступен: остановка compose stack пропущена."
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
    local purge_packages=() expanded_purge_packages=()
    while IFS= read -r pkg; do
      [[ -n "$pkg" ]] || continue
      purge_packages+=("$pkg")
    done < "$APT_INSTALLED_RECORD"

    if [[ "${#purge_packages[@]}" -gt 0 ]]; then
      mapfile -t expanded_purge_packages < <(expand_purge_packages "${purge_packages[@]}")
      log "Найден список apt-пакетов, установленных этим скриптом: ${expanded_purge_packages[*]}"
      if confirm "Попробовать apt purge этих пакетов?" "no"; then
        apt-get purge -y "${expanded_purge_packages[@]}" || log_warn "apt purge завершился с ошибкой."
        invalidate_runtime_caches
        apt-get autoremove -y || log_warn "apt autoremove завершился с ошибкой."
        invalidate_runtime_caches
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

  if has_working_systemd; then
    status_line="${C_GREEN}ok${C_RESET}"
  else
    status_line="${C_RED}unavailable${C_RESET}"
  fi
  printf '    - %-14s : %b\n' "systemd-host" "$status_line"

  if command_exists "$PYTHON_BIN"; then
    if check_python_venv; then
      status_line="${C_GREEN}ok${C_RESET}"
    else
      status_line="${C_RED}missing${C_RESET}"
    fi
    printf '    - %-14s : %b (pkg: %s)\n' "python3-venv" "$status_line" "python3-venv"
  fi
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
  invalidate_runtime_caches
  log ""
  log_section "Текущий статус"
  if has_working_systemd; then
    log "  systemd host: ${C_GREEN}доступен${C_RESET}"
    if systemctl list-unit-files "$SERVICE_NAME" --no-legend >/dev/null 2>&1; then
      log "  systemd unit: $SERVICE_NAME ${C_GREEN}найден${C_RESET}"
      systemctl is-active --quiet "$SERVICE_NAME" && log "  active: yes" || log_warn "active: no"
    else
      log "  systemd unit: $SERVICE_NAME ${C_RED}не найден${C_RESET}"
    fi
    if systemctl list-unit-files "$RCLONE_WEB_SERVICE_NAME" --no-legend >/dev/null 2>&1; then
      log "  rclone web unit: $RCLONE_WEB_SERVICE_NAME ${C_GREEN}найден${C_RESET}"
      systemctl is-active --quiet "$RCLONE_WEB_SERVICE_NAME" && log "  rclone web active: yes" || log_warn "rclone web active: no"
    else
      log "  rclone web unit: $RCLONE_WEB_SERVICE_NAME ${C_RED}не найден${C_RESET}"
    fi
  else
    log "  systemd host: ${C_RED}недоступен${C_RESET} (${C_DIM}нет рабочего systemd / PID 1${C_RESET})"
    log "  systemd unit: проверка пропущена"
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
    local systemd_menu_line
    print_status
    if has_working_systemd; then
      systemd_menu_line="  1) Установить/обновить через systemd"
    else
      systemd_menu_line="  ${C_DIM}${C_RED}1) Установить/обновить через systemd [недоступно: нет рабочего systemd]${C_RESET}"
    fi
    printf '%s
' "Выберите действие:"
    printf '%b
' "$systemd_menu_line"
    cat <<'MENU'
  2) Установить/обновить через Docker
  3) Только переход с legacy: backup + удалить старые legacy-скрипты и unit'ы
  4) Удалить taskboard-установку
  5) Выйти
MENU
    local choice
    read -r -p "Номер действия [1-5]: " choice
    case "$choice" in
      1)
        if has_working_systemd; then
          install_or_update_systemd
        else
          log_err "Пункт 1 недоступен: в этой системе нет рабочего systemd."
        fi
        ;;
      2) install_or_update_docker ;;
      3) TARGET_ROOT="$(ask_path_value "Каталог для migration-backups" "$TARGET_ROOT")"; cleanup_legacy ;;
      4) uninstall_taskboard ;;
      5|q|quit|exit) exit 0 ;;
      *) log "Неизвестный выбор: $choice" ;;
    esac
  done
}

setup_colors
trap on_error ERR

case "${1:-}" in
  systemd) install_or_update_systemd ;;
  docker) install_or_update_docker ;;
  legacy-cleanup|migrate-legacy) TARGET_ROOT="$(ask_path_value "Каталог для migration-backups" "$TARGET_ROOT")"; cleanup_legacy ;;
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
  JOBS_TEMPLATE_MODE=${JOBS_TEMPLATE_MODE:-examples|empty}
  RCLONE_WEB_SERVICE_NAME=$RCLONE_WEB_SERVICE_NAME
  RCLONE_WEB_ADDR=$RCLONE_WEB_ADDR
EOF
    exit 2
    ;;
esac
