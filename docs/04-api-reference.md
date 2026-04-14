# 📖 API Reference

Документ описывает текущее API приложения по состоянию исходного кода `taskboard/backend/app/main.py`.

Ниже приведён краткий обзор текущего API без сокращений и устаревших endpoints.

`TASKBOARD_API_TOKEN` используется как токен для операций записи во внешних интеграциях и deployment-сценариях.

---

## 🌐 Dashboard

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/` | Возвращает HTML dashboard |
| `GET` | `/favicon.svg` | Возвращает SVG-иконку приложения |

---

## 🩺 Health и состояние

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/health` | Проверка доступности сервиса |
| `GET` | `/api/state` | Общее состояние, очереди, workers, последние запуски и флаг `token_required` |
| `GET` | `/api/jobs` | Полный рабочий каталог: профили, очереди, Gotify, bandwidth, logging, watcher, clouds и jobs |
| `GET` | `/api/stats/summary` | Сводная статистика запусков и передачи за выбранный период |

### Важные поля `GET /api/state`

- `queue_statuses` — состояние очередей
- `copy_progress` — активные и ожидающие backup/copy шаги для progress UI
- `total_copy_speed_bytes_per_second` — суммарная скорость всех активных `copy/sync` задач в байтах в секунду
- `total_copy_speed_megabits_per_second` — та же суммарная скорость в Мбит/с
- `active_operations` — список открытых операций
- `latest_runs` — последние запуски
- `backup_jobs` — backup-задачи каталога
- `watcher` — runtime-статус встроенного наблюдателя

### Query params `GET /api/stats/summary`

- `period` — `day`, `week`, `month`, `year`

### Важные поля `GET /api/stats/summary`

- `runs.succeeded`, `runs.failed`, `runs.stopped`, `runs.unsuccessful`, `runs.total`
- `transfer.traffic_bytes`
- `transfer.files`
- `transfer.average_speed_bytes_per_second`
- `retention.history_days`
- `retention.last_pruned_at`

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
| `GET` | `/api/watcher` | Получить настройки и runtime-статус watcher |
| `PUT` | `/api/watcher` | Включить или отключить watcher и изменить debounce |

### Форматы настроек

- `PUT /api/gotify`: `enabled`, `url`, `token`, `default_priority`
- `PUT /api/queues`: `allow_parallel_profiles`, `allow_scheduler_queueing`, `allow_event_queueing`, `definitions`
- `PUT /api/bandwidth`: `limit`
- `PUT /api/logging`: `rclone_log_enabled`, `auto_rclone_log_enabled`, `auto_rclone_log_threshold`
- `PUT /api/watcher`: `enabled`, `debounce_seconds`

---

## ☁️ Clouds

Cloud settings читаются из `rclone.conf`, но приложение сохраняет безопасные app-метаданные для remotes отдельно.

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/clouds` | Получить список облаков, считанных из `rclone.conf` |
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

### Query params

- `path` — абсолютный путь; если параметр не передан, возвращаются корневые директории из разрешённого списка
- `include_files` — если `true`, вместе с директориями возвращаются файлы; используется для выбора path-исключений

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

---

## 🔔 События watcher

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `POST` | `/api/triggers/event` | Принять событие watcher и запустить совпавшие backup-задачи |

### Формат запроса

- `event_type`
- `path`
- `details`
