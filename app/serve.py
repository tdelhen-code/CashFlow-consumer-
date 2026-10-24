"""The production server: the web app on waitress, with the ledger check and
the backups on a timer.

    python3 -m venv .venv
    .venv/bin/pip install --require-hashes -r requirements.txt
    .venv/bin/python -m app.serve --db /srv/wallet/wallet.db --backup-dir /srv/wallet/backups

(waitress goes into a virtual environment: the system's own pip refuses to
install anything on Ubuntu 24.04 and the other systems that follow PEP 668.)

- One process, holding the database's lock (backup.server_lock) for as long
  as it runs: a second server on the same database is refused, as the request
  limits are kept per process, and a restore can't happen under it.
- The files and folders it makes (the database, the backups) are for the
  user it runs as only (umask 077; not on Windows).
- It refuses to start when the --db file is not there but --backup-dir
  already has backups of it: a lost database is restored (python3 -m
  app.manage restore), never quietly replaced by an empty one whose backups
  would push the real ones out. A new wallet needs an empty backup folder.
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
- In a container the settings can come from the environment instead:
  WALLET_DB, WALLET_BACKUP_DIR, WALLET_COPY_TO (folders separated by
  os.pathsep) and WALLET_PING_URL; a flag given on the command line wins.
"""
import argparse
import os
import select
import signal
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
                    "backups_on": bool(self.backup_dir),
                    "backup_overdue": self._overdue()}

    def _overdue(self):
        """No sound backup for more than twice the interval (10 minutes' grace),
        counting from the start until the first one."""
        if not self.backup_dir:
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
                                    "error": error, "problems": 0, "copy_errors": [],
                                    "prune_errors": []}
            self.log(f"PROBLEM: the backup failed: {error}")
            return
        problems, copy_errors = result["problems"], result["copy_errors"]
        prune_errors = result.get("prune_errors", [])
        with self._lock:
            self.last_good_backup_at = at       # a sound file was made
            self.last_backup = {"at": _iso(at),
                                "ok": not (problems or copy_errors or prune_errors),
                                "name": result["name"], "error": None,
                                "problems": len(problems), "copy_errors": copy_errors,
                                "prune_errors": prune_errors}
        self.log(f"backup: {result['name']} ({result['size']} bytes)"
                 + "".join(f", copied to {c}" for c in result["copies"]))
        for error in copy_errors:       # each one names its folder
            self.log(f"PROBLEM: copying the backup failed: {error}")
        for error in prune_errors:
            self.log(f"PROBLEM: {error}")
        if problems:
            self.log(f"PROBLEM: the backup was made, but the ledger check found "
                     f"{len(problems)} problem(s) in it")

    def ping(self):
        if not self.ping_url:
            return
        with self._lock:
            ok = all(r is None or r["ok"] for r in (self.last_check, self.last_backup)) \
                and not self._overdue()
        try:
            url = self.ping_url
            if not ok:          # /fail goes on the path, before any ?query
                parts = urllib.parse.urlsplit(url)
                url = parts._replace(path=parts.path.rstrip("/") + "/fail").geturl()
            request = urllib.request.Request(url, headers={"User-Agent": "lyd-wallet"})
            with urllib.request.urlopen(request, timeout=PING_TIMEOUT) as answer:
                answer.read(1024)
        except Exception as e:      # not only OSErrors: a bad URL, a reply that isn't HTTP
            self.log(f"could not reach the ping address: {type(e).__name__}: {str(e).strip()}")

    def _step(self, what, step):
        """Run one step of the timer. Whatever goes wrong in it is logged and
        the timer goes on: a thread that ended would make no more checks or
        backups, and tell nobody."""
        try:
            step()
        except Exception as e:
            self.log(f"PROBLEM: the {what} went wrong: {type(e).__name__}: {e}")

    def _run(self):
        clock = time.monotonic
        next_check = next_backup = clock() + self.first_run
        if not self.backup_dir:
            next_backup = float("inf")
        while not self._stop.wait(max(0, min(next_check, next_backup) - clock())):
            due = clock()
            if due >= next_check:
                self._step("ledger check", self.check)
                next_check = due + self.check_every
            if due >= next_backup:
                self._step("backup", self.backup)
                next_backup = due + self.backup_every
            self._step("ping", self.ping)

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
        raise ServeError("waitress is not installed: python3 -m venv .venv, then "
                         ".venv/bin/pip install --require-hashes -r requirements.txt, "
                         "and start the server with .venv/bin/python -m app.serve") \
            from None
    options = dict(host=host, port=port, threads=threads,
                   max_request_body_size=MAX_REQUEST_BYTES,
                   clear_untrusted_proxy_headers=True,
                   asyncore_use_poll=hasattr(select, "poll"))
    if proxy:
        options.update(trusted_proxy=proxy, trusted_proxy_count=1,
                       trusted_proxy_headers={"x-forwarded-for"})
    try:
        return waitress_server(app, **options)
    except ValueError as e:     # waitress: a host that doesn't resolve, for one
        raise ServeError(f"cannot listen on {host} port {port}: {e}") from None


def _addresses(server):
    """Where `server` listens. --host can give more than one address (localhost
    is ::1 and 127.0.0.1 on most systems), and then waitress makes one server
    for all of them, with a list instead of one host and port."""
    listen = getattr(server, "effective_listen", None) or \
        [(server.effective_host, server.effective_port)]
    return " and ".join(f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
                        for host, port in listen)


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


def _port(text):
    value = int(text)
    if not 0 <= value <= 65535:     # the system would quietly take 70000 as 4464
        raise argparse.ArgumentTypeError("must be from 0 to 65535")
    return value


def _ping_url(text):
    """An http(s) address with a host, and nothing in it that would only fail
    at the first ping: a space, a control character, a port that isn't one."""
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port                  # ValueError when it isn't a port number
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"is not a usable address: {e}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise argparse.ArgumentTypeError("must be an http:// or https:// address")
    if any(c.isspace() or not c.isprintable() for c in text):
        raise argparse.ArgumentTypeError("must not have spaces or control characters")
    return text


def _env(name):
    return os.environ.get(name) or None


def _parser():
    parser = argparse.ArgumentParser(prog="python3 -m app.serve",
                                     description="LYD wallet production server")
    parser.add_argument("--db", default=_env("WALLET_DB"), required=not _env("WALLET_DB"),
                        help="the database file (or WALLET_DB)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8000)
    parser.add_argument("--proxy", default="127.0.0.1",
                        help="address of the HTTPS proxy in front (empty: none)")
    parser.add_argument("--threads", type=int, default=THREADS)
    parser.add_argument("--backup-dir", default=_env("WALLET_BACKUP_DIR"),
                        help="folder for the backups (or WALLET_BACKUP_DIR; none: no backups)")
    parser.add_argument("--copy-to", action="append", metavar="FOLDER",
                        help="also copy each backup here (can be given more than once; "
                             "or WALLET_COPY_TO)")
    parser.add_argument("--backup-every", type=_minutes, default=3600, metavar="MINUTES",
                        help="minutes between backups (60)")
    parser.add_argument("--check-every", type=_minutes, default=3600, metavar="MINUTES",
                        help="minutes between ledger checks (60)")
    parser.add_argument("--ping-url", type=_ping_url, default=_env("WALLET_PING_URL"),
                        help="address to call after each check and backup (or WALLET_PING_URL)")
    return parser


def _arguments(argv):
    """The settings: the command line, then WALLET_* for what it leaves out."""
    args = _parser().parse_args(argv)
    args.backup_dir = args.backup_dir or None       # --backup-dir "": no backups
    if args.copy_to is None:
        args.copy_to = (_env("WALLET_COPY_TO") or "").split(os.pathsep)
    args.copy_to = [f for f in args.copy_to if f]
    return args


def _has_backups(backup_dir, db_path):
    """Whether `backup_dir` holds backups of the database at `db_path` (they
    are named for its stem). A folder that isn't there yet holds none."""
    stem = Path(db_path).stem
    try:
        found = B.list_backups(backup_dir)
    except FileNotFoundError:
        return False
    return any(B.NAME.match(b["name"])["stem"] == stem for b in found)


def main(argv=None):
    if os.name != "nt":
        os.umask(0o077)         # the database and its backups: for this user only
    args = _arguments(argv)
    if args.copy_to and not args.backup_dir:
        print("error: --copy-to needs --backup-dir", file=sys.stderr)
        return 1
    try:
        with B.server_lock(args.db):
            # Connecting would make a new, empty database: its backups would
            # then push out the ones of the database that went missing.
            if args.backup_dir and not Path(args.db).exists() \
                    and _has_backups(args.backup_dir, args.db):
                raise ServeError(
                    f"there is no database at {args.db}, but {args.backup_dir} has "
                    "backups of it: restore one with python3 -m app.manage restore "
                    "(with Docker: docker compose run --rm app manage restore "
                    "/backups/<file>), or use an empty backup folder for a new wallet")
            app = W.make_app(args.db)
            maintenance = Maintenance(args.db, backup_dir=args.backup_dir,
                                      copy_to=args.copy_to, check_every=args.check_every,
                                      backup_every=args.backup_every, ping_url=args.ping_url)
            app.maintenance = maintenance
            server = create_server(app, host=args.host, port=args.port,
                                   proxy=args.proxy, threads=args.threads)
            log(f"LYD wallet on {_addresses(server)} "
                f"(database {args.db}; proxy {args.proxy or 'none'}; "
                f"backups {args.backup_dir or 'off'})")
            app.warn_missing_pages()    # a broken install shows in the log at start
            if not _has_admin(args.db):
                log("no admin yet: create the first one with python3 -m app.manage "
                    "first-admin (with Docker: docker compose exec app manage first-admin)")
            _stop_on_signals()
            try:
                maintenance.start()     # in the try, so that it is always stopped
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
