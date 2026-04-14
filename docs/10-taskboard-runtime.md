# 📦 Taskboard Runtime

Каталог `taskboard/` содержит приложение, шаблоны окружения и файлы для развертывания.

---

## 📁 Содержимое

| Путь | Назначение |
| --- | --- |
| `taskboard/backend/app/main.py` | FastAPI entrypoint |
| `taskboard/backend/app/orchestrator.py` | Scheduler, queues, workers |
| `taskboard/backend/app/storage.py` | SQLite persistence |
| `taskboard/backend/app/jobs/default_jobs.example.json` | Шаблон рабочего каталога |
| `taskboard/backend/app/jobs/default_jobs.empty.json` | Пустой шаблон рабочего каталога без задач |
| `taskboard/.env.docker.example` | Шаблон env для Docker |
| `taskboard/.env.systemd.example` | Шаблон env для systemd |
| `taskboard/docker-compose.yml` | Docker-стек |

---

## 🚀 Bootstrap

При чистом старте приложение создаёт:

```text
taskboard/backend/app/jobs/default_jobs.json
```

из шаблона:

```text
taskboard/backend/app/jobs/default_jobs.example.json
```

---

## ⚙️ Важные переменные

- `TASKBOARD_APP_NAME`
- `TASKBOARD_DB_PATH`
- `TASKBOARD_JOBS_FILE`
- `TASKBOARD_RCLONE_CONFIG`
- `TASKBOARD_API_TOKEN`
- `TASKBOARD_WATCHER_DEBOUNCE_SECONDS`
- `TASKBOARD_COPY_STARTUP_DELAY_SECONDS`
- `TASKBOARD_COPY_MIN_START_INTERVAL_SECONDS`
- `TASKBOARD_ENABLE_SCHEDULER`
- `TASKBOARD_STANDARD_INTERVAL_MINUTES`
- `TASKBOARD_HEAVY_HOUR`

---

## 🧩 Runtime Features

- structured `rclone`-опции у backup и retention задач:
  `transfers`, `checkers`, `tpslimit`, `tpslimit_burst`, `retries`, `low_level_retries`,
  `retries_sleep`, `fast_list`, `no_traverse`, `debug_dump`, `extra_args`
- `Mail.ru safe preset` в редакторе backup-задач
- сериализация запусков для Mail.ru remote на вкладке `Облака`
- ручное и автоматическое step-логирование `rclone`

---

## 📖 API Surface

Полное описание вынесено в `docs/04-api-reference.md`.

---

## 📘 Связанные документы

- [Руководство по развертыванию](07-deployment.md)
- [Служебные заметки для разработки](08-development-notes.md)
- [Архивные материалы по legacy](09-legacy-migration.md)
