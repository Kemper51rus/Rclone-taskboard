# 📖 API Reference

Документ описывает текущее API приложения по состоянию исходного кода `taskboard/backend/app/main.py`.

Ниже приведён краткий обзор текущего API без сокращений и устаревших endpoints.

`TASKBOARD_API_TOKEN` используется как токен для операций записи во внешних интеграциях и deployment-сценариях.

---

## 🌐 Backend root и frontend

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/` | Backend health/info JSON: сервис, компонент, статус и ссылка на `/api/health` |

HTML dashboard и `/favicon.svg` теперь отдаются отдельным frontend-сервисом из `taskboard/frontend/static`.
Новый frontend должен работать с backend через `/api/*`; штатный frontend-сервис проксирует этот префикс к backend.

---

## 🩺 Health и состояние

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/health` | Проверка доступности сервиса |
| `GET` | `/api/homepage` | Лёгкий снимок для Homepage/customapi без полной загрузки dashboard-state |
| `GET` | `/api/state` | Общее состояние, очереди, workers, последние запуски и флаг `token_required` |
| `GET` | `/api/jobs` | Полный рабочий каталог: профили, очереди, Gotify, bandwidth, logging, watcher, clouds и jobs |
| `GET` | `/api/stats/summary` | Сводная статистика запусков и передачи за выбранный период |
| `GET` | `/api/system` | Диагностика SQLite-базы и процесса backend |
| `POST` | `/api/system/database/checkpoint` | Сбросить WAL-журнал SQLite через checkpoint |
| `POST` | `/api/system/database/vacuum` | Сжать SQLite-базу через `VACUUM` |

### Важные поля `GET /api/homepage`

Endpoint предназначен для внешних панелей вроде Homepage. Он не отдаёт историю запусков, каталог задач и полную диагностику процесса, поэтому его можно опрашивать чаще, чем `/api/state`.
Runtime-поля обновляются на каждый запрос, а медленные поля БД и каталога задач кэшируются на `slow_cache_seconds`.

- `open_runs_total` — число незавершённых запусков
- `standard_queue_size`, `heavy_queue_size` — размеры основных очередей
- `total_copy_speed_bytes_per_second` — суммарная скорость активных `copy/sync` задач в байтах в секунду
- `total_copy_speed_megabits_per_second` — та же скорость в Мбит/с
- `jobs_total`, `enabled_jobs_total` — количество задач в каталоге
- `database_total_size_bytes` — общий размер `taskboard.db`, `taskboard.db-wal` и `taskboard.db-shm`
- `database_size_bytes` — размер основного SQLite-файла
- `database_wal_size_bytes` — размер WAL-журнала
- `database_reclaimable_bytes` — примерный объём, который может вернуть `VACUUM`
- `database_journal_mode` — текущий режим журнала SQLite
- `generated_at` — время сборки ответа
- `slow_cache_seconds` — TTL кэша для медленных полей БД и каталога задач

### Важные поля `GET /api/state`

- `queue_statuses` — состояние очередей
- `copy_progress` — активные и ожидающие backup/copy шаги для progress UI
- `total_copy_speed_bytes_per_second` — суммарная скорость всех активных `copy/sync` задач в байтах в секунду
- `total_copy_speed_megabits_per_second` — та же суммарная скорость в Мбит/с
- `active_operations` — список открытых операций
- `latest_runs` — последние запуски
- `backup_jobs` — backup-задачи каталога
- `watcher` — runtime-статус встроенного наблюдателя
- `system.database` — размер SQLite-файлов, WAL, freelist и режим журнала
- `system.process` — PID, uptime, память и открытые файловые дескрипторы backend-процесса

### Query params `GET /api/stats/summary`

- `period` — `day`, `week`, `month`, `year`
- `job_key` — необязательный ключ задачи; если задан, статистика считается только по этой задаче

### Важные поля `GET /api/stats/summary`

- `runs.succeeded`, `runs.failed`, `runs.stopped`, `runs.unsuccessful`, `runs.total`
- `job_key`, `job_title` — выбранная задача, если фильтр был задан
- `transfer.traffic_bytes`
- `transfer.files`
- `transfer.average_speed_bytes_per_second`
- `retention.history_days`
- `retention.last_pruned_at`
- `system.database`
- `system.process`

### Важные поля `GET /api/system`

- `database.database_size_bytes` — размер основного файла `taskboard.db`
- `database.wal_size_bytes` — размер WAL-журнала, если он существует
- `database.shm_size_bytes` — размер shared memory файла SQLite, если он существует
- `database.total_size_bytes` — общий размер `taskboard.db`, `taskboard.db-wal` и `taskboard.db-shm`
- `database.journal_mode` — текущий режим журнала SQLite; штатное значение `wal`
- `database.freelist_count` и `database.reclaimable_bytes` — свободные страницы внутри БД и приблизительный объём, который может вернуть `VACUUM`
- `database.last_vacuum_at` — время последнего ручного сжатия базы через API/UI
- `process.open_fds`, `process.fd_soft_limit`, `process.fd_hard_limit` — открытые fd и лимиты процесса
- `process.rss_bytes` — текущий RSS backend-процесса
- `process.uptime_seconds` — время жизни текущего процесса

### Профилактика SQLite

`POST /api/system/database/checkpoint` выполняет `PRAGMA wal_checkpoint(TRUNCATE)`.
Операция полезна, если WAL-файл вырос, а нужно вернуть место на диске без полного сжатия базы.

`POST /api/system/database/vacuum` выполняет `VACUUM`, затем checkpoint WAL и записывает `database_last_vacuum_at`.
Операцию лучше запускать без активных копирований: SQLite перепаковывает файл базы, и на большой истории это может занять время.

---

## ⚙️ Настройки

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/gotify` | Получить настройки Gotify |
| `PUT` | `/api/gotify` | Сохранить настройки Gotify |
| `POST` | `/api/gotify/test` | Отправить тестовое уведомление |
| `GET` | `/api/queues` | Получить настройки очередей |
| `PUT` | `/api/queues` | Сохранить настройки очередей |
| `GET` | `/api/bandwidth` | Получить глобальный лимит скорости |
| `PUT` | `/api/bandwidth` | Сохранить глобальный лимит скорости |
| `GET` | `/api/logging` | Получить настройки `rclone`-логирования |
| `PUT` | `/api/logging` | Настроить ручное и автоматическое `rclone`-логирование |
| `PUT` | `/api/scheduler` | Включить или выключить scheduler runtime |
| `PUT` | `/api/antibot` | Включить или выключить anti-bot задержки запусков |
| `GET` | `/api/watcher` | Получить настройки и runtime-статус watcher |
| `PUT` | `/api/watcher` | Включить или отключить watcher и изменить debounce |

### Форматы настроек

- `PUT /api/gotify`: `enabled`, `url`, `token`, `default_priority`
- `PUT /api/queues`: `allow_parallel_profiles`, `allow_scheduler_queueing`, `allow_event_queueing`, `definitions`
- `PUT /api/bandwidth`: `limit`
- `PUT /api/logging`: `rclone_log_enabled`, `auto_rclone_log_enabled`, `auto_rclone_log_threshold`
- `PUT /api/scheduler`: `enabled`
- `PUT /api/antibot`: `enabled`
- `PUT /api/watcher`: `enabled`, `debounce_seconds`

---

## ☁️ Clouds

Cloud settings читаются из `rclone.conf`, но приложение сохраняет безопасные app-метаданные для remotes отдельно.

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/clouds` | Получить список облаков, считанных из `rclone.conf` |
| `GET` | `/api/clouds/browse` | Получить дочерние папки внутри выбранного cloud remote |
| `PUT` | `/api/clouds/{cloud_key}/lock` | Включить или выключить сериализацию запусков для конкретного Mail.ru remote |
| `PUT` | `/api/clouds` | Существует, но возвращает `403` |
| `POST` | `/api/clouds/import-rclone` | Существует, но возвращает `403` |
| `POST` | `/api/clouds/import-rclone-remote` | Существует, но возвращает `403` |
| `POST` | `/api/clouds/test` | Существует, но возвращает `403` |

---

## 📂 Файловый браузер

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/fs/browse` | Получить список корней или дочерних директорий |
| `GET` | `/api/clouds/browse` | Получить список дочерних папок в cloud remote |

### Query params

- `path` — абсолютный путь; если параметр не передан, возвращаются корневые директории из разрешённого списка
- `include_files` — если `true`, вместе с директориями возвращаются файлы; используется для выбора path-исключений
- `cloud_key` для `GET /api/clouds/browse` — ключ облака из `GET /api/clouds`
- `path` для `GET /api/clouds/browse` — путь внутри remote; пустое значение означает корень remote

---

## 📝 Логи

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/logging/rclone-tail` | Вернуть хвост последнего `rclone`-лога |
| `GET` | `/api/logging/rclone-files` | Список step-логов `rclone` с метаданными по запуску и файлу |
| `GET` | `/api/logging/rclone-files/{step_id}` | Вернуть tail конкретного step-лога `rclone` |
| `DELETE` | `/api/logging/rclone-log` | Очистить все `.log` файлы в каталоге `data/rclone-logs` |
| `DELETE` | `/api/logging/rclone-files/{step_id}` | Очистить конкретный step-лог `rclone` |

### Query params

- `lines` для `GET /api/logging/rclone-tail`: число строк от `1` до `2000`, по умолчанию `100`
- `limit` для `GET /api/logging/rclone-files`: от `1` до `1000`, по умолчанию `200`
- `job_key`, `status`, `trigger_type`, `run_id`, `only_with_log`, `only_errors` для `GET /api/logging/rclone-files` — серверные фильтры списка
- `GET /api/logging/rclone-files/{step_id}` возвращает содержимое выбранного step-лога целиком

---

## ▶️ Запуски и шаги

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/runs` | Список запусков |
| `DELETE` | `/api/runs` | Очистка истории запусков |
| `GET` | `/api/runs/{run_id}` | Детали запуска и всех его шагов |
| `POST` | `/api/runs` | Запуск профиля |
| `POST` | `/api/runs/job/{job_key}` | Запуск одной задачи |
| `POST` | `/api/run-steps/{step_id}/control` | Управление шагом: `pause`, `resume`, `stop` |

### Query params

- `limit` для `GET /api/runs`: от `1` до `999`, по умолчанию `50`

### Важные поля

- `failure_reason` в `GET /api/runs` и `GET /api/runs/{run_id}` — короткая причина ошибки по первому проблемному шагу, если запуск завершился с ошибкой

### Форматы запросов

- `POST /api/runs`: `profile`, `source`, `requested_by`, `metadata`
- `POST /api/run-steps/{step_id}/control`: `action`

---

## 📦 Каталог задач

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `PUT` | `/api/backups` | Обновить только backup-задачи |
| `PUT` | `/api/jobs` | Обновить полный каталог задач, включая backup и command jobs |

### Особенности

- `PUT /api/backups` работает только с задачами типа `backup`
- `PUT /api/jobs` принимает и `backup`, и `command`
- обе операции пересобирают профили на основе актуальных очередей
- если задача ссылается на несуществующую очередь, API возвращает `400`
- `backup.options` и `retention` поддерживают structured `rclone`-поля:
  `transfers`, `checkers`, `tpslimit`, `tpslimit_burst`, `retries`, `low_level_retries`,
  `retries_sleep`, `fast_list`, `no_traverse`, `debug_dump`, `mailru_safe_preset`, `exclude`, `extra_args`
- `backup.options.force_rclone_log` принудительно включает step-лог `rclone` для конкретной backup-задачи без включения глобального логирования
- `backup.options.exclude_paths` поддерживает path-исключения вида `{"path": "/abs/path", "kind": "file|directory"}`; путь должен быть внутри `source_path`
- `backup.archive` поддерживает 7z-архивацию перед отправкой в облако:
  `enabled`, `filename_template`, `date_format`, `compression_level`, `temp_dir`, `password`, `encrypt_headers`
- Для backup-задач с облаком используйте `cloud_key` и `destination_subpath`; backend сам собирает итоговый `destination_path`

---

## 🧩 Контракт для нового frontend

Новый frontend может не использовать старый HTML вообще и подключаться только к backend API:

- стартовая загрузка: `GET /api/state` + `GET /api/jobs`
- список задач и редактор: `GET /api/jobs`, сохранение через `PUT /api/jobs`
- запуск задачи: `POST /api/runs/job/{job_key}`
- запуск профиля: `POST /api/runs`
- активный прогресс: `copy_progress` из `GET /api/state`
- история запусков: `GET /api/runs`, детали через `GET /api/runs/{run_id}`
- логи: `GET /api/logging/rclone-files`, `GET /api/logging/rclone-files/{step_id}`
- настройки: `GET/PUT` endpoints из раздела «Настройки»
- browse локального пути: `GET /api/fs/browse`
- browse облака: `GET /api/clouds/browse`

При split-deployment штатный frontend слушает публичный порт и проксирует `/api/*` к backend.
Если frontend живёт на другом LXC, укажите в его `.env.frontend`:

```env
TASKBOARD_FRONTEND_API_PROXY_URL=http://<backend-host>:8081
```

На backend-LXC для такого режима нужно слушать доступный адрес (`TASKBOARD_BACKEND_HOST=0.0.0.0`) или открыть backend через внешний reverse-proxy.
Если браузер обращается к backend напрямую, настройте `TASKBOARD_CORS_ORIGINS`.

---

## 🔔 События watcher

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `POST` | `/api/triggers/event` | Принять событие watcher и запустить совпавшие backup-задачи |

### Формат запроса

- `event_type`
- `path`
- `details`
