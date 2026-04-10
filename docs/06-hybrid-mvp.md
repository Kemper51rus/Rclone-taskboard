# ⚙️ Configuration

Этот документ описывает рабочий каталог задач и основные переменные окружения.

---

## 📄 Jobs Catalog

### Файлы

| Файл | Роль |
| --- | --- |
| `hybrid/backend/app/jobs/default_jobs.example.json` | Шаблон каталога, хранится в Git |
| `hybrid/backend/app/jobs/default_jobs.json` | Рабочий каталог, создаётся из шаблона |

### Основные секции

Catalog содержит:

- `profiles`
- `gotify`
- `queues`
- `bandwidth`
- `logging`
- `clouds`
- `jobs`

---

## 🛠️ Что настраивается

### Jobs

- key и порядок выполнения
- source path
- destination path
- cloud binding
- transfer mode: `copy` или `sync`
- timeout
- schedule
- notifications
- retention policy

### Queues

- параллельное выполнение профилей
- queueing для scheduler
- queueing для watcher
- число workers в каждой очереди
- отдельные лимиты скорости для очередей

### Clouds

- remote metadata
- provider
- remote name
- endpoint
- root path
- optional extra config

---

## 🚀 Bootstrap Behavior

Если рабочий каталог отсутствует, приложение автоматически создаёт его по схеме:

```text
default_jobs.example.json -> default_jobs.json
```

Это упрощает первый запуск и не требует заранее готовить рабочий JSON-файл.

---

## 🌍 Environment Variables

| Переменная | Назначение |
| --- | --- |
| `HYBRID_APP_NAME` | Публичное имя приложения |
| `APP_ROOT` | Корневой рабочий каталог |
| `HYBRID_DB_PATH` | Путь к SQLite |
| `HYBRID_JOBS_FILE` | Путь к рабочему каталогу |
| `HYBRID_RCLONE_CONFIG` | Путь к `rclone.conf` |
| `APP_TIMEZONE` | Таймзона приложения |
| `HYBRID_ENABLE_SCHEDULER` | Включение scheduler |
| `HYBRID_STANDARD_INTERVAL_MINUTES` | Интервал стандартных задач |
| `HYBRID_HEAVY_HOUR` | Час heavy-задач |
| `HYBRID_EVENT_DEBOUNCE_SECONDS` | Debounce окно для событий |
| `HYBRID_DEFAULT_TIMEOUT_SECONDS` | Таймаут команд по умолчанию |
| `HYBRID_OUTPUT_TAIL_CHARS` | Размер сохраняемого tail вывода |
| `HYBRID_DRY_RUN` | Dry-run режим |
| `HYBRID_API_TOKEN` | Токен для write access |
| `HYBRID_API_URL` | URL API для watcher |

---

## 🔄 Что сделать после bootstrap

1. Проверить сгенерированный `default_jobs.json`
2. Проверить, что список облаков корректно читается из `rclone.conf`
3. Проверить destination paths и schedules
4. Проверить retention policies
5. Проверить настройки очередей, логирования и лимитов скорости
