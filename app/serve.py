"""The production server: the web app on waitress, with the ledger check and
the backups on a timer.

    pip install -r requirements.txt
    python3 -m app.serve --db /srv/wallet/wallet.db --backup-dir /srv/wallet/backups

- One process, holding the database's lock (backup.server_lock) for as long
  as it runs: a second server on the same database is refused, as the request
  limits are kept per process, and a restore can't happen under it.
- Behind an HTTPS proxy (Caddy; deploy/ has it set up). --proxy is the
  proxy's address (127.0.0.1 by default): waitress takes the client's address
  from that proxy's X-Forwarded-For only, and drops the header when anyone
  else sends it, so nobody can choose the address their login tries count for.
- Every --check-every minutes the ledger and order checks run; every
  --backup-every minutes (with --backup-dir) a backup is made, copied into
  each --copy-to folder, and old ones are pruned. How they went is in the log
  (PROBLEM lines) and on GET /api/admin/health under "maintenance". A failure
  is retried at the next run and never stops the server.
- --ping-url: an address called after each run (a healthchecks.io check, for
  example), with /fail added when something went wrong. Such a service tells
  a person when it hears /fail, and also when it stops hearing anything.
"""
import argparse
import select
import signal
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import auth as A
from . import backup as B
from . import ledger as L
from . import web as W

THREADS = 8
MAX_REQUEST_BYTES = 1024 * 1024     # waitress's own limit; the app's is 64 KiB
FIRST_RUN_SECONDS = 10              # the first check and backup, soon after start
OVERDUE_GRACE = timedelta(minutes=10)
PING_TIMEOUT = 10


class ServeError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _iso(when):
    return when.isoformat(timespec="seconds") if when else None


def log(message):
    print(f"{_iso(_now())} {message}", file=sys.stderr, flush=True)


class Maintenance:
    """The ledger check and the backups, on a timer in their own thread, and
    how the last ones went. One thread, so never two backups at once."""

    def __init__(self, db_path, *, backup_dir=None, copy_to=(), check_every=3600,
                 backup_every=3600, ping_url=None, first_run=FIRST_RUN_SECONDS,
                 now=_now, log=log):
        self.db_path, self.backup_dir, self.copy_to = db_path, backup_dir, list(copy_to)
        self.check_every, self.backup_every = check_every, backup_every
        self.ping_url, self.first_run = ping_url, first_run
        self.now, self.log = now, log
        self.started_at = now()
        self.last_check = self.last_backup = self.last_good_backup_at = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def status(self):
        """For GET /api/admin/health."""
        with self._lock:
            return {"last_check": self.last_check, "last_backup": self.last_backup,
                    "last_good_backup_at": _iso(self.last_good_backup_at),
                    "backups_on": self.backup_dir is not None,
                    "backup_overdue": self._overdue()}

    def _overdue(self):
        """No sound backup for more than twice the interval (10 minutes' grace),
        counting from the start until the first one."""
        if self.backup_dir is None:
            return False
        since = self.last_good_backup_at or self.started_at
        return self.now() - since > 2 * timedelta(seconds=self.backup_every) + OVERDUE_GRACE

    def check(self):
        at, problems, error = self.now(), [], None
        try:
            problems = B.check_database(self.db_path)["problems"]
        except Exception as e:      # whatever it is, the next run tries again
            error = f"{type(e).__name__}: {e}"
        with self._lock:
            self.last_check = {"at": _iso(at), "ok": not (problems or error),
                               "problems": len(problems), "error": error}
        if error:
            self.log(f"PROBLEM: the ledger check could not run: {error}")
        for problem in problems:
            self.log(f"PROBLEM: {problem}")
        if not (problems or error):
            self.log("ledger check: ok")

    def backup(self):
        at = self.now()
        try:
            result = B.run(self.db_path, self.backup_dir, self.copy_to, now=at)
        except Exception as e:      # the next run tries again
            error = f"{type(e).__name__}: {e}"
            with self._lock:
                self.last_backup = {"at": _iso(at), "ok": False, "name": None,
                                    "error": error, "problems": 0, "copy_errors": []}
            self.log(f"PROBLEM: the backup failed: {error}")
            return
        problems, copy_errors = result["problems"], result["copy_errors"]
        with self._lock:
            self.last_good_backup_at = at       # a sound file was made
            self.last_backup = {"at": _iso(at), "ok": not (problems or copy_errors),
                                "name": result["name"], "error": None,
                                "problems": len(problems), "copy_errors": copy_errors}
        self.log(f"backup: {result['name']} ({result['size']} bytes)"
                 + "".join(f", copied to {c}" for c in result["copies"]))
        for error in copy_errors:
            self.log(f"PROBLEM: the backup could not be copied to {error}")
        if problems:
            self.log(f"PROBLEM: the backup was made, but the ledger check found "
                     f"{len(problems)} problem(s) in it")

    def ping(self):
        if not self.ping_url:
            return
        with self._lock:
            ok = all(r is None or r["ok"] for r in (self.last_check, self.last_backup)) \
                and not self._overdue()
        url = self.ping_url if ok else self.ping_url.rstrip("/") + "/fail"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "lyd-wallet"})
            with urllib.request.urlopen(request, timeout=PING_TIMEOUT) as answer:
                answer.read(1024)
        except (OSError, ValueError) as e:      # URLError and HTTPError are OSErrors
            self.log(f"could not reach the ping address: {e}")

    def _run(self):
        clock = time.monotonic
        next_check = next_backup = clock() + self.first_run
        if self.backup_dir is None:
            next_backup = float("inf")
        while not self._stop.wait(max(0, min(next_check, next_backup) - clock())):
            due = clock()
            if due >= next_check:
                self.check()
                next_check = due + self.check_every
            if due >= next_backup:
                self.backup()
                next_backup = due + self.backup_every
            self.ping()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="maintenance", daemon=True)
        self._thread.start()

    def stop(self, timeout=120):
        """Stop after what it is doing (a backup under way is finished)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def create_server(app, *, host="127.0.0.1", port=8000, proxy="127.0.0.1", threads=THREADS):
    """A waitress server for `app` (call run() on it). Only `proxy` may say who
    the client is (X-Forwarded-For); the header is dropped from anyone else."""
    try:
        from waitress.server import create_server as waitress_server
    except ImportError:
        raise ServeError("waitress is not installed: pip install -r requirements.txt") \
            from None
    options = dict(host=host, port=port, threads=threads,
                   max_request_body_size=MAX_REQUEST_BYTES,
                   clear_untrusted_proxy_headers=True,
                   asyncore_use_poll=hasattr(select, "poll"))
    if proxy:
        options.update(trusted_proxy=proxy, trusted_proxy_count=1,
                       trusted_proxy_headers={"x-forwarded-for"})
    return waitress_server(app, **options)


def _stop_on_signals():
    """SIGTERM (docker stop, systemd) stops the server like Ctrl+C does."""
    def stop(signum, frame):
        raise KeyboardInterrupt
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)


def _minutes(text):
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("must be more than 0 minutes")
    return value * 60


def _ping_url(text):
    if urllib.parse.urlsplit(text).scheme not in ("http", "https"):
        raise argparse.ArgumentTypeError("must be an http:// or https:// address")
    return text


def _parser():
    parser = argparse.ArgumentParser(prog="python3 -m app.serve",
                                     description="LYD wallet production server")
    parser.add_argument("--db", required=True, help="the database file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--proxy", default="127.0.0.1",
                        help="address of the HTTPS proxy in front (empty: none)")
    parser.add_argument("--threads", type=int, default=THREADS)
    parser.add_argument("--backup-dir", help="folder for the backups (none: no backups)")
    parser.add_argument("--copy-to", action="append", default=[], metavar="FOLDER",
                        help="also copy each backup here (can be given more than once)")
    parser.add_argument("--backup-every", type=_minutes, default=3600, metavar="MINUTES",
                        help="minutes between backups (60)")
    parser.add_argument("--check-every", type=_minutes, default=3600, metavar="MINUTES",
                        help="minutes between ledger checks (60)")
    parser.add_argument("--ping-url", type=_ping_url,
                        help="address to call after each check and backup")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.copy_to and not args.backup_dir:
        print("error: --copy-to needs --backup-dir", file=sys.stderr)
        return 1
    try:
        with B.server_lock(args.db):
            app = W.make_app(args.db)
            maintenance = Maintenance(args.db, backup_dir=args.backup_dir,
                                      copy_to=args.copy_to, check_every=args.check_every,
                                      backup_every=args.backup_every, ping_url=args.ping_url)
            app.maintenance = maintenance
            server = create_server(app, host=args.host, port=args.port,
                                   proxy=args.proxy, threads=args.threads)
            _stop_on_signals()
            maintenance.start()
            log(f"LYD wallet on http://{args.host}:{server.effective_port} "
                f"(database {args.db}; proxy {args.proxy or 'none'}; "
                f"backups {args.backup_dir or 'off'})")
            if not _has_admin(args.db):
                log("no admin yet: create one with python3 -m app.manage first-admin")
            try:
                server.run()            # until SIGTERM or Ctrl+C
            except KeyboardInterrupt:
                pass
            finally:
                log("stopping")
                server.close()
                maintenance.stop()
                log("stopped")
    except (ServeError, B.BackupError, L.LedgerError, sqlite3.Error, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:       # stopped while starting
        log("stopped")
    return 0


def _has_admin(db_path):
    conn = A.connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone() is not None
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
