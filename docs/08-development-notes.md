# 🛠️ Development Notes

Служебная заметка для разработки. Не пользовательская документация.

---

## UI Tooltip Pattern

В dashboard используется общий tooltip-механизм для элементов интерфейса.

Использование:

```html
<div class="ui-tooltip has-tooltip" data-tooltip="Текст подсказки" tabindex="0">...</div>
```

Правила:

- `ui-tooltip` включает базовый tooltip-стиль
- `has-tooltip` включает hover/focus-поведение
- `data-tooltip` содержит текст подсказки
- `tabindex="0"` нужен, если tooltip должен открываться с клавиатуры

Где сейчас используется:

- status widgets в `taskboard/frontend/static/dashboard.html`

Примечание для разработки:

- при добавлении новых tooltip в dashboard нужно переиспользовать этот механизм, а не делать отдельный `title` или новый CSS-стиль

---

## Frontend / Backend Split

- Backend API живёт в `taskboard/backend/app` и не должен импортировать или читать frontend-файлы на старте.
- Статический UI живёт в `taskboard/frontend/static`.
- Лёгкий frontend-сервис `taskboard/frontend/taskboard_frontend/server.py` отдаёт статику и проксирует `/api/*` к backend.
- В systemd backend по умолчанию слушает `127.0.0.1:8081`, frontend — `0.0.0.0:8080`.
- Изменения HTML/CSS/JS должны требовать перезапуска только frontend-сервиса.
