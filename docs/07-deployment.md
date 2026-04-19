# 📦 Deployment

Проект поддерживает два варианта развертывания:

- `docker`
- `systemd`

---

## 🐳 Docker Deployment

В Docker-режиме запускается один сервис:

- `taskboard-web`

### Требования

- Docker с Compose
- Доступные host bind mounts:
  - `/media`
  - `/srv`
  - `/root/.config/rclone`

### Подготовка

```bash
cd taskboard
cp .env.docker.example .env.docker
```

Проверьте:

- `TASKBOARD_RCLONE_CONFIG`
- `APP_TIMEZONE`
- `TASKBOARD_API_TOKEN`

### Запуск

```bash
docker compose --env-file .env.docker up -d --build
```

### Installer Script

```bash
sudo ./install.sh docker
```

---

## 🖥️ Systemd Deployment

В режиме `systemd` backend, scheduler и watcher работают внутри одного web-сервиса.

## Единый installer

Основной способ установки и обслуживания:

```bash
sudo ./install.sh
```

Скрипт работает как интерактивное меню и умеет:

- поставить или обновить deployment через `systemd`
- поставить или обновить deployment через `docker`
- подтянуть исходники из Git перед установкой
- проверить зависимости и предложить доустановить недостающие
- выбрать начальный каталог задач: с примерами или пустой список задач без шаблона
- настроить отдельный `rclone-web.service` для Rclone Web GUI, если rclone ещё не настроен
- выполнить переход с legacy: сделать backup, остановить и удалить старые legacy-скрипты и unit'ы
- удалить taskboard-установку при повторном запуске

Installer не записывает `rclone.conf`.
Если на хосте уже есть `/root/.config/rclone/rclone.conf` с remotes или уже установлен `rclone-web.service`, installer не меняет и не перезапускает rclone.
Если rclone Web GUI unit создаётся впервые, он повторяет текущую локальную схему запуска: `rclone rcd --rc-web-gui --rc-addr :3000 --rc-no-auth`.
В итогах установки выводится LAN-адрес taskboard и, если unit есть, LAN-адрес Rclone Web GUI.

Legacy-cleanup покрывает старые файлы:

```text
/usr/local/bin/rclone-backup.sh
/usr/local/bin/rclone-watch.sh
/usr/local/bin/rclone-backup-status.sh
/usr/local/bin/rclone-backup.sh.bak.*
/etc/systemd/system/rclone-backup.service
/etc/systemd/system/rclone-backup.timer
/etc/systemd/system/rclone-watch.service
```

Для неинтерактивного запуска доступны команды:

```bash
sudo ./install.sh systemd
sudo ./install.sh docker
sudo ./install.sh migrate-legacy
sudo ./install.sh uninstall
```

### Требования

- `python3`
- `python3-venv`
- `rclone`
- `curl`
- `systemd`

### Подготовка

```bash
cp taskboard/.env.systemd.example taskboard/.env
```

Проверьте:

- `TASKBOARD_DB_PATH`
- `TASKBOARD_JOBS_FILE`
- `TASKBOARD_RCLONE_CONFIG`
- `TASKBOARD_WATCHER_DEBOUNCE_SECONDS`
- `TASKBOARD_COPY_STARTUP_DELAY_SECONDS`
- `TASKBOARD_COPY_MIN_START_INTERVAL_SECONDS`

### Установка

```bash
sudo ./install.sh systemd
```

### Включение сервисов

```bash
systemctl status rclone-taskboard.service --no-pager
```

Unit `rclone-taskboard.service` задаёт `LimitNOFILE=8192`.
Это запас для backend-процесса, scheduler, watcher и SQLite/WAL-файлов. Нормальная работа не должна приближаться к этому лимиту: текущие значения видны в разделе `Статистика` и в `GET /api/system`.

### Переход со старого external watcher

Если на хосте раньше был legacy pipeline или отдельный watcher-service, выполните migration через единый installer:

```bash
sudo ./install.sh migrate-legacy
```

Скрипт делает backup, останавливает и отключает старые unit'ы, удаляет устаревшие legacy-скрипты и unit'ы и оставляет только встроенный watcher внутри `rclone-taskboard.service`.

---

## ✅ Post-Deployment Checklist

Проверьте:

- `GET /api/health`
- `GET /api/state`
- `GET /api/system`
- ручной запуск профиля или задачи
- создание SQLite database
- создание `default_jobs.json` при чистом старте

---

## 🆚 Выбор режима

| Режим | Когда подходит лучше |
| --- | --- |
| `docker` | Удобнее контейнерный запуск |
| `systemd` | Нужна прямая интеграция с системой и запуск на хосте |
