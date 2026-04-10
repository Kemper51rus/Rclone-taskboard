# 📦 Hybrid Runtime

Каталог `hybrid/` содержит приложение, шаблоны окружения и файлы для развертывания.

---

## 📁 Содержимое

| Путь | Назначение |
| --- | --- |
| `backend/app/main.py` | FastAPI entrypoint |
| `backend/app/orchestrator.py` | Scheduler, queues, workers |
| `backend/app/storage.py` | SQLite persistence |
| `backend/app/jobs/default_jobs.example.json` | Шаблон рабочего каталога |
| `.env.docker.example` | Шаблон env для Docker |
| `.env.systemd.example` | Шаблон env для systemd |
| `docker-compose.yml` | Docker-стек |

---

## 🚀 Bootstrap

При чистом старте приложение создаёт:

```text
backend/app/jobs/default_jobs.json
```

из шаблона:

```text
backend/app/jobs/default_jobs.example.json
```

---

## ⚙️ Важные переменные

- `HYBRID_APP_NAME`
- `HYBRID_DB_PATH`
- `HYBRID_JOBS_FILE`
- `HYBRID_RCLONE_CONFIG`
- `HYBRID_API_TOKEN`
- `HYBRID_API_URL`
- `HYBRID_ENABLE_SCHEDULER`
- `HYBRID_STANDARD_INTERVAL_MINUTES`
- `HYBRID_HEAVY_HOUR`

---

## 📖 API Surface

- `GET /`
- `GET /api/health`
- `GET /api/state`
- `GET /api/jobs`
- `GET /api/gotify`
- `GET /api/queues`
- `GET /api/bandwidth`
- `GET /api/logging`
- `GET /api/logging/rclone-tail`
- `GET /api/clouds`
- `GET /api/fs/browse`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `PUT /api/gotify`
- `PUT /api/queues`
- `PUT /api/bandwidth`
- `PUT /api/logging`
- `DELETE /api/logging/rclone-log`
- `POST /api/runs`
- `POST /api/runs/job/{job_key}`
- `POST /api/run-steps/{step_id}/control`
- `POST /api/triggers/event`
- `PUT /api/backups`
- `PUT /api/jobs`

Подробное описание находится в `docs/04-api-reference.md`.

---

## 📘 Связанные документы

- [Руководство по развертыванию](/root/projects/rclone-web-ui/rclone/docs/07-deployment.md)
- [Архивные материалы по legacy](/root/projects/rclone-web-ui/rclone/legacy/README.md)
