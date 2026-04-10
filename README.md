# 🚀 Rclone Commander Web GUI

Веб-панель для управления backup-задачами на базе `rclone`.

`Rclone Commander Web GUI` собирает запуск задач, расписание, очереди, историю и интерфейс управления в одном приложении. Сам перенос данных по-прежнему выполняет `rclone`, а приложение отвечает за координацию, хранение состояния и работу API.

---

## 📚 Table of Contents

- [✨ Возможности](#-возможности)
- [🏗️ Архитектура](#️-архитектура)
- [📂 Структура проекта](#-структура-проекта)
- [📦 Installation](#-installation)
- [🚀 Usage](#-usage)
- [⚙️ Configuration](#️-configuration)
- [📖 API](#-api)
- [🧪 Examples](#-examples)
- [❓ FAQ](#-faq)
- [📘 Документация](#-документация)

---

## ✨ Возможности

- dashboard для ручного управления задачами и наблюдения за состоянием сервиса
- HTTP API для запуска, просмотра истории и изменения настроек
- настраиваемые очереди с собственным числом workers и ограничениями скорости
- встроенный scheduler для периодических запусков
- запуск по событиям файловой системы через `inotifywait`
- SQLite для хранения запусков, шагов, событий и служебного состояния
- редактирование каталога задач через UI и API
- поддержка развертывания через `docker` и `systemd`
- автоматическое создание рабочего каталога задач при первом старте

---

## 🏗️ Архитектура

Проект устроен так:

- приложение принимает команды из UI, scheduler и watcher
- `rclone` выполняет копирование и синхронизацию
- очереди распределяют запуск задач по профилям
- SQLite хранит историю и текущее состояние

### Основные компоненты

| Компонент | Назначение |
| --- | --- |
| FastAPI app | API, dashboard и операции управления |
| Scheduler | Плановые запуски по расписанию |
| Workers | Исполнение задач из очередей |
| Watcher | Отправка событий файловой системы в API |
| SQLite | История запусков, шагов, событий и состояние scheduler |

### Поток выполнения

1. Запуск создаётся из dashboard, API, scheduler или watcher.
2. Профиль попадает в нужную очередь.
3. Worker берёт актуальные задачи из рабочего каталога.
4. Каждый шаг выполняет `rclone` или shell-команду.
5. Результаты сохраняются в SQLite и сразу становятся доступны в UI и API.

---

## 📂 Структура проекта

```text
.
├── docs/
├── hybrid/
│   ├── backend/
│   │   ├── app/
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── .env.docker.example
│   ├── .env.systemd.example
│   └── docker-compose.yml
├── legacy/
├── scripts/
├── systemd/
└── README.md
```

### Ключевые пути

| Путь | Назначение |
| --- | --- |
| `hybrid/backend/app/` | Исходный код backend |
| `hybrid/backend/app/jobs/default_jobs.example.json` | Шаблон каталога задач |
| `hybrid/backend/app/jobs/default_jobs.json` | Рабочий каталог задач, создаётся при первом запуске |
| `hybrid/docker-compose.yml` | Docker-стек |
| `legacy/` | Отдельные материалы и скрипты для старого окружения |
| `systemd/` | Unit-файлы для запуска на хосте |
| `scripts/` | Скрипты установки |

---

## 📦 Installation

Поддерживаются два варианта развертывания.

### 🐳 Docker

Подходит, если нужно запускать приложение в контейнерах.

#### Требования

- Docker с поддержкой Compose
- доступ хоста к:
  - `/media`
  - `/srv`
  - `/root/.config/rclone`

#### Быстрый старт

```bash
cd hybrid
cp .env.docker.example .env.docker
docker compose --env-file .env.docker up -d --build
```

#### Что запускается

- `hybrid-web` — API, dashboard, scheduler и workers
- `hybrid-watch` — watcher, который отправляет события в API

### 🖥️ Systemd

Подходит, если нужен запуск напрямую на хосте без контейнеров.

#### Требования

- `python3`
- `python3-venv`
- `rclone`
- `curl`
- `inotifywait`
- `systemd`

#### Быстрый старт

```bash
cp hybrid/.env.systemd.example hybrid/.env
./scripts/install-hybrid-systemd.sh /opt/rclone-hybrid
systemctl enable --now rclone-hybrid-web.service
systemctl enable --now rclone-watch-hybrid.service
```

---

## 🚀 Usage

После запуска доступны:

- dashboard: `http://<host>:8080/`
- проверка сервиса: `GET /api/health`
- текущее состояние: `GET /api/state`

### Базовый сценарий работы

1. Поднять приложение через `docker` или `systemd`.
2. Открыть dashboard.
3. Проверить состояние очередей, scheduler и последних запусков.
4. Запустить профиль или отдельную задачу вручную.
5. Просмотреть историю запусков и результат шагов.

### Профили по умолчанию

| Профиль | Назначение |
| --- | --- |
| `standard` | Частые и короткие задачи |
| `heavy` | Долгие и ресурсоёмкие задачи |
| `all` | Сводный запуск всех очередей из UI |

Набор очередей можно менять в настройках. В шаблоне каталога по умолчанию уже есть `standard` и `heavy`.

---

## ⚙️ Configuration

Проект использует рабочий каталог задач и переменные окружения.

### Jobs Catalog

| Файл | Назначение |
| --- | --- |
| `default_jobs.example.json` | Шаблон каталога, хранится в Git |
| `default_jobs.json` | Рабочий каталог, создаётся при первом запуске |

### Что хранится в каталоге

- `profiles`
- `gotify`
- `queues`
- `bandwidth`
- `logging`
- `clouds`
- `jobs`

### Что можно настроить

- состав и порядок задач
- режим передачи: `copy` или `sync`
- расписание
- таймауты
- retention-политику
- уведомления Gotify
- параметры очередей
- глобальный лимит скорости
- включение подробного `rclone`-лога

### Поведение при первом запуске

Если `default_jobs.json` отсутствует, приложение автоматически создаёт его из шаблона:

```text
default_jobs.example.json -> default_jobs.json
```

### Основные env-переменные

| Переменная | Назначение |
| --- | --- |
| `HYBRID_APP_NAME` | Имя приложения |
| `APP_ROOT` | Корневой рабочий каталог |
| `HYBRID_DB_PATH` | Путь к SQLite |
| `HYBRID_JOBS_FILE` | Путь к рабочему каталогу задач |
| `HYBRID_RCLONE_CONFIG` | Путь к `rclone.conf` |
| `APP_TIMEZONE` | Таймзона приложения |
| `HYBRID_ENABLE_SCHEDULER` | Включение scheduler |
| `HYBRID_STANDARD_INTERVAL_MINUTES` | Интервал стандартных задач |
| `HYBRID_HEAVY_HOUR` | Час запуска heavy-задач |
| `HYBRID_EVENT_DEBOUNCE_SECONDS` | Окно debounce для watcher |
| `HYBRID_DEFAULT_TIMEOUT_SECONDS` | Таймаут команд по умолчанию |
| `HYBRID_OUTPUT_TAIL_CHARS` | Размер сохраняемого tail вывода |
| `HYBRID_DRY_RUN` | Dry-run режим |
| `HYBRID_API_TOKEN` | Токен для операций записи |
| `HYBRID_API_URL` | URL API для watcher |

---

## 📖 API

Полное описание вынесено в `docs/04-api-reference.md`. Ниже приведён краткий обзор текущего API без сокращений и устаревших endpoints.

### Dashboard и чтение состояния

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/` | HTML dashboard |
| `GET` | `/api/health` | Проверка доступности сервиса |
| `GET` | `/api/state` | Состояние сервиса, очередей, workers и последних запусков |
| `GET` | `/api/jobs` | Полный рабочий каталог |
| `GET` | `/api/runs` | Список запусков |
| `GET` | `/api/runs/{run_id}` | Детали запуска и шагов |

### Настройки и справочные данные

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/api/gotify` | Текущие настройки Gotify |
| `GET` | `/api/queues` | Текущие настройки очередей |
| `GET` | `/api/bandwidth` | Глобальный лимит скорости |
| `GET` | `/api/logging` | Настройки подробного логирования |
| `GET` | `/api/logging/rclone-tail` | Хвост последнего `rclone`-лога |
| `DELETE` | `/api/logging/rclone-log` | Очистка файлов `rclone`-логов |
| `GET` | `/api/clouds` | Список облаков из `rclone.conf` |
| `GET` | `/api/fs/browse` | Просмотр доступных директорий |

### Операции записи

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `PUT` | `/api/gotify` | Сохранение настроек Gotify |
| `POST` | `/api/gotify/test` | Тестовое сообщение Gotify |
| `PUT` | `/api/queues` | Сохранение настроек очередей |
| `PUT` | `/api/bandwidth` | Сохранение глобального лимита скорости |
| `PUT` | `/api/logging` | Включение или отключение `rclone`-логов |
| `PUT` | `/api/backups` | Обновление только backup-задач |
| `PUT` | `/api/jobs` | Обновление полного каталога задач |
| `POST` | `/api/runs` | Запуск профиля |
| `POST` | `/api/runs/job/{job_key}` | Запуск отдельной задачи |
| `POST` | `/api/run-steps/{step_id}/control` | Пауза, продолжение или остановка шага |
| `POST` | `/api/triggers/event` | Приём событий от watcher |
| `DELETE` | `/api/runs` | Очистка истории запусков |

### Read-only и отключённые cloud endpoints

Cloud settings теперь берутся напрямую из `rclone.conf`. Поэтому:

- `GET /api/clouds` возвращает актуальный список remotes
- `PUT /api/clouds` существует, но отвечает `403`
- `POST /api/clouds/import-rclone` существует, но отвечает `403`
- `POST /api/clouds/import-rclone-remote` существует, но отвечает `403`
- `POST /api/clouds/test` существует, но отвечает `403`

---

## 🧪 Examples

### Запуск профиля `standard`

```bash
curl -X POST http://127.0.0.1:8080/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"profile":"standard","source":"api","requested_by":"operator"}'
```

### Просмотр состояния сервиса

```bash
curl http://127.0.0.1:8080/api/state
```

### Получение списка запусков

```bash
curl 'http://127.0.0.1:8080/api/runs?limit=20'
```

### Отправка filesystem event вручную

```bash
curl -X POST http://127.0.0.1:8080/api/triggers/event \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"filesystem","path":"/media/photo/immich_library/upload","details":{"event":"close_write"}}'
```

### Установка через scripts

```bash
./scripts/install-hybrid-docker.sh /opt/rclone-hybrid
```

```bash
./scripts/install-hybrid-systemd.sh /opt/rclone-hybrid
```

## ❓ FAQ

### Пройдёт ли чистый запуск без `default_jobs.json`?

Да. При первом запуске приложение создаёт `default_jobs.json` из `default_jobs.example.json`.

### Где задаются параметры доступа к облакам?

Основной источник данных для облаков теперь `rclone.conf`. Интерфейс читает remotes оттуда и не редактирует их напрямую.

### Что выбрать: `docker` или `systemd`?

| Режим | Когда использовать |
| --- | --- |
| `docker` | Если удобнее контейнерный запуск |
| `systemd` | Если нужен запуск напрямую на хосте |

### Что проверять после развертывания?

- `GET /api/health`
- `GET /api/state`
- создание SQLite database
- создание `default_jobs.json`
- успешный ручной запуск задачи или профиля

---

## 📘 Документация

- `docs/01-overview.md` — обзор проекта
- `docs/03-runtime-behavior.md` — как создаются и исполняются запуски
- `docs/04-api-reference.md` — полное описание API
- `docs/06-hybrid-mvp.md` — структура каталога и настройки
- `docs/07-deployment.md` — развертывание
- `hybrid/README.md` — заметки по каталогу `hybrid/`
- `legacy/README.md` — отдельные материалы по старому окружению и миграции
- `Security.md` — локальные заметки по безопасности, файл игнорируется Git
