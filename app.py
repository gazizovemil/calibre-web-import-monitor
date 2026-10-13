#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8087"))

APP_VERSION = os.getenv("APP_VERSION", "1.4.0")
BUILD_ID = os.getenv("BUILD_ID", "20261012.2")

CONFIG_DIR = Path(os.getenv("CWA_CONFIG_DIR", "/cwa-config"))
INBOX_DIR = Path(os.getenv("CWA_INBOX_DIR", "/inbox"))
LIBRARY_DIR = Path(os.getenv("CWA_LIBRARY_DIR", "/library"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
LANG_DIR = Path(os.getenv("LANG_DIR", "/lang"))


def bundled_path(name: str) -> Path:
    container = Path("/app") / name
    if container.exists():
        return container
    return Path(__file__).resolve().parent / name


BUILTIN_LANG_DIR = Path(os.getenv("BUILTIN_LANG_DIR", str(bundled_path("lang"))))
INDEX_FILE = Path(os.getenv("INDEX_FILE", str(bundled_path("index.html"))))
FAVICON_FILE = Path(os.getenv("FAVICON_FILE", str(bundled_path("favicon.svg"))))

CWA_WEB_URL = os.getenv("CWA_WEB_URL", "http://192.168.1.5:8084").rstrip("/")
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "ru")
TELEGRAM_LANGUAGE = os.getenv("TELEGRAM_LANGUAGE", DEFAULT_LANGUAGE)

POLL_SECONDS = max(1, int(os.getenv("POLL_SECONDS", "1")))
EVENT_LIMIT = max(50, int(os.getenv("EVENT_LIMIT", "500")))
STALL_THRESHOLD_SECONDS = max(60, int(os.getenv("STALL_THRESHOLD_SECONDS", "600")))
BATCH_COMPLETE_GRACE_SECONDS = max(5, int(os.getenv("BATCH_COMPLETE_GRACE_SECONDS", "20")))
LIBRARY_SCAN_SECONDS = max(5, int(os.getenv("LIBRARY_SCAN_SECONDS", "30")))
FAILED_SCAN_SECONDS = max(5, int(os.getenv("FAILED_SCAN_SECONDS", "15")))
CONFIG_LOOKUP_SECONDS = max(10, int(os.getenv("CONFIG_LOOKUP_SECONDS", "60")))
HEALTH_STALE_SECONDS = max(30, int(os.getenv("HEALTH_STALE_SECONDS", "120")))
PERSIST_SECONDS = max(1, int(os.getenv("PERSIST_SECONDS", "5")))

TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
TELEGRAM_PROGRESS_INTERVAL = max(60, int(os.getenv("TELEGRAM_PROGRESS_INTERVAL", "900")))
TELEGRAM_NOTIFY_ERRORS = os.getenv("TELEGRAM_NOTIFY_ERRORS", "true").strip().lower() in {"1", "true", "yes", "on"}
TELEGRAM_BOT_TOKEN_FILE = Path(os.getenv("TELEGRAM_BOT_TOKEN_FILE", "/run/secrets/telegram_bot_token"))
TELEGRAM_CHAT_ID_FILE = Path(os.getenv("TELEGRAM_CHAT_ID_FILE", "/run/secrets/telegram_chat_id"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MONITOR_TOKEN = os.getenv("MONITOR_TOKEN", "").strip()
MONITOR_TOKEN_FILE = Path(os.getenv("MONITOR_TOKEN_FILE", "/run/secrets/monitor_token"))
AUTH_COOKIE = "cwa_monitor_token"

BOOK_EXTENSIONS = {
    ".epub", ".kepub", ".lit", ".mobi", ".azw", ".azw3", ".fb2", ".fbz",
    ".azw4", ".prc", ".odt", ".lrf", ".pdb", ".cbz", ".pml", ".rb", ".cbr",
    ".cb7", ".cbc", ".chm", ".djvu", ".snb", ".tcr", ".pdf", ".docx", ".rtf",
    ".html", ".htmlz", ".txtz", ".txt", ".m4b", ".m4a", ".mp4",
}

STATE_FILE = DATA_DIR / "state.json"
EVENTS_FILE = DATA_DIR / "events.json"

lock = threading.RLock()
events: deque[dict] = deque(maxlen=EVENT_LIMIT)
completion_times: deque[float] = deque(maxlen=20000)


def new_batch_state() -> dict:
    return {
        "active": False,
        "id": 0,
        "started_at": None,
        "initial_count": 0,
        "added": 0,
        "handled": 0,
        "failed": 0,
        "library_start": None,
        "library_added": 0,
        "last_activity": None,
        "last_progress_notification": None,
        "stalled": False,
        "stall_notified": False,
        "completed_at": None,
    }


state = {
    "started_at": time.time(),
    "initialized": False,
    "processed_since_start": 0,
    "failed_since_start": 0,
    "baseline_books": None,
    "last_scan": 0.0,
    "last_status_fingerprint": "",
    "batch": new_batch_state(),
    "last_batch": {},
}

cache = {
    "inbox": {},
    "inbox_size": 0,
    "status": {},
    "retry": {},
    "library": {},
    "failed": {},
    "failed_keys": set(),
    "updated_at": 0.0,
    "library_scanned_at": 0.0,
    "failed_scanned_at": 0.0,
    "status_path": None,
    "status_path_checked_at": 0.0,
    "retry_path": None,
    "retry_path_checked_at": 0.0,
}

watcher_state = {
    "last_success": 0.0,
    "last_error": "",
}

SESSION_STATE_KEYS = {
    "started_at",
    "initialized",
    "processed_since_start",
    "failed_since_start",
    "baseline_books",
    "last_scan",
    "last_status_fingerprint",
}

telegram_runtime = {
    "last_sent_at": None,
    "last_error": "",
    "last_category": "",
}

previous_inbox: dict[str, tuple[int, int]] = {}
previous_failed: set[str] = set()
previous_book_count: int | None = None
previous_recent_ids: set[int] = set()


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"

    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")

    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0

    return f"{size:.1f} TiB"


def format_duration(seconds: int | float | None, language: str | None = None) -> str:
    if seconds is None:
        return "—"

    seconds = max(0, int(seconds))
    code = (language or TELEGRAM_LANGUAGE or "ru").lower()

    if code.startswith("en"):
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
        return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"

    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60} мин"
    return f"{seconds // 86400} д {(seconds % 86400) // 3600} ч"


def safe_write_json(path: Path, payload) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as exc:
        print(f"Failed to persist {path}: {type(exc).__name__}: {exc}", flush=True)


def normalize_state() -> None:
    batch_defaults = new_batch_state()

    if not isinstance(state.get("batch"), dict):
        state["batch"] = batch_defaults
    else:
        for key, value in batch_defaults.items():
            state["batch"].setdefault(key, value)

    if not isinstance(state.get("last_batch"), dict):
        state["last_batch"] = {}


def persist() -> None:
    with lock:
        safe_write_json(STATE_FILE, state)
        safe_write_json(EVENTS_FILE, list(events))


def load_persistent() -> None:
    global events

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            for key in state:
                if key in saved and key not in SESSION_STATE_KEYS:
                    state[key] = saved[key]
    except Exception:
        pass

    normalize_state()

    try:
        saved_events = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved_events, list):
            events = deque(saved_events[-EVENT_LIMIT:], maxlen=EVENT_LIMIT)
    except Exception:
        pass


def language_paths() -> dict[str, Path]:
    result: dict[str, Path] = {}

    for directory in (BUILTIN_LANG_DIR, LANG_DIR):
        try:
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                if re.fullmatch(r"[A-Za-z0-9_-]+\.json", path.name):
                    result[path.stem] = path
        except (PermissionError, OSError):
            continue

    return result


def load_language(code: str) -> dict:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", code or "")
    paths = language_paths()
    path = paths.get(clean)

    if path is None:
        path = paths.get(DEFAULT_LANGUAGE) or paths.get("ru") or paths.get("en")

    if path is None:
        return {
            "meta": {
                "code": "en",
                "name": "English",
                "flag": "🇬🇧",
                "locale": "en-US",
            },
            "strings": {},
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("translation file root must be an object")
        data.setdefault("meta", {})
        data.setdefault("strings", {})
        data["meta"].setdefault("code", path.stem)
        data["meta"].setdefault("name", path.stem)
        data["meta"].setdefault("flag", "🌐")
        data["meta"].setdefault("locale", "en-US")
        return data
    except Exception:
        return {
            "meta": {
                "code": path.stem,
                "name": path.stem,
                "flag": "🌐",
                "locale": "en-US",
            },
            "strings": {},
        }


def list_languages() -> list[dict]:
    items = []

    for code, path in sorted(language_paths().items()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            meta = data.get("meta", {}) if isinstance(data, dict) else {}
            items.append({
                "code": str(meta.get("code") or code),
                "name": str(meta.get("name") or code),
                "flag": str(meta.get("flag") or "🌐"),
                "locale": str(meta.get("locale") or "en-US"),
            })
        except Exception:
            items.append({
                "code": code,
                "name": code,
                "flag": "🌐",
                "locale": "en-US",
            })

    return items


def tr(key: str, params: dict | None = None, language: str | None = None) -> str:
    data = load_language(language or DEFAULT_LANGUAGE)
    strings = data.get("strings", {})
    value = strings.get(key, key)

    try:
        return str(value).format_map(SafeDict(params or {}))
    except Exception:
        return str(value)


def add_event(kind: str, key: str, params: dict | None = None, detail: str = "") -> None:
    params = params or {}

    item = {
        "time": time.time(),
        "iso": now_iso(),
        "kind": kind,
        "key": key,
        "params": params,
        "title": tr(key, params, DEFAULT_LANGUAGE),
        "detail": detail,
    }

    with lock:
        events.appendleft(item)

    persist()


def is_book_file(path: Path) -> bool:
    return (
        not path.name.startswith(".")
        and path.suffix.lower() in BOOK_EXTENSIONS
    )


def scan_tree(root: Path, only_books: bool = False) -> tuple[dict[str, tuple[int, int]], int]:
    result: dict[str, tuple[int, int]] = {}
    total_size = 0

    try:
        if not root.exists():
            return result, total_size
    except (PermissionError, OSError):
        return result, total_size

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name for name in dirnames
                if not name.startswith(".")
            ]

            for filename in filenames:
                if filename.startswith("."):
                    continue

                path = Path(dirpath) / filename

                if only_books and not is_book_file(path):
                    continue

                try:
                    st = path.stat()
                except (FileNotFoundError, PermissionError, OSError):
                    continue

                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    rel = str(path)

                result[rel] = (
                    int(st.st_size),
                    int(st.st_mtime),
                )
                total_size += int(st.st_size)

    except (PermissionError, OSError):
        pass

    return result, total_size


def find_config_file(names: list[str], contains: tuple[str, ...]) -> Path | None:
    for name in names:
        path = CONFIG_DIR / name
        try:
            if path.is_file():
                return path
        except (PermissionError, OSError):
            pass

    try:
        for path in CONFIG_DIR.rglob("*"):
            try:
                lowered = path.name.lower()
                if (
                    path.is_file()
                    and all(part in lowered for part in contains)
                    and path.stat().st_size < 5 * 1024 * 1024
                ):
                    return path
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass

    return None


def parse_status(text: str, path: Path | None) -> dict:
    raw = text.strip()

    info = {
        "available": bool(path),
        "path": str(path) if path else "",
        "raw": raw[-4000:] if raw else "",
        "state": "",
        "file": "",
        "detail": "",
        "updated_at": None,
    }

    if path:
        try:
            info["updated_at"] = path.stat().st_mtime
        except (PermissionError, OSError):
            pass

    if not raw:
        return info

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            info["state"] = str(
                parsed.get("state")
                or parsed.get("status")
                or parsed.get("phase")
                or ""
            )
            info["file"] = str(
                parsed.get("file")
                or parsed.get("filename")
                or parsed.get("path")
                or ""
            )
            info["detail"] = str(
                parsed.get("detail")
                or parsed.get("message")
                or parsed.get("error")
                or ""
            )
            return info
    except Exception:
        pass

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return info

    parsed_line = split_ingest_status(lines[-1])
    if parsed_line is None:
        info["detail"] = lines[-1]
        return info

    info["state"], info["file"], info["detail"] = parsed_line
    return info


def split_ingest_status(line: str) -> tuple[str, str, str] | None:
    known = {
        "idle", "processing", "queued", "completed", "complete", "success",
        "error", "failed", "timeout", "safety_timeout", "waiting", "converting",
        "importing", "metadata", "fetching",
    }
    colon = line.find(":")
    if colon <= 0:
        return None

    state = line[:colon].strip().lower()
    if state not in known:
        return None

    rest = line[colon + 1:]
    # A Windows drive letter is itself a colon. Keep it inside the file field:
    # processing:C:\books\a.epub:ignored:detail
    if re.match(r"^[A-Za-z]:[\\/]", rest):
        parts = rest[2:].split(":", 2)
        filename = rest[:2] + parts[0].strip()
    else:
        parts = rest.split(":", 2)
        filename = parts[0].strip()

    detail = parts[2].strip() if len(parts) > 2 else ""
    return state, filename, detail


def cached_config_file(cache_key: str, names: list[str], contains: tuple[str, ...], now: float) -> Path | None:
    path_key = f"{cache_key}_path"
    checked_key = f"{cache_key}_path_checked_at"

    with lock:
        checked_at = float(cache.get(checked_key) or 0)
        if checked_at and now - checked_at < CONFIG_LOOKUP_SECONDS:
            cached = cache.get(path_key)
            return cached if isinstance(cached, Path) else None

    found = find_config_file(names, contains)
    with lock:
        cache[path_key] = found
        cache[checked_key] = now
    return found


def read_status(now: float | None = None) -> dict:
    path = cached_config_file(
        "status",
        [
            "cwa_ingest_status",
            "cwa_ingest_status.txt",
            "ingest_status",
            "ingest_status.txt",
        ],
        ("ingest", "status"),
        time.time() if now is None else now,
    )

    if not path:
        return parse_status("", None)

    try:
        return parse_status(
            path.read_text(encoding="utf-8", errors="replace"),
            path,
        )
    except Exception as exc:
        with lock:
            cache["status_path_checked_at"] = 0.0
        return {
            "available": True,
            "path": str(path),
            "raw": "",
            "state": "error",
            "file": "",
            "detail": f"status-file: {exc}",
            "updated_at": None,
        }


def read_retry(now: float | None = None) -> dict:
    path = cached_config_file(
        "retry",
        [
            "cwa_ingest_retry_queue",
            "cwa_ingest_retry_queue.txt",
            "ingest_retry_queue",
            "ingest_retry_queue.txt",
        ],
        ("ingest", "retry"),
        time.time() if now is None else now,
    )

    if not path:
        return {
            "available": False,
            "path": "",
            "count": 0,
            "items": [],
        }

    try:
        lines = [
            line.strip()
            for line in path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
            if line.strip()
        ]

        return {
            "available": True,
            "path": str(path),
            "count": len(lines),
            "items": lines[:50],
        }
    except Exception as exc:
        return {
            "available": True,
            "path": str(path),
            "count": 0,
            "items": [],
            "error": str(exc),
        }


def sqlite_readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def read_library() -> dict:
    db = LIBRARY_DIR / "metadata.db"

    result = {
        "available": False,
        "books": None,
        "authors": None,
        "formats": None,
        "recent": [],
        "format_stats": [],
        "db_path": str(db),
        "error": "",
    }

    try:
        if not db.is_file():
            result["error"] = "metadata.db not found"
            return result
    except PermissionError as exc:
        result["error"] = f"permission denied: {exc}"
        return result
    except OSError as exc:
        result["error"] = f"filesystem error: {exc}"
        return result

    con = None
    try:
        con = sqlite3.connect(
            sqlite_readonly_uri(db),
            uri=True,
            timeout=2,
            isolation_level=None,
        )
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=1500")

        result["books"] = int(
            con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        )
        result["authors"] = int(
            con.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        )

        try:
            result["formats"] = int(
                con.execute("SELECT COUNT(*) FROM data").fetchone()[0]
            )
        except sqlite3.Error:
            result["formats"] = None

        rows = con.execute(
            "SELECT b.id, b.title, b.path, b.has_cover, "
            "COALESCE(GROUP_CONCAT(a.name, ' & '), '') "
            "FROM books b "
            "LEFT JOIN books_authors_link bal ON bal.book=b.id "
            "LEFT JOIN authors a ON a.id=bal.author "
            "GROUP BY b.id "
            "ORDER BY b.id DESC LIMIT 30"
        ).fetchall()

        result["recent"] = [
            {
                "id": int(row[0]),
                "title": str(row[1] or ""),
                "path": str(row[2] or ""),
                "has_cover": bool(row[3]),
                "authors": str(row[4] or "—"),
            }
            for row in rows
        ]

        try:
            format_rows = con.execute(
                "SELECT UPPER(format), COUNT(*) "
                "FROM data GROUP BY UPPER(format) "
                "ORDER BY COUNT(*) DESC, UPPER(format)"
            ).fetchall()
            result["format_stats"] = [
                {"format": str(row[0] or "?"), "count": int(row[1])}
                for row in format_rows
            ]
        except sqlite3.Error:
            result["format_stats"] = []

        result["available"] = True

    except sqlite3.Error as exc:
        result["error"] = f"SQLite: {exc}"
    except PermissionError as exc:
        result["error"] = f"permission denied: {exc}"
    except OSError as exc:
        result["error"] = f"filesystem error: {exc}"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if con is not None:
            con.close()

    return result


def scan_failed() -> dict:
    for failed_dir in (
        CONFIG_DIR / "processed_books" / "failed",
        CONFIG_DIR / "failed_books",
    ):
        try:
            if failed_dir.exists():
                files, total_size = scan_tree(failed_dir, only_books=False)
                latest = [
                    {
                        "path": rel,
                        "name": Path(rel).name,
                        "size": size,
                        "size_h": human_bytes(size),
                        "mtime": mtime,
                    }
                    for rel, (size, mtime) in sorted(
                        files.items(),
                        key=lambda item: item[1][1],
                        reverse=True,
                    )[:100]
                ]
                return {
                    "path": str(failed_dir),
                    "count": len(files),
                    "size": total_size,
                    "size_h": human_bytes(total_size),
                    "items": latest,
                    "keys": set(files),
                }
        except (PermissionError, OSError):
            continue

    return {
        "path": "",
        "count": 0,
        "size": 0,
        "size_h": "0 B",
        "items": [],
        "keys": set(),
    }


def read_secret(env_value: str, path: Path) -> str:
    if env_value:
        return env_value.strip()

    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    return ""


def telegram_config() -> dict:
    token = read_secret(TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_TOKEN_FILE)
    chat_id = read_secret(TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_FILE)

    return {
        "enabled": TELEGRAM_ENABLED,
        "configured": bool(token and chat_id),
        "token": token,
        "chat_id": chat_id,
        "language": TELEGRAM_LANGUAGE,
        "progress_interval": TELEGRAM_PROGRESS_INTERVAL,
        "notify_errors": TELEGRAM_NOTIFY_ERRORS,
    }


def monitor_token() -> str:
    return read_secret(MONITOR_TOKEN, MONITOR_TOKEN_FILE)


def token_matches(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    provided_bytes = provided.encode("utf-8")
    expected_bytes = expected.encode("utf-8")
    if len(provided_bytes) != len(expected_bytes):
        return False
    return secrets.compare_digest(provided_bytes, expected_bytes)


def provided_token(headers) -> str:
    direct = (headers.get("X-Monitor-Token") or "").strip()
    if direct:
        return direct

    authorization = (headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    prefix = AUTH_COOKIE + "="
    for part in (headers.get("Cookie") or "").split(";"):
        item = part.strip()
        if item.startswith(prefix):
            return unquote(item[len(prefix):]).strip()
    return ""


def authorization_status(headers) -> str:
    expected = monitor_token()
    if not expected:
        return "missing_config"
    if token_matches(provided_token(headers), expected):
        return "ok"
    return "unauthorized"


def telegram_public_status() -> dict:
    cfg = telegram_config()

    return {
        "enabled": cfg["enabled"],
        "configured": cfg["configured"],
        "language": cfg["language"],
        "progress_interval": cfg["progress_interval"],
        "notify_errors": cfg["notify_errors"],
        "last_sent_at": telegram_runtime["last_sent_at"],
        "last_error": telegram_runtime["last_error"],
        "last_category": telegram_runtime["last_category"],
    }


def send_telegram(text: str, category: str = "status") -> tuple[bool, str]:
    cfg = telegram_config()

    if not cfg["enabled"]:
        return False, "disabled"

    if not cfg["configured"]:
        telegram_runtime["last_error"] = "Telegram token/chat_id is not configured"
        return False, telegram_runtime["last_error"]

    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    payload = json.dumps({
        "chat_id": cfg["chat_id"],
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}: {body[:200]!r}")

        telegram_runtime["last_sent_at"] = time.time()
        telegram_runtime["last_error"] = ""
        telegram_runtime["last_category"] = category
        return True, "ok"

    except Exception as exc:
        telegram_runtime["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"Telegram notification failed: {telegram_runtime['last_error']}", flush=True)
        return False, telegram_runtime["last_error"]


def reset_runtime_state() -> None:
    """Drop process-local counters and close a batch that cannot be observed anymore."""
    batch = state.get("batch") if isinstance(state.get("batch"), dict) else {}
    interrupted_id = None

    if batch.get("active"):
        interrupted = dict(batch)
        interrupted["active"] = False
        interrupted["interrupted"] = True
        interrupted["completed_at"] = time.time()
        state["last_batch"] = interrupted
        interrupted_id = interrupted.get("id") or 0

    state["batch"] = new_batch_state()
    state["started_at"] = time.time()
    state["initialized"] = False
    state["processed_since_start"] = 0
    state["failed_since_start"] = 0
    state["baseline_books"] = None
    state["last_scan"] = 0.0
    state["last_status_fingerprint"] = ""

    if interrupted_id is None:
        return

    add_event("info", "event.batch_interrupted", {"id": interrupted_id})
    cfg = telegram_config()
    if cfg["enabled"] and cfg["configured"]:
        send_telegram(
            tr("telegram.batch_interrupted", {"id": interrupted_id}, cfg["language"]),
            "batch_interrupted",
        )


def health_payload() -> tuple[dict, HTTPStatus]:
    last_success = float(watcher_state.get("last_success") or 0)
    age = None if last_success <= 0 else max(0, int(time.time() - last_success))
    fresh = age is not None and age <= HEALTH_STALE_SECONDS
    payload = {
        "ok": fresh,
        "version": APP_VERSION,
        "build": BUILD_ID,
        "last_scan_age_seconds": age,
        "stale_after_seconds": HEALTH_STALE_SECONDS,
        "watcher_error": watcher_state.get("last_error") or "",
        "auth_required": authorization_status({}) != "missing_config",
    }
    status = HTTPStatus.OK if fresh else HTTPStatus.SERVICE_UNAVAILABLE
    return payload, status


def calculate_rates() -> dict:
    now = time.time()
    windows = {
        "5m": 300,
        "30m": 1800,
        "60m": 3600,
    }
    result = {}

    with lock:
        while completion_times and completion_times[0] < now - 3600:
            completion_times.popleft()

        values = list(completion_times)

    for key, seconds in windows.items():
        relevant = [value for value in values if value >= now - seconds]

        if not relevant:
            result[key] = 0.0
            continue

        # During a fresh batch, do not divide the first few completions by the
        # entire 5/30/60-minute window. Use at least one minute of observation,
        # then naturally converge to the full configured window.
        observed = min(seconds, max(60.0, now - min(relevant) + 1.0))
        result[key] = len(relevant) * 3600.0 / observed

    return result


def speed_history() -> list[dict]:
    now = int(time.time())
    current_minute = now - (now % 60)

    with lock:
        counter = Counter(
            int(ts) - (int(ts) % 60)
            for ts in completion_times
            if ts >= current_minute - 59 * 60
        )

    return [
        {
            "ts": current_minute - offset * 60,
            "count": counter.get(current_minute - offset * 60, 0),
        }
        for offset in range(59, -1, -1)
    ]


def eta_seconds(queue_count: int, rates: dict) -> int | None:
    preferred = rates.get("30m") or rates.get("60m") or rates.get("5m") or 0.0

    if queue_count <= 0:
        return 0

    if preferred <= 0.01:
        return None

    return int(queue_count / preferred * 3600)


def start_batch(queue_count: int, inbox_size: int, library_books: int | None, now: float) -> None:
    batch = state["batch"]
    batch_id = int(batch.get("id") or 0) + 1

    state["batch"] = {
        "active": True,
        "id": batch_id,
        "started_at": now,
        "initial_count": queue_count,
        "added": 0,
        "handled": 0,
        "failed": 0,
        "library_start": library_books,
        "library_added": 0,
        "last_activity": now,
        "last_progress_notification": now,
        "stalled": False,
        "stall_notified": False,
        "completed_at": None,
    }

    add_event(
        "queued",
        "event.batch_started",
        {"count": queue_count, "size": human_bytes(inbox_size)},
    )

    cfg = telegram_config()
    if cfg["enabled"] and cfg["configured"]:
        send_telegram(
            tr(
                "telegram.batch_started",
                {
                    "count": queue_count,
                    "size": human_bytes(inbox_size),
                    "library": library_books if library_books is not None else "—",
                },
                cfg["language"],
            ),
            "batch_started",
        )


def batch_snapshot(queue_count: int, library_books: int | None) -> dict:
    batch = dict(state["batch"])
    active = bool(batch.get("active"))

    if not active:
        result = {
            **batch,
            "total": 0,
            "remaining": queue_count,
            "progress": 100.0 if queue_count == 0 else 0.0,
            "duration_seconds": 0,
            "eta_seconds": None,
            "rates": calculate_rates(),
            "stalled_seconds": 0,
        }
        return result

    total = max(
        int(batch.get("initial_count") or 0) + int(batch.get("added") or 0),
        int(batch.get("handled") or 0) + queue_count,
    )
    handled = int(batch.get("handled") or 0)
    progress = 100.0 if total <= 0 else min(100.0, handled / total * 100.0)
    started_at = float(batch.get("started_at") or time.time())
    duration = max(0, int(time.time() - started_at))
    rates = calculate_rates()

    library_start = batch.get("library_start")
    library_added = int(batch.get("library_added") or 0)
    if library_books is not None and library_start is not None:
        library_added = max(library_added, library_books - int(library_start))

    last_activity = float(batch.get("last_activity") or started_at)
    stalled_seconds = max(0, int(time.time() - last_activity))

    return {
        **batch,
        "total": total,
        "remaining": queue_count,
        "handled": handled,
        "library_added": library_added,
        "progress": round(progress, 1),
        "duration_seconds": duration,
        "eta_seconds": eta_seconds(queue_count, rates),
        "rates": rates,
        "stalled_seconds": stalled_seconds,
    }


def telegram_status_message(queue_count: int, library_books: int | None) -> str:
    cfg = telegram_config()
    batch = batch_snapshot(queue_count, library_books)
    rate = batch["rates"].get("30m") or batch["rates"].get("60m") or batch["rates"].get("5m") or 0.0

    return tr(
        "telegram.progress",
        {
            "handled": batch.get("handled", 0),
            "total": batch.get("total", 0),
            "progress": f"{batch.get('progress', 0):.1f}",
            "remaining": queue_count,
            "rate": f"{rate:.1f}",
            "eta": format_duration(batch.get("eta_seconds"), cfg["language"]),
            "failed": batch.get("failed", 0),
            "library": library_books if library_books is not None else "—",
        },
        cfg["language"],
    )


def complete_batch(library_books: int | None, now: float) -> None:
    batch = state["batch"]
    if not batch.get("active"):
        return

    snapshot = batch_snapshot(0, library_books)
    snapshot["completed_at"] = now
    snapshot["active"] = False

    state["last_batch"] = snapshot
    state["batch"]["active"] = False
    state["batch"]["completed_at"] = now

    add_event(
        "success",
        "event.batch_completed",
        {
            "handled": snapshot.get("handled", 0),
            "failed": snapshot.get("failed", 0),
            "duration": format_duration(snapshot.get("duration_seconds"), DEFAULT_LANGUAGE),
        },
    )

    cfg = telegram_config()
    if cfg["enabled"] and cfg["configured"]:
        average_rate = 0.0
        duration = max(1, int(snapshot.get("duration_seconds") or 0))
        handled = int(snapshot.get("handled") or 0)
        average_rate = handled * 3600.0 / duration if handled else 0.0

        send_telegram(
            tr(
                "telegram.completed",
                {
                    "handled": handled,
                    "imported": snapshot.get("library_added", 0),
                    "failed": snapshot.get("failed", 0),
                    "duration": format_duration(duration, cfg["language"]),
                    "rate": f"{average_rate:.1f}",
                    "library": library_books if library_books is not None else "—",
                },
                cfg["language"],
            ),
            "batch_completed",
        )


def maybe_send_progress(queue_count: int, library_books: int | None, now: float) -> None:
    batch = state["batch"]
    cfg = telegram_config()

    if not batch.get("active") or not cfg["enabled"] or not cfg["configured"]:
        return

    last = float(batch.get("last_progress_notification") or batch.get("started_at") or now)

    if now - last >= cfg["progress_interval"]:
        ok, _ = send_telegram(
            telegram_status_message(queue_count, library_books),
            "progress",
        )
        if ok:
            batch["last_progress_notification"] = now


def maybe_detect_stall(queue_count: int, library_books: int | None, now: float) -> None:
    batch = state["batch"]

    if not batch.get("active"):
        return

    last_activity = float(batch.get("last_activity") or batch.get("started_at") or now)
    stalled = queue_count > 0 and now - last_activity >= STALL_THRESHOLD_SECONDS

    if stalled:
        batch["stalled"] = True

        if not batch.get("stall_notified"):
            batch["stall_notified"] = True
            add_event(
                "error",
                "event.import_stalled",
                {"duration": format_duration(now - last_activity, DEFAULT_LANGUAGE)},
            )

            cfg = telegram_config()
            if cfg["enabled"] and cfg["configured"]:
                snapshot = batch_snapshot(queue_count, library_books)
                send_telegram(
                    tr(
                        "telegram.stalled",
                        {
                            "duration": format_duration(now - last_activity, cfg["language"]),
                            "remaining": queue_count,
                            "handled": snapshot.get("handled", 0),
                            "total": snapshot.get("total", 0),
                        },
                        cfg["language"],
                    ),
                    "stalled",
                )
    else:
        if batch.get("stalled"):
            add_event("info", "event.import_resumed", {})
        batch["stalled"] = False
        batch["stall_notified"] = False


def cover_path_for_book(book_id: int) -> Path | None:
    db = LIBRARY_DIR / "metadata.db"
    con = None

    try:
        con = sqlite3.connect(
            sqlite_readonly_uri(db),
            uri=True,
            timeout=2,
        )
        row = con.execute(
            "SELECT path, has_cover FROM books WHERE id=?",
            (book_id,),
        ).fetchone()
    except Exception:
        return None
    finally:
        if con is not None:
            con.close()

    if not row or not row[1] or not row[0]:
        return None

    candidate = LIBRARY_DIR / str(row[0]) / "cover.jpg"

    try:
        library_real = os.path.realpath(LIBRARY_DIR)
        candidate_real = os.path.realpath(candidate)
        if not candidate_real.startswith(library_real.rstrip(os.sep) + os.sep):
            return None
        if not os.path.isfile(candidate_real):
            return None
        return Path(candidate_real)
    except Exception:
        return None


def watcher() -> None:
    global previous_inbox
    global previous_failed
    global previous_book_count
    global previous_recent_ids

    runtime_initialized = False
    last_persist = 0.0

    while True:
        started = time.time()

        try:
            now = time.time()
            with lock:
                library_due = now - float(cache.get("library_scanned_at") or 0) >= LIBRARY_SCAN_SECONDS
                failed_due = now - float(cache.get("failed_scanned_at") or 0) >= FAILED_SCAN_SECONDS
                cached_library = dict(cache.get("library") or {})
                cached_failed = dict(cache.get("failed") or {})
                cached_failed["keys"] = set(cache.get("failed_keys") or ())

            current, current_size = scan_tree(INBOX_DIR, only_books=True)
            status = read_status(now)
            retry = read_retry(now)
            library = read_library() if library_due or not cached_library else cached_library
            failed = scan_failed() if failed_due or not cache.get("failed_scanned_at") else cached_failed

            cur_keys = set(current)
            prev_keys = set(previous_inbox)
            failed_keys = set(failed.get("keys", set()))

            if not runtime_initialized:
                runtime_initialized = True
                state["initialized"] = True
                state["started_at"] = now
                state["baseline_books"] = library.get("books")
                previous_inbox = current
                previous_failed = failed_keys
                previous_book_count = library.get("books")
                previous_recent_ids = {
                    int(item["id"])
                    for item in library.get("recent", [])
                }

                add_event(
                    "system",
                    "event.monitor_started",
                    {
                        "queue": len(current),
                        "library": library.get("books") if library.get("books") is not None else "—",
                    },
                )

                if len(current) > 0 and not state["batch"].get("active"):
                    start_batch(
                        len(current),
                        current_size,
                        library.get("books"),
                        now,
                    )

            else:
                added = sorted(cur_keys - prev_keys)
                removed = sorted(prev_keys - cur_keys)

                if len(current) > 0 and not state["batch"].get("active"):
                    start_batch(
                        len(current),
                        current_size,
                        library.get("books"),
                        now,
                    )

                batch = state["batch"]

                if added:
                    if batch.get("active"):
                        batch["added"] = int(batch.get("added") or 0) + len(added)
                        batch["last_activity"] = now

                    if len(added) <= 8:
                        for rel in added:
                            add_event(
                                "queued",
                                "event.file_added",
                                {"file": rel},
                            )
                    else:
                        add_event(
                            "queued",
                            "event.batch_extended",
                            {"count": len(added)},
                        )

                if removed:
                    state["processed_since_start"] += len(removed)

                    if batch.get("active"):
                        batch["handled"] = int(batch.get("handled") or 0) + len(removed)
                        batch["last_activity"] = now

                    for _ in removed:
                        completion_times.append(now)

                    if len(removed) <= 8:
                        for rel in removed:
                            add_event(
                                "success",
                                "event.file_left_inbox",
                                {"file": rel},
                            )
                    else:
                        add_event(
                            "success",
                            "event.files_handled",
                            {"count": len(removed)},
                        )

                new_failed = sorted(failed_keys - previous_failed)

                if new_failed:
                    state["failed_since_start"] += len(new_failed)

                    if batch.get("active"):
                        batch["failed"] = int(batch.get("failed") or 0) + len(new_failed)
                        batch["last_activity"] = now

                    for rel in new_failed[:20]:
                        add_event(
                            "error",
                            "event.file_failed",
                            {"file": rel},
                        )

                    cfg = telegram_config()
                    if cfg["enabled"] and cfg["configured"] and cfg["notify_errors"]:
                        send_telegram(
                            tr(
                                "telegram.error",
                                {
                                    "count": len(new_failed),
                                    "file": new_failed[0] if len(new_failed) == 1 else "",
                                },
                                cfg["language"],
                            ),
                            "error",
                        )

                books = library.get("books")

                if (
                    books is not None
                    and previous_book_count is not None
                    and books != previous_book_count
                ):
                    delta = books - previous_book_count

                    if batch.get("active") and delta > 0:
                        batch["library_added"] = int(batch.get("library_added") or 0) + delta
                        batch["last_activity"] = now

                    add_event(
                        "success" if delta > 0 else "info",
                        "event.library_changed",
                        {
                            "before": previous_book_count,
                            "after": books,
                            "delta": f"{delta:+d}",
                        },
                    )

                recent_ids = {
                    int(item["id"])
                    for item in library.get("recent", [])
                }
                new_ids = sorted(
                    recent_ids - previous_recent_ids,
                    reverse=True,
                )

                if new_ids:
                    by_id = {
                        int(item["id"]): item
                        for item in library.get("recent", [])
                    }

                    for book_id in new_ids[:15]:
                        book = by_id.get(book_id, {})
                        add_event(
                            "success",
                            "event.book_added",
                            {"title": book.get("title") or "—"},
                            book.get("authors") or "",
                        )

                fingerprint = json.dumps(
                    {
                        "state": status.get("state"),
                        "file": status.get("file"),
                        "detail": status.get("detail"),
                        "updated_at": status.get("updated_at"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )

                if fingerprint != state["last_status_fingerprint"]:
                    state["last_status_fingerprint"] = fingerprint
                    status_name = (status.get("state") or "").lower()
                    filename = Path(status.get("file") or "").name

                    if status_name in {
                        "processing", "importing", "converting", "metadata", "fetching"
                    }:
                        add_event(
                            "processing",
                            "event.processing",
                            {"file": filename or "—"},
                            status.get("detail") or "",
                        )
                    elif status_name in {
                        "error", "failed", "timeout", "safety_timeout"
                    }:
                        add_event(
                            "error",
                            "event.ingest_error",
                            {"file": filename or "—"},
                            status.get("detail") or "",
                        )

                previous_inbox = current
                previous_failed = failed_keys
                previous_book_count = books
                previous_recent_ids = recent_ids

            with lock:
                cache["inbox"] = current
                cache["inbox_size"] = current_size
                cache["status"] = status
                cache["retry"] = retry
                if library_due or not cache.get("library_scanned_at"):
                    cache["library"] = library
                    cache["library_scanned_at"] = now
                cache["failed"] = {
                    key: value
                    for key, value in failed.items()
                    if key != "keys"
                }
                cache["failed_keys"] = set(failed.get("keys") or ())
                if failed_due or not cache.get("failed_scanned_at"):
                    cache["failed_scanned_at"] = now
                cache["updated_at"] = now
                state["last_scan"] = now

            batch = state["batch"]
            if batch.get("active"):
                # Self-correct total if the monitor missed a very fast add/remove cycle.
                inferred_total = int(batch.get("handled") or 0) + len(current)
                declared_total = int(batch.get("initial_count") or 0) + int(batch.get("added") or 0)
                if inferred_total > declared_total:
                    batch["added"] = inferred_total - int(batch.get("initial_count") or 0)

                maybe_detect_stall(len(current), library.get("books"), now)
                maybe_send_progress(len(current), library.get("books"), now)

                status_name = (status.get("state") or "").lower()
                last_activity = float(batch.get("last_activity") or batch.get("started_at") or now)

                if (
                    len(current) == 0
                    and status_name not in {
                        "processing", "importing", "converting", "metadata", "fetching"
                    }
                    and now - last_activity >= BATCH_COMPLETE_GRACE_SECONDS
                ):
                    complete_batch(library.get("books"), now)

            watcher_state["last_success"] = time.time()
            watcher_state["last_error"] = ""
            if now - last_persist >= PERSIST_SECONDS:
                persist()
                last_persist = now

        except Exception as exc:
            watcher_state["last_error"] = f"{type(exc).__name__}: {exc}"
            add_event(
                "error",
                "event.monitor_loop_error",
                {"error": watcher_state["last_error"]},
            )

        elapsed = time.time() - started
        time.sleep(max(0.25, POLL_SECONDS - elapsed))


def status_payload() -> dict:
    with lock:
        inbox = dict(cache["inbox"])
        inbox_size = int(cache["inbox_size"])
        status = dict(cache["status"])
        retry = dict(cache["retry"])
        library = dict(cache["library"])
        failed = dict(cache["failed"])
        evs = list(events)[:200]
        started_at = float(state["started_at"])
        last_scan = float(state["last_scan"])
        processed = int(state["processed_since_start"])
        failed_since_start = int(state["failed_since_start"])
        last_batch = dict(state.get("last_batch") or {})

    queue_count = len(inbox)
    rates = calculate_rates()
    batch = batch_snapshot(queue_count, library.get("books"))

    items = [
        {
            "path": rel,
            "name": Path(rel).name,
            "size": size,
            "size_h": human_bytes(size),
            "mtime": mtime,
        }
        for rel, (size, mtime) in sorted(
            inbox.items(),
            key=lambda item: (item[1][1], item[0].lower()),
        )[:250]
    ]

    status_name = (status.get("state") or "").lower()

    if batch.get("stalled"):
        activity = "stalled"
    elif status_name in {
        "processing", "importing", "converting", "metadata", "fetching"
    }:
        activity = "processing"
    elif status_name in {
        "error", "failed", "timeout", "safety_timeout"
    }:
        activity = "error"
    elif queue_count > 0:
        activity = "waiting"
    else:
        activity = "idle"

    status_age = None
    if status.get("updated_at"):
        status_age = max(
            0,
            int(time.time() - float(status["updated_at"])),
        )

    error_events = [
        item
        for item in evs
        if item.get("kind") == "error"
    ][:100]

    return {
        "now": time.time(),
        "activity": activity,
        "queue": {
            "count": queue_count,
            "size": inbox_size,
            "size_h": human_bytes(inbox_size),
            "items": items,
        },
        "session": {
            "uptime_seconds": max(0, int(time.time() - started_at)),
            "processed": processed,
            "failed": failed_since_start,
        },
        "batch": batch,
        "last_batch": last_batch,
        "rates": rates,
        "speed_history": speed_history(),
        "ingest_status": {
            **status,
            "age_seconds": status_age,
        },
        "retry": retry,
        "library": library,
        "errors": {
            "failed": failed,
            "events": error_events,
        },
        "events": evs,
        "paths": {
            "config": str(CONFIG_DIR),
            "inbox": str(INBOX_DIR),
            "library": str(LIBRARY_DIR),
            "lang": str(LANG_DIR),
        },
        "monitor": {
            "last_scan": last_scan,
            "poll_seconds": POLL_SECONDS,
            "stall_threshold_seconds": STALL_THRESHOLD_SECONDS,
            "complete_grace_seconds": BATCH_COMPLETE_GRACE_SECONDS,
        },
        "telegram": telegram_public_status(),
        "build": {
            "version": APP_VERSION,
            "build": BUILD_ID,
        },
        "links": {
            "cwa": CWA_WEB_URL,
        },
        "auth": {
            "required": authorization_status({}) != "missing_config",
        },
    }


def manual_telegram_status() -> tuple[bool, str]:
    payload = status_payload()
    cfg = telegram_config()

    if not cfg["enabled"]:
        return False, "disabled"

    return send_telegram(
        telegram_status_message(
            payload["queue"]["count"],
            payload["library"].get("books"),
        ),
        "manual_status",
    )


class Handler(BaseHTTPRequestHandler):
    server_version = f"CWAImportMonitor/{APP_VERSION}"

    def log_message(self, fmt, *args):
        pass

    def send_bytes(
        self,
        payload: bytes,
        content_type: str,
        status=HTTPStatus.OK,
        cache_control: str = "no-store",
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def send_json(
        self,
        payload: dict,
        status=HTTPStatus.OK,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
            extra_headers=extra_headers,
        )

    def ensure_read_access(self) -> bool:
        if authorization_status(self.headers) in {"ok", "missing_config"}:
            return True
        self.send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def ensure_action_access(self) -> bool:
        status = authorization_status(self.headers)
        if status == "ok":
            return True
        if status == "missing_config":
            self.send_json(
                {"ok": False, "error": "monitor_token_required"},
                HTTPStatus.FORBIDDEN,
            )
            return False
        self.send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            try:
                self.send_bytes(
                    INDEX_FILE.read_bytes(),
                    "text/html; charset=utf-8",
                )
            except Exception as exc:
                self.send_bytes(
                    str(exc).encode("utf-8"),
                    "text/plain; charset=utf-8",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return

        if path == "/favicon.svg":
            try:
                self.send_bytes(
                    FAVICON_FILE.read_bytes(),
                    "image/svg+xml",
                    HTTPStatus.OK,
                    "public, max-age=86400",
                )
            except Exception:
                self.send_bytes(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)
            return

        if path == "/api/auth":
            status = authorization_status(self.headers)
            self.send_json({
                "required": status != "missing_config",
                "authorized": status in {"ok", "missing_config"},
            })
            return

        if path == "/api/status":
            if self.ensure_read_access():
                self.send_json(status_payload())
            return

        if path == "/api/version":
            self.send_json({
                "version": APP_VERSION,
                "build": BUILD_ID,
            })
            return

        if path == "/api/languages":
            self.send_json({
                "default": DEFAULT_LANGUAGE,
                "languages": list_languages(),
            })
            return

        if path.startswith("/lang/") and path.endswith(".json"):
            code = path[len("/lang/"):-len(".json")]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
                self.send_bytes(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)
                return

            data = load_language(code)
            self.send_json(data)
            return

        if path == "/api/cover":
            if not self.ensure_read_access():
                return
            query = parse_qs(parsed.query)
            try:
                book_id = int((query.get("book_id") or [""])[0])
            except ValueError:
                book_id = 0

            cover = cover_path_for_book(book_id)
            if not cover:
                self.send_bytes(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)
                return

            try:
                self.send_bytes(
                    cover.read_bytes(),
                    "image/jpeg",
                    HTTPStatus.OK,
                    "private, max-age=3600",
                )
            except Exception:
                self.send_bytes(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)
            return

        if path == "/health":
            payload, status = health_payload()
            self.send_json(payload, status)
            return

        self.send_bytes(
            b"Not found",
            "text/plain; charset=utf-8",
            HTTPStatus.NOT_FOUND,
        )

    def read_json_body(self, limit: int = 4096) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > limit:
            return {"__too_large": True}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/login":
            body = self.read_json_body()
            if body.get("__too_large"):
                self.send_json({"ok": False, "error": "payload_too_large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return

            expected = monitor_token()
            if not expected:
                self.send_json({"ok": True, "required": False})
                return

            provided = str(body.get("token") or "").strip()
            if not token_matches(provided, expected):
                self.send_json({"ok": False, "error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
                return

            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
            cookie = (
                f"{AUTH_COOKIE}={quote(provided, safe='')}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age=2592000{secure}"
            )
            self.send_json(
                {"ok": True, "required": True},
                extra_headers=[("Set-Cookie", cookie)],
            )
            return

        if path == "/api/telegram/test":
            if not self.ensure_action_access():
                return
            cfg = telegram_config()
            if not cfg["enabled"]:
                self.send_json({"ok": False, "error": "disabled"}, HTTPStatus.CONFLICT)
                return

            ok, message = send_telegram(
                tr(
                    "telegram.test",
                    {
                        "version": APP_VERSION,
                        "build": BUILD_ID,
                    },
                    cfg["language"],
                ),
                "test",
            )
            self.send_json(
                {"ok": ok, "message": message},
                HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
            )
            return

        if path == "/api/telegram/status":
            if not self.ensure_action_access():
                return
            ok, message = manual_telegram_status()
            self.send_json(
                {"ok": ok, "message": message},
                HTTPStatus.OK if ok else HTTPStatus.CONFLICT,
            )
            return

        self.send_bytes(
            b"Not found",
            "text/plain; charset=utf-8",
            HTTPStatus.NOT_FOUND,
        )


def main() -> None:
    load_persistent()
    reset_runtime_state()

    worker = threading.Thread(
        target=watcher,
        name="cwa-monitor-watcher",
        daemon=True,
    )
    worker.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    print(
        f"CWA Import Monitor v{APP_VERSION} build {BUILD_ID} "
        f"listening on http://0.0.0.0:{PORT}",
        flush=True,
    )
    print(f"Inbox:   {INBOX_DIR}", flush=True)
    print(f"Library: {LIBRARY_DIR}", flush=True)
    print(f"Config:  {CONFIG_DIR}", flush=True)
    print(f"Lang:    {LANG_DIR} (+ builtin {BUILTIN_LANG_DIR})", flush=True)

    tg = telegram_public_status()
    print(
        f"Telegram: enabled={tg['enabled']} configured={tg['configured']} "
        f"language={tg['language']}",
        flush=True,
    )
    auth_required = authorization_status({}) != "missing_config"
    print(f"Auth: required={auth_required}", flush=True)
    if not auth_required:
        print(
            "WARNING: MONITOR_TOKEN is not set. Status data is readable without "
            "authentication, and Telegram HTTP actions stay disabled.",
            flush=True,
        )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
