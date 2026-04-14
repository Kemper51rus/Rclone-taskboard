# ⚙️ Configuration

Этот документ описывает рабочий каталог задач и основные переменные окружения.

---

## 📄 Jobs Catalog

### Файлы

| Файл | Роль |
| --- | --- |
| `taskboard/backend/app/jobs/default_jobs.example.json` | Шаблон каталога, хранится в Git |
| `taskboard/backend/app/jobs/default_jobs.empty.json` | Пустой шаблон без задач для чистой установки |
| `taskboard/backend/app/jobs/default_jobs.json` | Рабочий каталог, создаётся из шаблона |

### Основные секции

Catalog содержит:

- `profiles`
- `gotify`
- `queues`
- `bandwidth`
- `logging`
- `watcher`
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
- structured `rclone`-опции у backup и retention:
  `transfers`, `checkers`, `tpslimit`, `tpslimit_burst`, `retries`, `low_level_retries`,
  `retries_sleep`, `fast_list`, `no_traverse`, `debug_dump`, `extra_args`
- `exclude patterns` с масками `rclone`, например `*.tmp`, `vzdump-qemu-400*`, `**/cache/**`
- `exclude_paths` для выбора нескольких файлов или каталогов внутри исходного каталога
- `Mail.ru safe preset` для снижения параллелизма и частоты API-запросов
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

### Logging

- глобальное включение подробного `rclone`-лога
- автоматическое включение подробного `rclone`-лога для backup-задачи по настраиваемому порогу подряд неуспешных запусков
- автоматическое отключение этого auto-режима после такого же порога подряд успешных запусков той же backup-задачи
- debug-режим с `--dump headers|headers,bodies` и `--log-level DEBUG` для конкретной задачи

### Watcher

- глобальное включение watcher
- debounce между повторами событий
- включение watcher у отдельных backup-задач

### Clouds

- remote metadata
- provider
- remote name
- endpoint
- root path
- optional extra config
- app-level флаг `serialize_provider_lock` для сериализации запусков Mail.ru remote

---

## 🚀 Bootstrap Behavior

Если рабочий каталог отсутствует, приложение автоматически создаёт его по схеме:

```text
default_jobs.example.json -> default_jobs.json
```

Это упрощает первый запуск и не требует заранее готовить рабочий JSON-файл.

Installer при чистой установке предлагает выбрать стартовый каталог:

- `examples` — каталог с примерами из `default_jobs.example.json`
- `empty` — пустой каталог без задач из `default_jobs.empty.json`

---

## 🌍 Environment Variables

| Переменная | Назначение |
| --- | --- |
| `TASKBOARD_APP_NAME` | Публичное имя приложения |
| `APP_ROOT` | Корневой рабочий каталог |
| `TASKBOARD_DB_PATH` | Путь к SQLite |
| `TASKBOARD_JOBS_FILE` | Путь к рабочему каталогу |
| `TASKBOARD_RCLONE_CONFIG` | Путь к `rclone.conf` |
| `APP_TIMEZONE` | Таймзона приложения |
| `TASKBOARD_ENABLE_SCHEDULER` | Включение scheduler |
| `TASKBOARD_STANDARD_INTERVAL_MINUTES` | Интервал стандартных задач |
| `TASKBOARD_HEAVY_HOUR` | Час heavy-задач |
| `TASKBOARD_WATCHER_DEBOUNCE_SECONDS` | Начальное значение debounce для watcher |
| `TASKBOARD_COPY_STARTUP_DELAY_SECONDS` | Задержка перед первым стартом backup/sync после запуска backend |
| `TASKBOARD_COPY_MIN_START_INTERVAL_SECONDS` | Минимальный интервал между стартами backup/sync по всей системе |
| `TASKBOARD_DEFAULT_TIMEOUT_SECONDS` | Таймаут команд по умолчанию |
| `TASKBOARD_OUTPUT_TAIL_CHARS` | Размер сохраняемого tail вывода |
| `TASKBOARD_DRY_RUN` | Dry-run режим |
| `TASKBOARD_API_TOKEN` | Токен для write access |

---

## 🔄 Что сделать после bootstrap

1. Проверить сгенерированный `default_jobs.json`
2. Проверить, что список облаков корректно читается из `rclone.conf`
3. Проверить destination paths и schedules
4. Проверить retention policies
5. Проверить настройки очередей, watcher, логирования и лимитов скорости
6. Для Mail.ru remote при необходимости включить safe preset у задач и сериализацию на вкладке `Облака`
