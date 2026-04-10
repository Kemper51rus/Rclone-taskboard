# 📖 API Reference

Документ описывает текущее API приложения по состоянию исходного кода `hybrid/backend/app/main.py`.

Если задан `HYBRID_API_TOKEN`, все операции записи требуют токен. Эндпоинты чтения работают без него.

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
| `GET` | `/api/jobs` | Полный рабочий каталог: профили, очереди, Gotify, bandwidth, logging, clouds и jobs |

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
| `PUT` | `/api/logging` | Включить или отключить `rclone`-логирование |

### Форматы настроек

- `PUT /api/gotify`: `enabled`, `url`, `token`, `default_priority`
- `PUT /api/queues`: `allow_parallel_profiles`, `allow_scheduler_queueing`, `allow_event_queueing`, `definitions`
- `PUT /api/bandwidth`: `limit`
- `PUT /api/logging`: `rclone_log_enabled`

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
| `DELETE` | `/api/logging/rclone-log` | Очистить все `.log` файлы в каталоге `data/rclone-logs` |

### Query params

- `lines` для `GET /api/logging/rclone-tail`: число строк от `1` до `2000`, по умолчанию `100`

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
| `POST` | `/api/triggers/event` | Принять событие от watcher и, если нужно, поставить профиль в очередь |

### Формат запроса

- `event_type`
- `path`
- `details`

---

## 📌 Что было не задокументировано раньше

В коде уже были доступны, но в основном описании проекта отсутствовали:

- `GET /`
- `GET /api/gotify`
- `GET /api/queues`
- `GET /api/bandwidth`
- `GET /api/logging`
- `GET /api/logging/rclone-tail`
- `DELETE /api/logging/rclone-log`
- `GET /api/clouds`
- `GET /api/fs/browse`
- `DELETE /api/runs`
- `POST /api/gotify/test`
- `POST /api/run-steps/{step_id}/control`
- `PUT /api/bandwidth`
- `PUT /api/logging`
- `PUT /api/jobs`

Cloud endpoints для записи также были задокументированы неточно: они существуют, но в текущей версии намеренно возвращают `403`.
