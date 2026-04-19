<p align="center">
   <img src="https://raw.githubusercontent.com/Kemper51rus/Rclone-taskboard/main/docs/readme-wordmark.svg" alt="Rclone taskboard" width="620">
</p>

RCLONE TASKBOARD — собирает запуск задач, расписание, очереди, историю и интерфейс управления в одном приложении. Сам перенос данных по-прежнему выполняет `rclone`, а приложение отвечает за координацию, хранение состояния и работу API.

---

## Quick Start

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Kemper51rus/Rclone-taskboard/main/install.sh)
```

---

## ✨ Возможности

- web dashboard для ручного управления задачами и наблюдения за состоянием сервиса
- HTTP API для запуска, просмотра истории и изменения настроек
- настраиваемые очереди с собственным числом workers и ограничениями скорости
- встроенный scheduler для периодических запусков
- встроенный watcher для запуска задач по изменениям в файловой системе
- SQLite для хранения запусков, шагов, событий и служебного состояния
- диагностика размера SQLite-базы, WAL, fd и памяти backend
- редактирование каталога задач через UI и API
- поддержка развертывания через `docker` и `systemd`
- автоматическое создание рабочего каталога задач при первом старте

---

## 📘 Документация

- [Обзор проекта](docs/01-overview.md)
- [Руководство по работе](docs/02-user-guide.md)
- [Runtime behavior](docs/03-runtime-behavior.md)
- [API reference](docs/04-api-reference.md)
- [Configuration](docs/06-taskboard-mvp.md)
- [Deployment](docs/07-deployment.md)
- [Development notes](docs/08-development-notes.md)
- [Legacy migration](docs/09-legacy-migration.md)
- [Runtime-каталог](docs/10-taskboard-runtime.md)
