# ⚙️ Runtime Behavior

Этот документ описывает, как в приложении создаются, ставятся в очередь, исполняются и отображаются запуски.

---

## 🧭 Источники запусков

Запуск может быть создан из трёх источников:

1. Scheduler
2. Dashboard или API
3. Filesystem watcher

---

## 👀 Event-Driven Flow

Watcher используется для реакции на изменения в файловой системе почти в реальном времени.

### Последовательность

1. `scripts/rclone-watch-hybrid.sh` отслеживает директории через `inotifywait`
2. Watcher отправляет `POST /api/triggers/event`
3. API сохраняет событие
4. Debounce-логика отсекает шумные серии событий
5. Если это разрешено настройками очередей, создаётся запуск профиля `standard`
6. Worker исполняет соответствующие задачи

---

## 🕒 Scheduled Flow

Scheduler работает внутри web-сервиса.

### Последовательность

1. Scheduler проверяет задачи каждую минуту
2. Когда наступает нужное время, создаётся запуск
3. Запуск попадает в нужную очередь
4. Worker исполняет его шаги

---

## 🧵 Профили и очереди

| Профиль | Назначение |
| --- | --- |
| `standard` | Частые и короткие задачи |
| `heavy` | Долгие и ресурсоёмкие задачи |
| `all` | Сводный запуск всех очередей из UI |

Поведение очередей определяется секцией `queues` в рабочем каталоге:

- `allow_parallel_profiles`
- `allow_scheduler_queueing`
- `allow_event_queueing`
- `definitions` с ключами очередей, числом workers и лимитами скорости

---

## 💾 Runtime State

### Runtime Catalog

- `hybrid/backend/app/jobs/default_jobs.json`

### SQLite Database

- путь задаётся через `HYBRID_DB_PATH`
- значение по умолчанию зависит от выбранного способа развертывания

В базе хранятся:

- runs
- step execution history
- events
- служебное состояние scheduler

---

## 📊 Наблюдаемость

### UI

- Dashboard доступен по `/`

### API

- `GET /api/health`
- `GET /api/state`
- `GET /api/jobs`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `POST /api/runs`
- `POST /api/triggers/event`

Полный список endpoints вынесен в `docs/04-api-reference.md`.

### Что сохраняется по каждому шагу

- status
- exit code
- duration
- stdout tail
- stderr tail
