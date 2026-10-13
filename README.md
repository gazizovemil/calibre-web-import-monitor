# Calibre-Web Import Monitor

A lightweight, read-only monitoring dashboard for **Calibre-Web Automated (CWA)** import jobs.

**Current version:** `1.4.0`  
**Build:** `20261012.2`

## Features

- Live CWA ingest queue monitoring
- Import batch progress and ETA
- Smoothed processing speed for 5 / 30 / 60 minute windows
- Import speed history chart
- Stall detection when an active queue stops progressing
- Calibre `metadata.db` statistics
- Recently imported books with cover previews
- Format statistics
- Dedicated errors and retry view
- Multilingual UI
  - 🇬🇧 English
  - 🇷🇺 Russian
  - additional languages can be added through external JSON files
- Telegram notifications for:
  - batch start
  - periodic status
  - failed imports
  - suspected stalls
  - final batch result
- Build/version information in the UI and API
- Read-only access to CWA config, inbox, and Calibre library
- Optional access token for the status API and covers
- Health check that fails when monitoring goes stale

## Screens / sections

The UI contains dedicated views for:

- Overview
- Import queue
- Events
- Library
- Errors / retry queue
- System status

## Requirements

- Docker with Docker Compose
- An existing Calibre-Web Automated installation
- Access to the host directories mapped to:
  - CWA config
  - CWA ingest directory
  - Calibre library

## Quick start

Clone the repository:

```bash
git clone https://github.com/gazizovemil/calibre-web-import-monitor.git
cd calibre-web-import-monitor
```

Create local configuration:

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
CWA_CONFIG_PATH=/path/to/calibre-web/config
CWA_INBOX_PATH=/path/to/cwa-book-ingest
CWA_LIBRARY_PATH=/path/to/calibre-library
CWA_WEB_URL=http://your-nas-or-server:8084
```

Build and start:

```bash
docker compose up -d --build
```

Open:

```text
http://your-nas-or-server:8087
```

Check logs:

```bash
docker compose logs -f
```

## Example for TerraMaster / NAS installations

Example `.env` values:

```dotenv
TZ=Asia/Almaty
MONITOR_PORT=8087
CWA_WEB_URL=http://192.168.1.5:8084

CWA_CONFIG_PATH=/Volume1/docker/calibre-web/config
CWA_INBOX_PATH=/Volume1/Books/Inbox
CWA_LIBRARY_PATH=/Volume1/Books/Library
MONITOR_DATA_PATH=/Volume1/docker/calibre-web-import-monitor/data

DEFAULT_LANGUAGE=ru
```

> On some NAS platforms, dropping all Linux capabilities can prevent traversal of a read-only bind-mounted Calibre library. The included Compose file intentionally does **not** use `cap_drop: ALL`.

## Telegram notifications

Telegram is disabled by default.

Create two files:

```text
secrets/telegram_bot_token
secrets/telegram_chat_id
```

Each file must contain only its value, without quotes.

To require a token for status data and covers, also create:

```text
secrets/monitor_token
```

Telegram test and status buttons always require this token. Without it they stay disabled, even when the dashboard itself is open.

Then enable Telegram in `.env`:

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_LANGUAGE=en
TELEGRAM_PROGRESS_INTERVAL=900
TELEGRAM_NOTIFY_ERRORS=true
```

Recreate the container:

```bash
docker compose up -d --force-recreate
```

The **System** page contains buttons to send a test notification and the current import status.

## External languages

Built-in translations are located in `lang/`.

To add another language, create a JSON file such as `lang/de.json`:

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

Translation files may be partial. Missing keys fall back to the built-in fallback language.

Because `./lang` is mounted into the container, language files can be added or edited without rebuilding the image; refresh the browser afterwards.

## Security model

The monitor is intentionally designed to avoid modifying CWA or the Calibre library:

- CWA config is mounted read-only
- Inbox is mounted read-only
- Calibre library is mounted read-only
- Telegram secrets are excluded from Git
- runtime state is stored only in the monitor data directory
- Docker socket access is not required
- the application process drops to `PUID`:`PGID` (1000:1000 by default) when that user can own `/data`
- `/health` returns an error if the monitor loop has not completed within `HEALTH_STALE_SECONDS`

Library statistics are refreshed every `LIBRARY_SCAN_SECONDS` (30 by default), and the failed-file directory every `FAILED_SCAN_SECONDS` (15). Inbox and ingest status stay on `POLL_SECONDS`.

If `secrets/monitor_token` is present, sign in through the web UI or send the token with API requests:

```bash
curl -H "X-Monitor-Token: YOUR_TOKEN" http://127.0.0.1:8087/api/status
```

## Updating

```bash
git pull
docker compose up -d --build --force-recreate
```

## Russian documentation

See [README_RU.md](README_RU.md).

## License

MIT
