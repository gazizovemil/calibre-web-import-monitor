# Calibre-Web Import Monitor v1.4.0

Build: `20261012.2`

Русскоязычная документация. English documentation: [README.md](README.md).

## Быстрый запуск

```bash
git clone https://github.com/gazizovemil/calibre-web-import-monitor.git
cd calibre-web-import-monitor
cp .env.example .env
```

Отредактируй `.env`, указав реальные пути к CWA config, Inbox и Calibre Library, затем:

```bash
docker compose up -d --build
```

## Что нового в v1.3

- настоящая статистика текущей партии импорта;
- сглаженная скорость за 5 / 30 / 60 минут и ETA;
- график скорости за последний час;
- определение возможного зависания импорта;
- последние книги с обложками из Calibre;
- статистика форматов библиотеки;
- отдельная вкладка ошибок и retry queue;
- мультиязычный интерфейс;
- встроены русский и английский языки с флагами;
- новые языки подключаются внешним JSON-файлом без пересборки Docker-образа;
- версия приложения и Build ID видны в интерфейсе и API;
- Telegram: уведомление о старте партии, периодический статус, ошибка, возможное зависание и итог импорта;
- кнопки «Отправить тест» и «Отправить текущий статус» на вкладке «Система».

## Обновление с v1.2

После `git clone` или обновления репозитория структура проекта должна выглядеть так:

```text
./
├── app.py
├── index.html
├── Dockerfile
├── docker-compose.yml
├── README_RU.md
├── data/
├── lang/
│   ├── ru.json
│   ├── en.json
│   └── README.md
└── secrets/
    └── README.txt
```

Пересобрать:

```bash
cd calibre-web-import-monitor

docker compose down
docker compose build --no-cache
docker compose up -d --force-recreate
```

Проверить:

```bash
docker compose logs
```

Нормальный старт:

```text
CWA Import Monitor v1.4.0 build 20261012.2 listening on http://0.0.0.0:8087
Inbox:   /inbox
Library: /library
Config:  /cwa-config
Lang:    /lang (+ builtin /app/lang)
Telegram: enabled=False configured=False language=ru
```

После обновления браузера желательно один раз сделать `Ctrl+F5`.

## Мультиязычность

Встроенные языки:

```text
🇷🇺 Русский
🇬🇧 English
```

Язык выбирается справа в верхней панели и сохраняется в `localStorage` браузера.

Внешние переводы лежат в:

```text
./lang
```

Чтобы добавить, например, немецкий язык, создать:

```text
lang/de.json
```

Пример структуры:

```json
{
  "meta": {
    "code": "de",
    "name": "Deutsch",
    "flag": "🇩🇪",
    "locale": "de-DE"
  },
  "strings": {
    "nav.overview": "Übersicht",
    "nav.queue": "Warteschlange"
  }
}
```

Файл может содержать только изменённые строки: отсутствующие ключи будут взяты из fallback-языка. После добавления нового JSON достаточно полностью обновить страницу браузера; Docker image пересобирать не нужно.

## Telegram

По умолчанию Telegram выключен:

```yaml
TELEGRAM_ENABLED: "false"
```

Секреты хранятся вне образа Docker.

Создать файлы:

```bash
cd calibre-web-import-monitor
mkdir -p secrets
nano secrets/telegram_bot_token
nano secrets/telegram_chat_id
chmod 600 secrets/telegram_bot_token secrets/telegram_chat_id
```

В `telegram_bot_token` должна быть только строка токена бота.

В `telegram_chat_id` — только Chat ID пользователя, группы или канала.

После этого поменять в `docker-compose.yml`:

```yaml
TELEGRAM_ENABLED: "true"
```

И пересоздать контейнер:

```bash
docker compose up -d --force-recreate
```

Проверить статус:

```bash
docker compose logs
```

Должно быть:

```text
Telegram: enabled=True configured=True language=ru
```

На вкладке **Система** можно нажать:

```text
Отправить тест
Отправить текущий статус
```

Также можно проверить из SSH. Для HTTP-действий нужен токен из `secrets/monitor_token`:

```bash
curl -s -X POST -H "X-Monitor-Token: YOUR_TOKEN" http://127.0.0.1:8087/api/telegram/test
```

## Какие Telegram-уведомления отправляются

При активном `TELEGRAM_ENABLED=true` монитор отправляет:

- начало новой партии;
- статус партии каждые 15 минут;
- новые failed-файлы;
- предупреждение, если нет прогресса 10 минут;
- итог после завершения партии.

Интервал статуса:

```yaml
TELEGRAM_PROGRESS_INTERVAL: "900"
```

Порог возможного зависания:

```yaml
STALL_THRESHOLD_SECONDS: "600"
```

Задержка перед признанием партии завершённой:

```yaml
BATCH_COMPLETE_GRACE_SECONDS: "20"
```

Язык Telegram не зависит от языка браузера:

```yaml
TELEGRAM_LANGUAGE: "ru"
```

Можно поставить:

```yaml
TELEGRAM_LANGUAGE: "en"
```

Или код собственного внешнего языкового файла.

## Безопасность

Монитор не получает `/var/run/docker.sock` и не может управлять CWA.

Эти каталоги подключены только для чтения:

```text
/cwa-config
/inbox
/library
/lang
/run/secrets
```

Запись разрешена только в:

```text
/data
```

Не добавляй обратно:

```yaml
cap_drop:
  - ALL
```

На TerraMaster это уже приводило к `PermissionError` при чтении `/library/metadata.db`.

Telegram token не хранится в `docker-compose.yml`, image или `state.json`.

Процесс приложения переключается на `PUID`:`PGID` (по умолчанию 1000:1000), если каталог `/data` можно отдать этому пользователю.

`/health` перестаёт быть успешным, если цикл мониторинга не завершался дольше `HEALTH_STALE_SECONDS`. Статистика библиотеки обновляется раз в `LIBRARY_SCAN_SECONDS` (30 секунд), каталог failed — раз в `FAILED_SCAN_SECONDS` (15 секунд). Inbox и status-файл читаются на каждом `POLL_SECONDS`.

Если создать `secrets/monitor_token`, статус и обложки откроются только после ввода токена. Кнопки Telegram требуют этот токен всегда: без него HTTP-действия выключены.

Для публикации монитора в интернет оставьте токен включённым или закройте сервис reverse-proxy и VPN.

## Полезные URL

```text
http://your-nas-or-server:8087/
http://your-nas-or-server:8087/api/status
http://your-nas-or-server:8087/api/version
http://your-nas-or-server:8087/api/languages
http://your-nas-or-server:8087/health
```

## Сброс старой статистики

Если после обновления хочется начать чистую историю:

```bash
cd calibre-web-import-monitor

docker compose down
rm -f data/state.json data/events.json
docker compose up -d
```

Это не затрагивает CWA, Inbox или Calibre Library.
