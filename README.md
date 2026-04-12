# 🚀 Rclone taskboard

<p align="center">
  <img src="https://raw.githubusercontent.com/Kemper51rus/Rclone-taskboard-web-gui/main/taskboard/backend/app/rclone-taskboard-logo.svg" alt="Rclone taskboard" width="260">
</p>

Веб-панель для управления backup-задачами на базе `rclone`.

`Rclone taskboard` собирает запуск задач, расписание, очереди, историю и интерфейс управления в одном приложении. Сам перенос данных по-прежнему выполняет `rclone`, а приложение отвечает за координацию, хранение состояния и работу API.

---

## Быстрая установка

Запуск напрямую из GitHub:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Kemper51rus/Rclone-taskboard-web-gui/main/install.sh)
```

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
- встроенный watcher для запуска задач по изменениям в файловой системе
- SQLite для хранения запусков, шагов, событий и служебного состояния
- редактирование каталога задач через UI и API
- поддержка развертывания через `docker` и `systemd`
- автоматическое создание рабочего каталога задач при первом старте

---

## 🏗️ Архитектура

Проект устроен так:

- приложение принимает команды из UI, scheduler и встроенного watcher
- `rclone` выполняет копирование и синхронизацию
- очереди распределяют запуск задач по профилям
- SQLite хранит историю и текущее состояние

### Основные компоненты

| Компонент | Назначение |
| --- | --- |
| FastAPI app | API, dashboard и операции управления |
| Scheduler | Плановые запуски по расписанию |
| Workers | Исполнение задач из очередей |
| Watcher | Наблюдение за каталогами и запуск задач по событиям |
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
├── taskboard/
│   ├── backend/
│   │   ├── app/
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── .env.docker.example
│   ├── .env.systemd.example
│   └── docker-compose.yml
├── rclone-taskboard.service
└── README.md
```

### Ключевые пути

| Путь | Назначение |
| --- | --- |
| `taskboard/backend/app/` | Исходный код backend |
| `taskboard/backend/app/jobs/default_jobs.example.json` | Шаблон каталога задач |
| `taskboard/backend/app/jobs/default_jobs.json` | Рабочий каталог задач, создаётся при первом запуске |
| `taskboard/docker-compose.yml` | Docker-стек |
| `rclone-taskboard.service` | Unit-файл для запуска через systemd |

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
cd taskboard
cp .env.docker.example .env.docker
docker compose --env-file .env.docker up -d --build
```

#### Что запускается

- `taskboard-web` — API, dashboard, scheduler, workers и встроенный watcher

### 🖥️ Systemd

Подходит, если нужен запуск напрямую на хосте без контейнеров.

#### Требования

- `python3`
- `python3-venv`
- `rclone`
- `curl`
- `systemd`

#### Быстрый старт

```bash
sudo ./install.sh systemd
```

#### Переход со старого external watcher

Если раньше использовался legacy pipeline или отдельный watcher-service, после обновления достаточно один раз выполнить:

```bash
sudo ./install.sh migrate-legacy
```

Скрипт сделает backup, остановит и отключит старые unit'ы, удалит устаревшие legacy-скрипты и unit'ы при наличии и оставит только встроенный watcher внутри backend.

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
- `watcher`
- `clouds`
- `jobs`

### Что можно настроить

- состав и порядок задач
- режим передачи: `copy` или `sync`
- structured `rclone`-опции для backup/retention:
  `transfers`, `checkers`, `tpslimit`, `tpslimit_burst`, `retries`, `low_level_retries`,
  `retries_sleep`, `fast_list`, `no_traverse`, `debug_dump`, `extra_args`
- `Mail.ru safe preset` для бережного режима `rclone`
- расписание
- таймауты
- retention-политику
- уведомления Gotify
- параметры очередей
- глобальный лимит скорости
- включение подробного `rclone`-лога
- автоматическое включение `rclone`-логов по порогу ошибок
- глобальное включение watcher и debounce
- включение watcher у отдельных backup-задач
- сериализацию запусков для Mail.ru remote на вкладке `Облака`

### Поведение при первом запуске

Если `default_jobs.json` отсутствует, приложение автоматически создаёт его из шаблона:

```text
default_jobs.example.json -> default_jobs.json
```

### Основные env-переменные

| Переменная | Назначение |
| --- | --- |
| `TASKBOARD_APP_NAME` | Имя приложения |
| `APP_ROOT` | Корневой рабочий каталог |
| `TASKBOARD_DB_PATH` | Путь к SQLite |
| `TASKBOARD_JOBS_FILE` | Путь к рабочему каталогу задач |
| `TASKBOARD_RCLONE_CONFIG` | Путь к `rclone.conf` |
| `APP_TIMEZONE` | Таймзона приложения |
| `TASKBOARD_ENABLE_SCHEDULER` | Включение scheduler |
| `TASKBOARD_STANDARD_INTERVAL_MINUTES` | Интервал стандартных задач |
| `TASKBOARD_HEAVY_HOUR` | Час запуска heavy-задач |
| `TASKBOARD_WATCHER_DEBOUNCE_SECONDS` | Начальное значение debounce для watcher |
| `TASKBOARD_DEFAULT_TIMEOUT_SECONDS` | Таймаут команд по умолчанию |
| `TASKBOARD_OUTPUT_TAIL_CHARS` | Размер сохраняемого tail вывода |
| `TASKBOARD_DRY_RUN` | Dry-run режим |
| `TASKBOARD_API_TOKEN` | Токен для операций записи |

---

## 📖 API

Полное описание API вынесено в `docs/04-api-reference.md`.

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

### Установка через installer

```bash
sudo ./install.sh
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
- `docs/06-taskboard-mvp.md` — структура каталога и настройки
- `docs/07-deployment.md` — развертывание
- `docs/09-legacy-migration.md` — материалы по старому окружению и миграции
- `docs/10-taskboard-runtime.md` — заметки по runtime-каталогу `taskboard/`
- `Security.md` — локальные заметки по безопасности, файл игнорируется Git
