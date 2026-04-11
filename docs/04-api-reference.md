# 📖 API Reference

Документ описывает текущее API приложения по состоянию исходного кода `hybrid/backend/app/main.py`.

Ниже приведён краткий обзор текущего API без сокращений и устаревших endpoints.

`HYBRID_API_TOKEN` используется как токен для операций записи во внешних интеграциях и deployment-сценариях.

---

## 🌐 Dashboard

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/` | Возвращает HTML dashboard |

---

## 🩺 Health и состояние

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/health` | Проверка доступности сервиса |
| `GET` | `/api/state` | Общее состояние, очереди, workers, последние запуски и флаг `token_required` |
| `GET` | `/api/jobs` | Полный рабочий каталог: профили, очереди, Gotify, bandwidth, logging, watcher, clouds и jobs |

### Важные поля `GET /api/state`

- `queue_statuses` — состояние очередей
- `copy_progress` — активные и ожидающие backup/copy шаги для progress UI
- `total_copy_speed_bytes_per_second` — суммарная скорость всех активных `copy/sync` задач в байтах в секунду
- `active_operations` — список открытых операций
- `latest_runs` — последние запуски
- `backup_jobs` — backup-задачи каталога
- `watcher` — runtime-статус встроенного наблюдателя

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

Cloud settings в текущей версии считаются read-only и берутся из `rclone.conf`.

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/clouds` | Получить список облаков, считанных из `rclone.conf` |
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

- `limit` для `GET /api/runs`: от `1` до `500`, по умолчанию `50`

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

---

## 🔔 События watcher

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `POST` | `/api/triggers/event` | Принять событие watcher и запустить совпавшие backup-задачи |

### Формат запроса

- `event_type`
- `path`
- `details`
