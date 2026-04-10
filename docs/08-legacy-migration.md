# 🔄 Legacy Migration

Этот документ описывает переход со старой связки shell-скриптов и systemd на текущий hybrid runtime приложения.

---

## 🧱 Legacy Components

Legacy stack обычно включал:

- `rclone-backup.service`
- `rclone-backup.timer`
- `rclone-watch.service`
- `rclone-web.service`
- shell scripts в `/usr/local/bin`

---

## 🛠️ Migration Command

```bash
./scripts/migrate-legacy-to-hybrid.sh <systemd|docker> [target-root]
```

### Аргументы

| Аргумент | Назначение |
| --- | --- |
| `systemd` или `docker` | Целевой способ развертывания |
| `target-root` | Каталог установки, по умолчанию `/opt/rclone-hybrid` |

---

## 📦 Что делает скрипт

1. Создаёт snapshot legacy-окружения
2. Экспортирует unit definitions и status output
3. Копирует legacy runtime artifacts в backup directory
4. Останавливает и отключает legacy services
5. Устанавливает hybrid runtime в выбранном режиме

---

## 🗂️ Содержимое backup snapshot

В snapshot могут попасть:

- `systemctl cat` для legacy units
- `systemctl status` для legacy units
- `/usr/local/bin/rclone-backup.sh`
- `/usr/local/bin/rclone-backup-status.sh`
- `/usr/local/bin/rclone-watch.sh`
- `/etc/rclone-backup.gotify`
- `/var/lib/rclone-backup`
- `/var/log/rclone-backup.log`

---

## 🧪 Examples

### Миграция в Systemd

```bash
sudo ./scripts/migrate-legacy-to-hybrid.sh systemd /opt/rclone-hybrid
```

### Миграция в Docker

```bash
sudo ./scripts/migrate-legacy-to-hybrid.sh docker /opt/rclone-hybrid
```

---

## ✅ Validation Checklist

После миграции проверьте:

- новые сервисы или Compose stack действительно запущены
- `GET /api/health` возвращает успешный ответ
- `GET /api/state` показывает живые scheduler и workers
- `default_jobs.json` создан при необходимости
- ручной запуск job-а или профиля проходит успешно
