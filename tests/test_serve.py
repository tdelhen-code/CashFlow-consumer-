import http.client
import http.server
import io
import json
import os
import re
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest import mock

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
SETTINGS = ("WALLET_DB", "WALLET_BACKUP_DIR", "WALLET_COPY_TO", "WALLET_PING_URL")

# Runs the server with a host name that gives two addresses, as localhost does
# on most systems (::1 and 127.0.0.1); here both are 127.0.0.1, other ports.
TWO_ADDRESSES = """
import runpy, socket
real = socket.getaddrinfo
def getaddrinfo(host, *args, **kwargs):
    if host == "wallet.test":
        return real("127.0.0.1", *args, **kwargs) * 2
    return real(host, *args, **kwargs)
socket.getaddrinfo = getaddrinfo
runpy.run_module("app.serve", run_name="__main__")
"""


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
            server.task_dispatcher.shutdown()   # the last response is finished first
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
        self.assertEqual(health["last_backup"]["prune_errors"], [])
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
    def listen(self, handler):
        """A local server for the pings to go to; returns its address."""
        listener = socketserver.TCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        return f"http://127.0.0.1:{listener.server_address[1]}"

    def hear(self):
        """A local HTTP server that answers 200; returns its address and the
        paths it heard."""
        heard = []

        class Hear(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                heard.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass
        return self.listen(Hear), heard

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
        address, heard = self.hear()
        make_wallet(self.db)[0].close()
        maintenance = S.Maintenance(str(self.db), backup_dir=str(self.backups),
                                    ping_url=address + "/check-1234", log=self.logged.append)
        maintenance.check()
        maintenance.backup()
        maintenance.ping()
        maintenance.backup_dir = str(self.db)
        maintenance.backup()
        maintenance.ping()
        self.assertEqual(heard, ["/check-1234", "/check-1234/fail"])

        # /fail goes on the path, before the query (healthchecks.io's ?create=1).
        maintenance.ping_url = address + "/key/slug/?create=1"
        maintenance.ping()
        self.assertEqual(heard[-1], "/key/slug/fail?create=1")

        maintenance.ping_url = "http://127.0.0.1:9/nobody"
        maintenance.ping()          # can't be reached: only logged
        self.assertTrue(any("could not reach the ping address" in line
                            for line in self.logged))

    def test_nothing_stops_the_timer(self):
        class Garbage(socketserver.StreamRequestHandler):
            def handle(self):       # not an HTTP answer at all
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                self.wfile.write(b"garbage\r\n")

        def broken():
            raise RuntimeError("a bug")
        make_wallet(self.db)[0].close()
        cases = {"a port that isn't one": ("http://127.0.0.1:80a/check", None),
                 "a space": ("http://127.0.0.1/check 1234", None),
                 "an answer that isn't HTTP": (self.listen(Garbage) + "/check", None),
                 "a step that fails": (None, broken)}
        for case, (url, ping) in cases.items():
            with self.subTest(case):
                logged, backups = [], self.dir / case
                maintenance = S.Maintenance(str(self.db), backup_dir=str(backups),
                                            ping_url=url, check_every=0.05, backup_every=0.05,
                                            first_run=0, log=logged.append)
                if ping:
                    maintenance.ping = ping
                maintenance.start()
                try:
                    self.assertTrue(wait_for(lambda: backups.exists()
                                             and len(B.list_backups(backups)) >= 2))
                    self.assertTrue(maintenance._thread.is_alive())
                finally:
                    maintenance.stop()
                told = "PROBLEM: the ping went wrong" if ping else "could not reach the ping"
                self.assertTrue(any(line.startswith(told) for line in logged), logged)

    def test_an_empty_backup_dir_means_no_backups(self):
        make_wallet(self.db)[0].close()
        maintenance = S.Maintenance(str(self.db), backup_dir="", check_every=0.05,
                                    first_run=0, log=self.logged.append)
        with mock.patch.object(B, "run", side_effect=AssertionError("backed up")):
            maintenance.start()
            try:
                self.assertTrue(wait_for(lambda: maintenance.status()["last_check"]))
            finally:
                maintenance.stop()
        status = maintenance.status()
        self.assertEqual((status["backups_on"], status["last_backup"], status["backup_overdue"]),
                         (False, None, False))

    def test_a_backup_whose_old_ones_could_not_be_removed(self):
        make_wallet(self.db)[0].close()
        error = f"{self.backups}: could not remove old backups: [Errno 13] Permission denied"
        made = {"path": str(self.backups / "wallet-20261018T120000Z.db.gz"),
                "name": "wallet-20261018T120000Z.db.gz", "size": 1234,
                "created_at": "2026-10-18T12:00:00+00:00", "schema_version": 1, "problems": [],
                "copies": [], "copy_errors": [], "pruned": [], "prune_errors": [error]}
        maintenance = S.Maintenance(str(self.db), backup_dir=str(self.backups), now=lambda: NOW,
                                    log=self.logged.append)
        with mock.patch.object(B, "run", return_value=made):
            maintenance.backup()
        status = maintenance.status()
        self.assertFalse(status["last_backup"]["ok"])
        self.assertEqual(status["last_backup"]["prune_errors"], [error])
        self.assertEqual(status["last_good_backup_at"], NOW.isoformat())   # the file is sound
        self.assertIn(f"PROBLEM: {error}", self.logged)


class MainTests(Folder):
    def setUp(self):
        super().setUp()
        # main() reads the WALLET_* settings, which the shell running the
        # tests may have set, and sets its own umask.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        for name in SETTINGS:
            os.environ.pop(name, None)
        umask = os.umask(0o077)
        os.umask(umask)
        self.addCleanup(os.umask, umask)

    def until_it_listens(self, *argv):
        """main() up to where it would start listening: (status, what it wrote)."""
        err = io.StringIO()
        with mock.patch.object(S, "create_server",
                               side_effect=S.ServeError("not listening in a test")), \
                redirect_stderr(err):
            status = S.main(list(argv))
        return status, err.getvalue()

    def test_a_second_server_on_the_same_database_is_refused(self):
        err = io.StringIO()
        with B.server_lock(self.db), redirect_stderr(err):
            status = S.main(["--db", str(self.db), "--port", "0"])
        self.assertEqual(status, 1)
        self.assertIn("another server is using this database", err.getvalue())

    def test_bad_settings_are_refused(self):
        for argv in (["--backup-every", "0"], ["--port", "70000"], ["--port", "-1"],
                     ["--ping-url", "file:///etc/passwd"], ["--ping-url", "https:///no-host"],
                     ["--ping-url", "https://hc-ping.com/1234 5678"],
                     ["--ping-url", "https://hc-ping.com/1234\x015678"],
                     ["--ping-url", "http://hc-ping.com:80a/uuid"],
                     ["--ping-url", "http://hc-ping.com:99999/uuid"],
                     ["--ping-url", "http://[::1/uuid"]):
            with self.subTest(argv), self.assertRaises(SystemExit):
                self.until_it_listens("--db", str(self.db), *argv)
        self.assertEqual(self.until_it_listens("--db", str(self.db), "--copy-to", "x"),
                         (1, "error: --copy-to needs --backup-dir\n"))

    def test_settings_can_come_from_the_environment(self):
        os.environ.update(WALLET_DB="env.db", WALLET_BACKUP_DIR="env-backups",
                          WALLET_COPY_TO=os.pathsep.join(["usb", "", "cloud"]),
                          WALLET_PING_URL="https://hc-ping.com/env")
        args = S._arguments([])
        self.assertEqual((args.db, args.backup_dir, args.copy_to, args.ping_url),
                         ("env.db", "env-backups", ["usb", "cloud"], "https://hc-ping.com/env"))
        # A flag wins over the variable, and an empty one turns it off.
        args = S._arguments(["--db", "flag.db", "--backup-dir", "flag-backups",
                             "--copy-to", "stick", "--ping-url", "https://hc-ping.com/flag"])
        self.assertEqual((args.db, args.backup_dir, args.copy_to, args.ping_url),
                         ("flag.db", "flag-backups", ["stick"], "https://hc-ping.com/flag"))
        args = S._arguments(["--backup-dir", "", "--copy-to", ""])
        self.assertEqual((args.backup_dir, args.copy_to), (None, []))
        # A typo in the variable shows at the start, not at the first ping.
        os.environ["WALLET_PING_URL"] = "https://hc-ping.com/1234 5678"
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            S._arguments([])

    def test_a_lost_database_is_not_replaced_by_an_empty_one(self):
        make_wallet(self.db)[0].close()
        B.snapshot(self.db, self.backups, now=NOW)
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.db}{suffix}").unlink(missing_ok=True)
        status, err = self.until_it_listens("--db", str(self.db),
                                            "--backup-dir", str(self.backups))
        self.assertEqual(status, 1)
        self.assertTrue(err.startswith(f"error: there is no database at {self.db}, "
                                       f"but {self.backups} has backups of it"), err)
        self.assertIn("manage restore", err)
        self.assertFalse(self.db.exists())

    def test_a_new_wallet_starts_without_backups_of_it(self):
        empty, others = self.dir / "empty", self.dir / "others"
        empty.mkdir()
        others.mkdir()
        (others / "other-20261018T120000Z.db.gz").write_bytes(b"")     # another database's
        for n, backups in enumerate((self.dir / "not-there-yet", empty, others)):
            with self.subTest(backups.name):
                db = self.dir / f"data-{n}" / "wallet.db"
                status, err = self.until_it_listens("--db", str(db), "--backup-dir", str(backups))
                self.assertEqual((status, err), (1, "error: not listening in a test\n"))
                self.assertTrue(db.is_file())

    @unittest.skipIf(os.name == "nt", "no umask on Windows")
    def test_what_it_makes_only_its_user_can_read(self):
        os.umask(0o022)             # as most shells have it
        db = self.dir / "data" / "wallet.db"
        self.until_it_listens("--db", str(db))
        for path in (db.parent, db, Path(f"{db}.lock")):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0, path)

    @unittest.skipUnless(HAVE_WAITRESS, "waitress is not installed")
    def test_a_host_it_cannot_listen_on_is_told_plainly(self):
        real = socket.getaddrinfo

        def getaddrinfo(host, *args, **kwargs):
            if host == "wallet.invalid":
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return real(host, *args, **kwargs)
        err = io.StringIO()
        with mock.patch("socket.getaddrinfo", getaddrinfo), redirect_stderr(err):
            status = S.main(["--db", str(self.db), "--host", "wallet.invalid", "--port", "0"])
        self.assertEqual(status, 1)
        self.assertTrue(err.getvalue().startswith("error: cannot listen on wallet.invalid"),
                        err.getvalue())

    def run_server(self, *command):
        """The server in its own process, without the shell's WALLET_* settings;
        returns it and its first log line."""
        env = {k: v for k, v in os.environ.items() if k not in SETTINGS}
        server = subprocess.Popen(
            [sys.executable, *command, "--db", str(self.db), "--port", "0",
             "--backup-dir", str(self.backups)],
            cwd=ROOT, env=env, stderr=subprocess.PIPE, text=True)
        self.addCleanup(server.stderr.close)
        self.addCleanup(lambda: server.poll() is None and server.kill())
        return server, server.stderr.readline()

    def ping(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/api/ping")
            return conn.getresponse().read()
        finally:
            conn.close()

    def stops_cleanly(self, server):
        server.send_signal(signal.SIGTERM)
        rest = server.stderr.read()
        self.assertEqual(server.wait(20), 0)
        self.assertIn("stopped", rest)
        self.assertNotIn("Traceback", rest)
        with B.server_lock(self.db):        # it let go of the database
            pass

    @unittest.skipUnless(HAVE_WAITRESS and hasattr(signal, "SIGTERM"), "needs waitress and SIGTERM")
    def test_runs_and_stops_cleanly_on_sigterm(self):
        make_wallet(self.db)[0].close()
        server, first = self.run_server("-m", "app.serve")
        port = int(re.search(r"http://127\.0\.0\.1:(\d+)", first).group(1))
        self.assertEqual(self.ping(port), b'{"ok": true}')
        self.stops_cleanly(server)

    @unittest.skipUnless(HAVE_WAITRESS and hasattr(signal, "SIGTERM"), "needs waitress and SIGTERM")
    def test_listens_on_every_address_of_its_host(self):
        make_wallet(self.db)[0].close()
        server, first = self.run_server("-c", TWO_ADDRESSES, "--host", "wallet.test")
        ports = [int(p) for p in re.findall(r"http://127\.0\.0\.1:(\d+)", first)]
        self.assertEqual(len(ports), 2, first)
        for port in ports:
            self.assertEqual(self.ping(port), b'{"ok": true}')
        self.stops_cleanly(server)


if __name__ == "__main__":
    unittest.main()
