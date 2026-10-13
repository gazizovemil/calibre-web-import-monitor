import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import app


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            "MONITOR_TOKEN": app.MONITOR_TOKEN,
            "DATA_DIR": app.DATA_DIR,
            "STATE_FILE": app.STATE_FILE,
            "EVENTS_FILE": app.EVENTS_FILE,
            "LIBRARY_DIR": app.LIBRARY_DIR,
            "state": json.loads(json.dumps(app.state)),
            "events": list(app.events),
            "watcher": dict(app.watcher_state),
        }

    def tearDown(self):
        app.MONITOR_TOKEN = self.original["MONITOR_TOKEN"]
        app.DATA_DIR = self.original["DATA_DIR"]
        app.STATE_FILE = self.original["STATE_FILE"]
        app.EVENTS_FILE = self.original["EVENTS_FILE"]
        app.LIBRARY_DIR = self.original["LIBRARY_DIR"]
        app.state.clear()
        app.state.update(json.loads(json.dumps(self.original["state"])))
        app.events.clear()
        app.events.extend(self.original["events"])
        app.watcher_state.clear()
        app.watcher_state.update(self.original["watcher"])

    def use_temp_data(self):
        directory = Path(tempfile.mkdtemp())
        app.DATA_DIR = directory
        app.STATE_FILE = directory / "state.json"
        app.EVENTS_FILE = directory / "events.json"
        return directory

    def test_windows_and_unix_status_paths(self):
        windows = app.parse_status(r"processing:C:\books\a.epub:ignored:detail", None)
        self.assertEqual(windows["state"], "processing")
        self.assertEqual(windows["file"], r"C:\books\a.epub")
        self.assertEqual(windows["detail"], "detail")

        unix = app.parse_status("processing:/books/a.epub:ignored:detail", None)
        self.assertEqual(unix["file"], "/books/a.epub")
        self.assertEqual(unix["detail"], "detail")

    def test_session_counters_do_not_survive_restart(self):
        self.use_temp_data()
        app.STATE_FILE.write_text(json.dumps({
            "processed_since_start": 99,
            "failed_since_start": 7,
            "batch": {"active": True, "id": 4, "last_activity": 1},
        }), encoding="utf-8")

        app.state["processed_since_start"] = 0
        app.state["failed_since_start"] = 0
        app.load_persistent()
        self.assertEqual(app.state["processed_since_start"], 0)
        self.assertEqual(app.state["failed_since_start"], 0)
        self.assertTrue(app.state["batch"]["active"])

        app.reset_runtime_state()
        self.assertEqual(app.state["processed_since_start"], 0)
        self.assertFalse(app.state["batch"]["active"])
        self.assertTrue(app.state["last_batch"]["interrupted"])
        self.assertEqual(app.state["last_batch"]["id"], 4)

    def test_sqlite_connection_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "metadata.db"
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE books (id INTEGER, title TEXT, path TEXT, has_cover INTEGER);
                CREATE TABLE authors (id INTEGER, name TEXT);
                CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
                CREATE TABLE data (book INTEGER, format TEXT);
                INSERT INTO books VALUES (1, 'Title', 'Author/Title', 0);
                INSERT INTO authors VALUES (1, 'Author');
                INSERT INTO books_authors_link VALUES (1, 1);
                INSERT INTO data VALUES (1, 'EPUB');
            """)
            con.commit()
            con.close()

            app.LIBRARY_DIR = root
            result = app.read_library()
            self.assertTrue(result["available"])
            self.assertEqual(result["books"], 1)
            db.unlink()

    def test_health_reports_stale_watcher(self):
        app.watcher_state["last_success"] = 0
        payload, status = app.health_payload()
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])

        app.watcher_state["last_success"] = time.time()
        payload, status = app.health_payload()
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        app.watcher_state["last_success"] = time.time() - app.HEALTH_STALE_SECONDS - 5
        payload, status = app.health_payload()
        self.assertEqual(status, 503)

    def test_token_matching(self):
        app.MONITOR_TOKEN = ""
        self.assertEqual(app.authorization_status({}), "missing_config")

        app.MONITOR_TOKEN = "secret"
        self.assertEqual(app.authorization_status({"X-Monitor-Token": "secret"}), "ok")
        self.assertEqual(app.authorization_status({"Authorization": "Bearer secret"}), "ok")
        self.assertEqual(app.authorization_status({"X-Monitor-Token": "wrong"}), "unauthorized")

    def test_http_auth_and_favicon(self):
        app.MONITOR_TOKEN = "secret"
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + "/api/status", timeout=3)
            self.assertEqual(denied.exception.code, 401)

            request = urllib.request.Request(base + "/api/telegram/test", data=b"", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as telegram:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(telegram.exception.code, 401)

            favicon = urllib.request.urlopen(base + "/favicon.svg", timeout=3)
            self.assertEqual(favicon.status, 200)
            self.assertIn(b"<svg", favicon.read())

            login = urllib.request.Request(
                base + "/api/login",
                data=json.dumps({"token": "secret"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self.assertEqual(opener.open(login, timeout=3).status, 200)
            self.assertEqual(opener.open(base + "/api/status", timeout=3).status, 200)
        finally:
            server.shutdown()
            server.server_close()

    def test_telegram_action_requires_configured_token(self):
        app.MONITOR_TOKEN = ""
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = urllib.request.Request(base + "/api/telegram/status", data=b"", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 403)
            body = json.loads(error.exception.read().decode("utf-8"))
            self.assertEqual(body["error"], "monitor_token_required")
        finally:
            server.shutdown()
            server.server_close()

    def test_translation_keys_match(self):
        root = Path(__file__).resolve().parents[1] / "lang"
        english = json.loads((root / "en.json").read_text(encoding="utf-8"))["strings"]
        russian = json.loads((root / "ru.json").read_text(encoding="utf-8"))["strings"]
        self.assertEqual(set(english), set(russian))


if __name__ == "__main__":
    unittest.main()
