# 📦 Deployment

Основной поддерживаемый вариант развертывания для LXC:

- `systemd`

---

## 🖥️ Systemd Deployment

В режиме `systemd` frontend и backend разделены:

- `rclone-taskboard.service` — backend/API runtime, по умолчанию слушает `127.0.0.1:8081`
- `rclone-taskboard-frontend.service` — статический frontend, по умолчанию слушает `0.0.0.0:8080` и проксирует `/api` в backend
- `rclone-taskboard-next-frontend.service` — экспериментальный новый frontend, по умолчанию слушает `0.0.0.0:8090` и проксирует `/api` в тот же backend

Так frontend можно обновлять, перезапускать или переносить в другой LXC без рестарта backend. Для внешнего frontend укажите `TASKBOARD_FRONTEND_API_PROXY_URL=http://<backend-host>:8081` в `.env.frontend`; на backend-LXC в этом случае задайте `TASKBOARD_BACKEND_HOST=0.0.0.0` или откройте API через свой reverse-proxy. Если браузер должен обращаться к backend напрямую, настройте `TASKBOARD_CORS_ORIGINS` в backend `.env`.
Экспериментальный frontend не заменяет текущий: для отката достаточно остановить `rclone-taskboard-next-frontend.service`, старый интерфейс на `8080` продолжит работать.

## Единый installer

Основной способ установки и обслуживания:

```bash
sudo ./install.sh
```

Скрипт работает как интерактивное меню и умеет:

- поставить или обновить deployment через `systemd`
- подтянуть исходники из Git перед установкой
- проверить зависимости и предложить доустановить недостающие
- выбрать начальный каталог задач: с примерами или пустой список задач без шаблона
- настроить отдельный `rclone-web.service` для Rclone Web GUI, если rclone ещё не настроен
- выполнить переход с legacy: сделать backup, остановить и удалить старые legacy-скрипты и unit'ы
- удалить taskboard-установку при повторном запуске

Installer не записывает `rclone.conf`.
Если на хосте уже есть `/root/.config/rclone/rclone.conf` с remotes или уже установлен `rclone-web.service`, installer не меняет и не перезапускает rclone.
Если rclone Web GUI unit создаётся впервые, он повторяет текущую локальную схему запуска: `rclone rcd --rc-web-gui --rc-addr :3000 --rc-no-auth`.
В итогах установки выводится LAN-адрес taskboard и, если unit есть, LAN-адрес Rclone Web GUI.

Legacy-cleanup покрывает старые файлы:

```text
/usr/local/bin/rclone-backup.sh
/usr/local/bin/rclone-watch.sh
/usr/local/bin/rclone-backup-status.sh
/usr/local/bin/rclone-backup.sh.bak.*
/etc/systemd/system/rclone-backup.service
/etc/systemd/system/rclone-backup.timer
/etc/systemd/system/rclone-watch.service
```

Для неинтерактивного запуска доступны команды:

```bash
sudo ./install.sh systemd
sudo ./install.sh migrate-legacy
sudo ./install.sh uninstall
```

### Требования

- `python3`
- `python3-venv`
- `rclone`
- `curl`
- `systemd`

### Подготовка

```bash
cp taskboard/.env.systemd.example taskboard/.env
cp taskboard/.env.frontend.example taskboard/.env.frontend
```

Проверьте:

- `TASKBOARD_DB_PATH`
- `TASKBOARD_JOBS_FILE`
- `TASKBOARD_RCLONE_CONFIG`
- `TASKBOARD_WATCHER_DEBOUNCE_SECONDS`
- `TASKBOARD_COPY_STARTUP_DELAY_SECONDS`
- `TASKBOARD_COPY_MIN_START_INTERVAL_SECONDS`

### Установка

```bash
sudo ./install.sh systemd
```

### Включение сервисов

```bash
systemctl status rclone-taskboard.service --no-pager
systemctl status rclone-taskboard-frontend.service --no-pager
```

Unit `rclone-taskboard.service` задаёт `LimitNOFILE=8192`.
Это запас для backend-процесса, scheduler, watcher и SQLite/WAL-файлов. Нормальная работа не должна приближаться к этому лимиту: текущие значения видны в разделе `Статистика` и в `GET /api/system`.

### Переход со старого external watcher

Если на хосте раньше был legacy pipeline или отдельный watcher-service, выполните migration через единый installer:

```bash
sudo ./install.sh migrate-legacy
```

Скрипт делает backup, останавливает и отключает старые unit'ы, удаляет устаревшие legacy-скрипты и unit'ы и оставляет только встроенный watcher внутри `rclone-taskboard.service`.

---

## ✅ Post-Deployment Checklist

Проверьте:

- `GET http://<host>:8080/frontend-health`
- `GET http://<host>:8080/api/health` через frontend proxy
- `GET http://<host>:8090/frontend-health` для экспериментального frontend
- `GET http://<host>:8090/api/health` через proxy экспериментального frontend
- `GET http://127.0.0.1:8081/api/health` напрямую в backend
- `GET /api/state`
- `GET /api/system`
- ручной запуск профиля или задачи
- создание SQLite database
- создание `default_jobs.json` при чистом старте

---

## 🆚 Выбор режима

| Режим | Когда подходит лучше |
| --- | --- |
| `systemd` | Нужна прямая интеграция с системой и запуск на хосте |
