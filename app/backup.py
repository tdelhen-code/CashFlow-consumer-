"""Backups of the wallet database, checking them, and putting one back.

- snapshot() copies the database with SQLite's own backup, so the copy is
  consistent while the server runs (including what is still only in the -wal
  file), checks it (the file is sound, and the ledger and order checks) and
  keeps it gzip-compressed as one file, named for when it was taken (UTC):
  wallet-20261018T052600Z.db.gz. Never copy the .db file by hand while the
  server runs: the copy can be half old, half new.
- prune() thins out a backup folder: every backup of the last 48 hours, the
  newest of each day for 30 days and of each month for 12 months, and always
  the 48 newest, whatever their dates (so one run with the clock far ahead
  can't delete the rest). It deletes only files named like backups and its
  own unfinished files.
- run() is one scheduled backup: snapshot, a copy into each extra folder
  (a USB drive, a synced folder), then prune in each.
- restore() puts a backup in place of the database. The server must be
  stopped: it holds server_lock() while it runs, and restore() takes it. The
  database it replaces is moved aside (with its -wal and -shm), never deleted.
  Everyone logs in again, and no setup code given out before the backup works.
- Backups, their copies and a restored database can be read by their owner
  only (0600), whatever the umask.
"""
import gzip
import os
import re
import shutil
import sqlite3
import tempfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ledger as L
from . import orders as O

SUFFIX = ".db.gz"
STAMP = "%Y%m%dT%H%M%SZ"            # no ':' so the name works on Windows too
NAME = re.compile(r"^(?P<stem>.+)-(?P<when>\d{8}T\d{6}Z)(?:-(?P<n>\d+))?\.db\.gz$")
KEEP_ALL_HOURS = 48
KEEP_DAILY_DAYS = 30
KEEP_MONTHLY_MONTHS = 12
LEFTOVER_HOURS = 24                 # unfinished files older than this are removed
TMP_PREFIX = ".tmp-wallet-backup-"  # snapshot's own temporary folders
CHUNK = 1024 * 1024


class BackupError(Exception):
    pass


def _utc(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(when):
    return when.isoformat(timespec="seconds")


def _fsync_dir(path):
    """Make a rename in `path` survive a power cut (not possible on Windows,
    where the rename itself is enough)."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open(path):
    """A plain connection to a database file, without the app's schema script,
    so checking a copy changes nothing in it."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _examine(path):
    """Check a database file: it must be sound, else BackupError. Returns
    {ok, schema_version, problems, integrity}; problems are the ledger and
    order checks' findings (empty = healthy)."""
    try:
        conn = _open(path)
        try:
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise BackupError("the database is damaged: " + "; ".join(integrity[:5]))
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise BackupError("the database is damaged: rows point at missing ones")
            version = L.schema_version(conn)
            if version > L.SCHEMA_VERSION:
                return {"ok": False, "schema_version": version, "integrity": "ok",
                        "problems": [f"made by a newer version of the app (schema "
                                     f"{version}); not checked further"]}
            problems = O.verify(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise BackupError(f"not a usable wallet database: {e}") from None
    return {"ok": not problems, "schema_version": version, "integrity": "ok",
            "problems": problems}


def _copy_database(db_path, dest):
    """A consistent copy of the live database, as one self-contained file."""
    source = _open(db_path)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)       # all pages in one step: one moment's state
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
    finally:
        source.close()


def _write_file(dest, write, *, new_name=None):
    """Write `dest` through a '.partial' file of its own (two writers never
    share one), readable by its owner only and synced to disk, then put in
    place, so `dest` is either whole or not there. With `new_name`, a finished
    file is never replaced: if `dest` was taken meanwhile, new_name() gives
    the next free one. Returns where the file went."""
    fd, partial = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".",
                                   suffix=".partial")
    partial = Path(partial)
    try:
        with open(fd, "wb") as out:
            write(out)
            out.flush()
            os.fsync(out.fileno())
        if new_name is None:
            os.replace(partial, dest)
        else:
            dest = _publish(partial, dest, new_name)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    _fsync_dir(dest.parent)
    return dest


def _publish(partial, dest, new_name):
    """Give the finished `partial` the name `dest`, or the next free one."""
    while True:
        try:
            os.link(partial, dest)      # unlike a rename, refuses a name that is taken
        except FileExistsError:
            dest = new_name()
        except OSError:                 # no hard links on this drive (FAT, exFAT)
            if dest.exists():
                dest = new_name()
            os.replace(partial, dest)
            return dest
        else:
            partial.unlink()
            return dest


def _compress(source, out, inner_name):
    with open(source, "rb") as f, gzip.GzipFile(inner_name, "wb", fileobj=out) as gz:
        shutil.copyfileobj(f, gz, CHUNK)


def _damaged(e):
    return BackupError(f"the backup file is damaged or not a backup: {e}")


def _decompress(backup_path, dest):
    """Unpack a backup into `dest`, a new file only its owner can read. Only an
    error reading the backup means it is damaged; one writing `dest` (a full
    disk) is reported as that."""
    try:
        gz = gzip.open(backup_path, "rb")
    except FileNotFoundError:
        raise BackupError(f"no backup file at {backup_path}") from None
    except OSError as e:
        raise _damaged(e) from None
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    with gz:
        try:
            with open(os.open(dest, flags, 0o600), "wb") as out:
                while True:
                    try:        # gzip.BadGzipFile is an OSError
                        chunk = gz.read(CHUNK)
                    except (OSError, EOFError, zlib.error) as e:
                        raise _damaged(e) from None
                    if not chunk:
                        break
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        except OSError as e:
            raise BackupError(f"could not unpack the backup into {Path(dest).parent}: "
                              f"{e}") from None


def _free_name(folder, stem, when):
    name = f"{stem}-{when.strftime(STAMP)}"
    candidate, n = name + SUFFIX, 1
    while (folder / candidate).exists():
        n += 1
        candidate = f"{name}-{n}{SUFFIX}"
    return candidate


def snapshot(db_path, backup_dir, *, now=None):
    """Take a backup of `db_path` into `backup_dir`. A copy that isn't sound is
    refused (BackupError) and nothing is kept. One with ledger problems is kept,
    as it shows how things were, and its problems are returned for the caller
    to report: {path, name, size, created_at, schema_version, problems}."""
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    if not db_path.is_file():   # never make an empty database by connecting
        raise BackupError(f"no database at {db_path}")
    now = _utc(now)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The uncompressed copy is made next to the backups: the system's temp
    # folder may be too small for it, and this one is on the right disk.
    with tempfile.TemporaryDirectory(prefix=TMP_PREFIX, dir=backup_dir) as tmp:
        copy = Path(tmp) / db_path.name
        try:
            _copy_database(db_path, copy)
        except sqlite3.DatabaseError as e:
            raise BackupError(f"could not copy the database: {e}") from None
        report = _examine(copy)

        def free():     # another backup in the same second may take a name first
            return backup_dir / _free_name(backup_dir, db_path.stem, now)
        dest = _write_file(free(), lambda out: _compress(copy, out, db_path.name),
                           new_name=free)
        name = dest.name
    return {"path": str(dest), "name": name, "size": dest.stat().st_size,
            "created_at": _iso(now), "schema_version": report["schema_version"],
            "problems": report["problems"]}


def _backups(folder):
    """(time, number, path) of every backup in `folder`, newest first. A name
    with an impossible time (wallet-20261318T...) is not a backup."""
    found = []
    for path in Path(folder).iterdir():
        match = NAME.match(path.name)
        if match and path.is_file():
            try:
                when = datetime.strptime(match["when"], STAMP)
            except ValueError:
                continue
            when = when.replace(tzinfo=timezone.utc)
            found.append((when, int(match["n"] or 1), match["stem"], path))
    found.sort(key=lambda b: (b[0], b[1]), reverse=True)
    return found


def list_backups(backup_dir):
    """The backups in a folder, newest first: {path, name, created_at, size}."""
    found = []
    for when, _, _, path in _backups(backup_dir):
        try:
            size = path.stat().st_size
        except FileNotFoundError:   # pruned meanwhile
            continue
        found.append({"path": str(path), "name": path.name, "created_at": _iso(when),
                      "size": size})
    return found


def _keep(backups, now):
    """Which of one database's backups (newest first) the rules keep."""
    keep, days, months = set(), set(), set()
    this_month = now.year * 12 + now.month
    for when, _, _, path in backups:
        day, month = when.date(), when.year * 12 + when.month
        if now - when <= timedelta(hours=KEEP_ALL_HOURS):
            keep.add(path)
        if (now.date() - day).days < KEEP_DAILY_DAYS and day not in days:
            days.add(day)
            keep.add(path)
        if this_month - month < KEEP_MONTHLY_MONTHS and month not in months:
            months.add(month)
            keep.add(path)
    # Always the 48 newest, however old: a backup made with the clock far
    # ahead would otherwise put every other one outside the rules.
    keep.update(path for _, _, _, path in backups[:KEEP_ALL_HOURS])
    return keep


def _leftover(path):
    """Whether `path` was left by an interrupted backup: '<backup
    name>.<random>.partial' (or the older '<backup name>.partial'), or
    snapshot's temporary folder. Never another program's file or folder."""
    if path.name.startswith(TMP_PREFIX):
        return path.is_dir()
    unfinished = path.name.removesuffix(".partial")
    if unfinished == path.name or not path.is_file():
        return False
    return bool(NAME.match(unfinished) or NAME.match(unfinished.rpartition(".")[0]))


def prune(backup_dir, *, now=None):
    """Delete the backups the rules don't keep (see the top of this file), and
    unfinished files left by an interrupted backup. Returns the deleted paths."""
    now, folder = _utc(now), Path(backup_dir)
    by_db = {}
    for backup in _backups(folder):
        by_db.setdefault(backup[2], []).append(backup)
    deleted = []
    for backups in by_db.values():
        keep = _keep(backups, now)
        for _, _, _, path in backups:
            if path not in keep:
                path.unlink(missing_ok=True)
                deleted.append(str(path))
    cutoff = now.timestamp() - LEFTOVER_HOURS * 3600
    for path in folder.iterdir():
        if _leftover(path) and path.stat().st_mtime < cutoff:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            deleted.append(str(path))
    return deleted


def copy_backup(backup_path, folder):
    """Copy a finished backup into another folder (whole or not at all). The
    folder is never made here: one that is missing is most likely on a drive
    that isn't plugged in, and making it would put the copy on this disk."""
    source, folder = Path(backup_path), Path(folder)
    if not folder.is_dir():
        raise BackupError(f"{folder} is not there (is the drive plugged in?)")
    dest = folder / source.name

    def write(out):
        with open(source, "rb") as f:
            shutil.copyfileobj(f, out, CHUNK)
    _write_file(dest, write)
    return str(dest)


def run(db_path, backup_dir, copy_to=(), *, now=None):
    """One scheduled backup: snapshot, copy into each `copy_to` folder, then
    prune each folder that got it. Pruning happens only after a good backup,
    so failing backups never thin out the good ones. A snapshot that fails
    raises BackupError. A copy or a prune that fails is reported in
    copy_errors or prune_errors, and the backup still counts as made.
    Returns the snapshot's dict plus copies, copy_errors, prune_errors and
    pruned."""
    now = _utc(now)
    result = snapshot(db_path, backup_dir, now=now)
    result.update(copies=[], copy_errors=[], prune_errors=[], pruned=[])
    folders = [backup_dir]
    for folder in copy_to:
        try:
            result["copies"].append(copy_backup(result["path"], folder))
            folders.append(folder)
        except BackupError as e:            # says which folder itself
            result["copy_errors"].append(str(e))
        except OSError as e:
            result["copy_errors"].append(f"{folder}: {e}")
    for folder in folders:
        try:
            result["pruned"] += prune(folder, now=now)
        except (OSError, ValueError) as e:
            result["prune_errors"].append(f"{folder}: could not remove old backups: {e}")
    return result


def check_file(backup_path):
    """Check a backup file without restoring it: {ok, schema_version, problems,
    integrity}. BackupError if it is damaged or not a backup."""
    with tempfile.TemporaryDirectory(prefix="wallet-check-") as tmp:
        copy = Path(tmp) / "check.db"
        _decompress(backup_path, copy)
        return _examine(copy)


def check_database(db_path):
    """Check the live database in place, like check_file (it reads only)."""
    if not Path(db_path).is_file():
        raise BackupError(f"no database at {db_path}")
    return _examine(db_path)


def _aside(path, stamp):
    """A free name to move a replaced database file to."""
    base, n = f"{path}.before-restore-{stamp}", 1
    candidate = base
    while any(Path(candidate + s).exists() for s in ("", "-wal", "-shm")):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def restore(backup_path, db_path, *, now=None):
    """Put the backup in place of `db_path`. Refused while a server holding
    server_lock() (app.serve) runs on it, if the backup is damaged, or if it
    is from a newer version of the app. Every session open in the backup is
    ended and every setup code nobody had used is voided, so everyone logs in
    again: a session or code ended after the backup (a log-out, a block, a new
    code) doesn't come back with it. The database it replaces, with its -wal
    and -shm files, is moved aside to <name>.before-restore-<time>, never
    deleted. Returns {restored_from, db_path, moved_aside, problems,
    sessions_ended, codes_voided}."""
    db_path, now = Path(db_path), _utc(now)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with server_lock(db_path):
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=db_path.parent) as tmp:
            copy = Path(tmp) / db_path.name
            _decompress(backup_path, copy)
            report = _examine(copy)
            if report["schema_version"] > L.SCHEMA_VERSION:
                raise BackupError("the backup is from a newer version of the app; "
                                  "update the app first")
            ended, voided = _end_logins(copy, now)
            moved = _move_aside(db_path, now)
            os.replace(copy, db_path)
            _fsync_dir(db_path.parent)
    return {"restored_from": str(backup_path), "db_path": str(db_path),
            "moved_aside": moved, "problems": report["problems"],
            "sessions_ended": ended, "codes_voided": voided}


def _end_logins(path, now):
    """End the open sessions and void the unused setup codes in a database
    (as auth does: ended_at and used_at set to now), in one write, when it has
    the login tables. Returns how many of each."""
    ended = voided = 0
    at = now.isoformat()
    conn = _open(path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        with L._write(conn):
            if "sessions" in tables:
                ended = conn.execute("UPDATE sessions SET ended_at = ? "
                                     "WHERE ended_at IS NULL", (at,)).rowcount
            if "setup_codes" in tables:
                voided = conn.execute("UPDATE setup_codes SET used_at = ? "
                                      "WHERE used_at IS NULL", (at,)).rowcount
    finally:
        conn.close()
    return ended, voided


def _move_aside(db_path, now):
    files = [Path(f"{db_path}{s}") for s in ("", "-wal", "-shm")]
    if not any(f.exists() for f in files):
        return []
    # Fold the -wal into the database first where it can be read, so the file
    # moved aside holds everything on its own.
    if files[0].exists():
        try:
            conn = _open(db_path)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            pass                # a damaged database is moved aside as it is
    base, moved = _aside(db_path, now.strftime(STAMP)), []
    # -wal and -shm first: a -wal left next to a new database file could be
    # read into it.
    for f, suffix in ((files[1], "-wal"), (files[2], "-shm"), (files[0], "")):
        if f.exists():
            os.replace(f, base + suffix)
            moved.append(base + suffix)
    return moved


@contextmanager
def server_lock(db_path):
    """Held by the server for as long as it runs, and by restore(): an OS lock on
    <db_path>.lock, so a second server can't start on the same database (the
    request limits are kept per process) and a restore can't happen under a
    running one. The OS lets go of it if the process dies."""
    lock_path = Path(f"{db_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
    try:
        try:
            _lock(f)
        except OSError:
            raise BackupError("another server is using this database "
                              f"({lock_path} is locked)") from None
        try:
            yield
        finally:
            _unlock(f)
    finally:
        f.close()


if os.name == "nt":
    import msvcrt

    def _lock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
