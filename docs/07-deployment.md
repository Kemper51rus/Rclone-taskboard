# 📦 Deployment

Проект поддерживает два варианта развертывания:

- `docker`
- `systemd`

---

## 🐳 Docker Deployment

В Docker-режиме запускаются два сервиса:

- `hybrid-web`
- `hybrid-watch`

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

В режиме `systemd` web-сервис и watcher запускаются напрямую на хосте.

### Требования

- `python3`
- `python3-venv`
- `rclone`
- `curl`
- `inotifywait`
- `systemd`

### Подготовка

```bash
cp hybrid/.env.systemd.example hybrid/.env
```

Проверьте:

- `HYBRID_DB_PATH`
- `HYBRID_JOBS_FILE`
- `HYBRID_RCLONE_CONFIG`
- `HYBRID_API_URL`

### Установка

```bash
./scripts/install-hybrid-systemd.sh /opt/rclone-hybrid
```

### Включение сервисов

```bash
systemctl enable --now rclone-hybrid-web.service
systemctl enable --now rclone-watch-hybrid.service
```

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
