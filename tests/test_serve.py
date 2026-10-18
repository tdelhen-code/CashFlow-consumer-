import http.client
import http.server
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path

from app import auth as A
from app import backup as B
from app import serve as S
from app import web as W

from .test_backup import NOW, make_wallet, unbalance

try:
    import waitress  # noqa: F401
    HAVE_WAITRESS = True
except ImportError:
    HAVE_WAITRESS = False

ROOT = Path(__file__).resolve().parent.parent


def wait_for(condition, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


class Folder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "wallet.db"
        self.backups = self.dir / "backups"
        self.logged = []


@unittest.skipUnless(HAVE_WAITRESS, "waitress is not installed")
class WaitressTests(Folder):
    def start(self, app, proxy="127.0.0.1"):
        server = S.create_server(app, host="127.0.0.1", port=0, proxy=proxy, threads=2)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        def stop():
            server.close()
            thread.join(10)
        self.addCleanup(stop)
        self.port = server.effective_port

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = None if body is None else json.dumps(body).encode()
            headers = dict(headers or {})
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, data, headers)
            answer = conn.getresponse()
            return answer.status, json.loads(answer.read() or b"null")
        finally:
            conn.close()

    def wrong_login(self, forwarded_for):
        return self.call("POST", "/api/login", {"phone": "0919999999", "password": "x" * 12},
                         {"X-Forwarded-For": forwarded_for})[0]

    def limited_app(self):
        return W.make_app(str(self.db), secure_cookies=False,
                          login_limiter=W.RateLimiter(2, 600))

    def test_the_client_is_who_the_trusted_proxy_says(self):
        self.start(self.limited_app(), proxy="127.0.0.1")
        self.assertEqual([self.wrong_login("198.51.100.1") for _ in range(3)], [401, 401, 429])
        self.assertEqual(self.wrong_login("198.51.100.2"), 401)        # another client
        # The proxy's own list ends with the client it saw; earlier ones are
        # what the client sent, and don't count.
        self.assertEqual(self.wrong_login("203.0.113.9, 198.51.100.3"), 401)
        self.assertEqual(self.wrong_login("198.51.100.1, 198.51.100.3"), 401)
        self.assertEqual(self.wrong_login("198.51.100.3"), 429)
        # IPv6 counts per /64, also as waitress passes it on, in brackets.
        self.assertEqual([self.wrong_login("2001:db8:1:2::1"), self.wrong_login("2001:db8:1:2::2")],
                         [401, 401])
        self.assertEqual(self.wrong_login("2001:db8:1:2::ff"), 429)
        self.assertEqual(self.wrong_login("2001:db8:1:3::1"), 401)

    def test_anyone_else_cannot_choose_their_address(self):
        self.start(self.limited_app(), proxy="10.255.255.1")       # not this test
        self.assertEqual([self.wrong_login("198.51.100.1"), self.wrong_login("198.51.100.2")],
                         [401, 401])
        self.assertEqual(self.wrong_login("198.51.100.3"), 429)    # all count for 127.0.0.1

    def test_ping_and_the_health_of_backups(self):
        conn, admin, _ = make_wallet(self.db)
        conn.close()
        app = W.make_app(str(self.db), secure_cookies=False)
        maintenance = S.Maintenance(str(self.db), backup_dir=str(self.backups),
                                    log=self.logged.append)
        app.maintenance = maintenance
        self.start(app)
        self.assertEqual(self.call("GET", "/api/ping"), (200, {"ok": True}))

        status, body = self.call("POST", "/api/login",
                                 {"phone": "0910000000", "password": "a long admin secret"})
        self.assertEqual(status, 200)
        bearer = {"Authorization": f"Bearer {body['token']}"}
        health = self.call("GET", "/api/admin/health", headers=bearer)[1]
        self.assertEqual(health["maintenance"]["last_backup"], None)
        self.assertFalse(health["maintenance"]["backup_overdue"])

        maintenance.check()
        maintenance.backup()
        health = self.call("GET", "/api/admin/health", headers=bearer)[1]["maintenance"]
        self.assertTrue(health["last_check"]["ok"])
        self.assertTrue(health["last_backup"]["ok"])
        self.assertTrue((self.backups / health["last_backup"]["name"]).is_file())
        self.assertIsNotNone(health["last_good_backup_at"])

        # A backup that can't be made is told, and the server goes on.
        maintenance.backup_dir = str(self.db)       # a file, not a folder
        maintenance.backup()
        health = self.call("GET", "/api/admin/health", headers=bearer)[1]["maintenance"]
        self.assertFalse(health["last_backup"]["ok"])
        self.assertTrue(health["last_backup"]["error"])
        self.assertTrue(any(line.startswith("PROBLEM: the backup failed")
                            for line in self.logged))
        self.assertEqual(self.call("GET", "/api/ping"), (200, {"ok": True}))


class MaintenanceTests(Folder):
    def test_runs_the_check_and_backups_on_its_timer(self):
        make_wallet(self.db)[0].close()
        maintenance = S.Maintenance(str(self.db), backup_dir=str(self.backups),
                                    copy_to=[str(self.dir / "usb")], check_every=0.05,
                                    backup_every=0.05, first_run=0, log=self.logged.append)
        maintenance.start()
        try:
            self.assertTrue(wait_for(lambda: self.backups.exists()
                                     and len(B.list_backups(self.backups)) >= 2))
        finally:
            maintenance.stop()
        status = maintenance.status()
        self.assertTrue(status["last_check"]["ok"])
        self.assertTrue(status["last_backup"]["ok"])
        self.assertTrue(status["backups_on"])
        self.assertTrue(list((self.dir / "usb").iterdir()))
        self.assertIn("ledger check: ok", self.logged)
        self.assertFalse(maintenance._thread.is_alive())

    def test_ledger_problems_are_logged_and_shown(self):
        conn = make_wallet(self.db)[0]
        unbalance(conn)
        conn.close()
        maintenance = S.Maintenance(str(self.db), backup_dir=str(self.backups),
                                    log=self.logged.append)
        maintenance.check()
        maintenance.backup()
        status = maintenance.status()
        self.assertFalse(status["last_check"]["ok"])
        self.assertGreater(status["last_check"]["problems"], 0)
        self.assertFalse(status["last_backup"]["ok"])           # made, with problems
        self.assertTrue(status["last_backup"]["name"])
        self.assertTrue(any(line.startswith("PROBLEM:") for line in self.logged))

    def test_overdue_when_no_sound_backup_for_twice_the_interval(self):
        make_wallet(self.db)[0].close()
        clock = [NOW]
        maintenance = S.Maintenance(str(self.db), backup_dir=str(self.backups),
                                    backup_every=3600, now=lambda: clock[0],
                                    log=self.logged.append)
        clock[0] = NOW + timedelta(hours=2, minutes=9)
        self.assertFalse(maintenance.status()["backup_overdue"])
        clock[0] = NOW + timedelta(hours=2, minutes=11)
        self.assertTrue(maintenance.status()["backup_overdue"])     # none since the start
        maintenance.backup()
        self.assertFalse(maintenance.status()["backup_overdue"])
        maintenance.backup_dir = str(self.db)                       # failing from now on
        clock[0] += timedelta(hours=1)
        maintenance.backup()
        self.assertFalse(maintenance.status()["backup_overdue"])
        clock[0] += timedelta(hours=1, minutes=11)
        self.assertTrue(maintenance.status()["backup_overdue"])
        no_backups = S.Maintenance(str(self.db), now=lambda: clock[0] + timedelta(days=9))
        self.assertFalse(no_backups.status()["backup_overdue"])
        self.assertFalse(no_backups.status()["backups_on"])

    def test_pings_the_address_with_fail_when_something_is_wrong(self):
        heard = []

        class Hear(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                heard.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        listener = http.server.HTTPServer(("127.0.0.1", 0), Hear)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        url = f"http://127.0.0.1:{listener.server_port}/check-1234"

        make_wallet(self.db)[0].close()
        maintenance = S.Maintenance(str(self.db), backup_dir=str(self.backups), ping_url=url,
                                    log=self.logged.append)
        maintenance.check()
        maintenance.backup()
        maintenance.ping()
        maintenance.backup_dir = str(self.db)
        maintenance.backup()
        maintenance.ping()
        self.assertEqual(heard, ["/check-1234", "/check-1234/fail"])

        listener.shutdown()
        listener.server_close()
        maintenance.ping_url = "http://127.0.0.1:9/nobody"
        maintenance.ping()          # can't be reached: only logged
        self.assertTrue(any("could not reach the ping address" in line
                            for line in self.logged))


class MainTests(Folder):
    def test_a_second_server_on_the_same_database_is_refused(self):
        err = io.StringIO()
        with B.server_lock(self.db), redirect_stderr(err):
            status = S.main(["--db", str(self.db), "--port", "0"])
        self.assertEqual(status, 1)
        self.assertIn("another server is using this database", err.getvalue())

    def test_bad_settings_are_refused(self):
        for argv in (["--backup-every", "0"], ["--ping-url", "file:///etc/passwd"]):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                S.main(["--db", str(self.db)] + argv)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(S.main(["--db", str(self.db), "--copy-to", "x"]), 1)

    @unittest.skipUnless(HAVE_WAITRESS and hasattr(signal, "SIGTERM"), "needs waitress and SIGTERM")
    def test_runs_and_stops_cleanly_on_sigterm(self):
        make_wallet(self.db)[0].close()
        server = subprocess.Popen(
            [sys.executable, "-m", "app.serve", "--db", str(self.db), "--port", "0",
             "--backup-dir", str(self.backups)],
            cwd=ROOT, stderr=subprocess.PIPE, text=True)
        self.addCleanup(server.stderr.close)
        self.addCleanup(lambda: server.poll() is None and server.kill())
        first = server.stderr.readline()
        port = int(re.search(r"http://127\.0\.0\.1:(\d+)", first).group(1))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/ping")
        self.assertEqual(conn.getresponse().read(), b'{"ok": true}')
        conn.close()
        server.send_signal(signal.SIGTERM)
        rest = server.stderr.read()
        self.assertEqual(server.wait(20), 0)
        self.assertIn("stopped", rest)
        self.assertNotIn("Traceback", rest)
        with B.server_lock(self.db):        # it let go of the database
            pass


if __name__ == "__main__":
    unittest.main()
