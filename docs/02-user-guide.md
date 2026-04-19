# 📖 Руководство по работе

Этот документ содержит подробные разделы, которые раньше были в корневом `README.md`: архитектуру, структуру проекта, базовые сценарии, примеры и FAQ.

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
├── install.sh
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
| `install.sh` | Единый installer |
| `rclone-taskboard.service` | Unit-файл для запуска через systemd |

---

## 🚀 Usage

После запуска доступны:

- dashboard: `http://<host>:8080/`
- проверка сервиса: `GET /api/health`
- лёгкий снимок для Homepage: `GET /api/homepage`
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

Проект использует рабочий каталог задач и переменные окружения. Полное описание вынесено в [Configuration](06-taskboard-mvp.md).

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
- structured `rclone`-опции для backup/retention: `transfers`, `checkers`, `tpslimit`, `tpslimit_burst`, `retries`, `low_level_retries`, `retries_sleep`, `fast_list`, `no_traverse`, `debug_dump`, `extra_args`
- принудительное включение step-лога `rclone` для отдельной backup-задачи
- исключения через `exclude patterns` с масками `rclone`, например `*.tmp`, `vzdump-qemu-400*`, `**/cache/**`
- исключения по путям через `exclude_paths`: выбор одного или нескольких файлов/каталогов внутри исходного каталога
- `Mail.ru safe preset` для бережного режима `rclone`
- расписание
- таймауты
- retention-политику
- уведомления Gotify
- параметры очередей
- глобальный лимит скорости
- включение подробного `rclone`-лога
- автоматическое включение `rclone`-логов по порогу ошибок
- принудительный step-лог для отдельных backup- и command-задач, если command начинается с `rclone`
- глобальное включение watcher и debounce
- включение watcher у отдельных backup-задач
- фильтрацию watcher-событий через `exclude patterns` и `exclude_paths`
- сериализацию запусков для Mail.ru remote на вкладке `Облака`
- размер SQLite-базы на главной панели статуса
- диагностику и профилактику SQLite в разделе `Статистика`

### Статистика и состояние базы

Раздел `Статистика` показывает не только сводку запусков за выбранный период, но и техническое состояние runtime-хранилища:

- общий размер SQLite-файлов
- размер основного `taskboard.db`
- размер WAL-журнала
- объём, который примерно может вернуть `VACUUM`
- режим журнала SQLite
- открытые файловые дескрипторы backend-процесса
- память и uptime процесса

На главной панели остаётся только компактный виджет `База` с текущим размером.
Подробности и профилактические кнопки находятся внутри `Статистика`, чтобы не перегружать основной экран.
Когда раздел открыт, сводка запусков и диагностика базы обновляются автоматически вместе с общим polling dashboard. Кнопка `Обновить всё` нужна для ручного обновления без ожидания следующего тика.

Кнопка `Сбросить WAL` выполняет checkpoint SQLite и освобождает разросшийся WAL-журнал.
Кнопка `Сжать базу` запускает `VACUUM`; её лучше нажимать без активных копирований, потому что SQLite перепаковывает файл базы.

### Homepage/customapi

Для внешнего Homepage лучше использовать лёгкий endpoint `GET /api/homepage`, а не полный `/api/state`.
Он отдаёт только компактные числа для виджетов и кэширует снимок на стороне taskboard, поэтому частый polling не заставляет backend каждый раз собирать историю запусков и полный dashboard-state.

Пример:

```yaml
widget:
  type: customapi
  url: http://100.100.0.243:8080/api/homepage
  refreshInterval: 5000
  mappings:
    - field: total_copy_speed_megabits_per_second
      label: Speed
      format: float
      suffix: " Mbps"
    - field: open_runs_total
      label: Open Runs
      format: number
    - field: database_total_size_bytes
      label: DB
      format: bytes
```

Если Homepage опрашивает endpoint чаще, taskboard всё равно обновляет тяжёлую часть снимка не чаще `cache_seconds`.

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
| `TASKBOARD_COPY_STARTUP_DELAY_SECONDS` | Задержка перед первым стартом backup/sync после запуска backend |
| `TASKBOARD_COPY_MIN_START_INTERVAL_SECONDS` | Минимальный интервал между стартами backup/sync по всей системе |
| `TASKBOARD_DEFAULT_TIMEOUT_SECONDS` | Таймаут команд по умолчанию |
| `TASKBOARD_OUTPUT_TAIL_CHARS` | Размер сохраняемого tail вывода |
| `TASKBOARD_DRY_RUN` | Dry-run режим |
| `TASKBOARD_API_TOKEN` | Токен для операций записи |

---

## 📖 API

Полное описание API вынесено в [API reference](04-api-reference.md).

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

---

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

Подробности по установке вынесены в [Deployment](07-deployment.md).

### Что проверять после развертывания?

- `GET /api/health`
- `GET /api/homepage`
- `GET /api/state`
- `GET /api/system`
- создание SQLite database
- создание `default_jobs.json`
- успешный ручной запуск задачи или профиля
