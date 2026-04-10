# 📦 Deployment

Проект поддерживает два варианта развертывания:

- `docker`
- `systemd`

---

## 🐳 Docker Deployment

В Docker-режиме запускается один сервис:

- `hybrid-web`

### Требования

- Docker с Compose
- Доступные host bind mounts:
  - `/media`
  - `/srv`
  - `/root/.config/rclone`

### Подготовка

```bash
cd hybrid
cp .env.docker.example .env.docker
```

Проверьте:

- `HYBRID_RCLONE_CONFIG`
- `APP_TIMEZONE`
- `HYBRID_API_TOKEN`

### Запуск

```bash
docker compose --env-file .env.docker up -d --build
```

### Installer Script

```bash
./scripts/install-hybrid-docker.sh /opt/rclone-hybrid
```

---

## 🖥️ Systemd Deployment

В режиме `systemd` backend, scheduler и watcher работают внутри одного web-сервиса.

### Требования

- `python3`
- `python3-venv`
- `rclone`
- `curl`
- `systemd`

### Подготовка

```bash
cp hybrid/.env.systemd.example hybrid/.env
```

Проверьте:

- `HYBRID_DB_PATH`
- `HYBRID_JOBS_FILE`
- `HYBRID_RCLONE_CONFIG`
- `HYBRID_WATCHER_DEBOUNCE_SECONDS`

### Установка

```bash
./scripts/install-hybrid-systemd.sh /opt/rclone-hybrid
```

### Включение сервисов

```bash
systemctl enable --now rclone-hybrid-web.service
```

### Переход со старого external watcher

Если на хосте раньше был отдельный `rclone-watch-hybrid.service`, выполните разовый migration:

```bash
/opt/rclone-hybrid/scripts/migrate-embedded-watcher-systemd.sh /opt/rclone-hybrid
systemctl restart rclone-hybrid-web.service
```

Скрипт останавливает и отключает старый watcher-service, удаляет устаревший unit-файл и оставляет только встроенный watcher внутри `rclone-hybrid-web.service`.

---

## ✅ Post-Deployment Checklist

Проверьте:

- `GET /api/health`
- `GET /api/state`
- ручной запуск профиля или задачи
- создание SQLite database
- создание `default_jobs.json` при чистом старте

---

## 🆚 Выбор режима

| Режим | Когда подходит лучше |
| --- | --- |
| `docker` | Удобнее контейнерный запуск |
| `systemd` | Нужна прямая интеграция с системой и запуск на хосте |
