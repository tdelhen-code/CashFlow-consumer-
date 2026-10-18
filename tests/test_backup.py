import gzip
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import auth as A
from app import backup as B
from app import ledger as L

try:
    import resource
except ImportError:     # Windows
    resource = None

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def make_wallet(path):
    """A small wallet database: an admin, and Ali with 25 LYD."""
    conn = A.connect(path)
    admin = A.create_first_admin(conn, "Admin", "0910000000", "a long admin secret")
    ali = L.create_user(conn, "customer", "Ali", "0911111111", admin_id=admin)
    L.deposit(conn, ali, L.parse_lyd("25"), admin_id=admin, into="BANK")
    return conn, admin, ali


def unbalance(conn):
    """A transaction whose entries don't add up, for the checks to find."""
    with L._write(conn):
        txn = conn.execute("INSERT INTO transactions(kind) VALUES ('deposit')").lastrowid
        conn.execute("INSERT INTO entries(txn_id, account_id, amount) "
                     "SELECT ?, id, -5 FROM accounts WHERE code = 'BANK'", (txn,))


def open_backup(backup_path, folder):
    """Unpack a backup on its own into `folder` and open it."""
    dest = Path(folder) / "unpacked.db"
    with gzip.open(backup_path) as gz, open(dest, "wb") as out:
        shutil.copyfileobj(gz, out)
    conn = sqlite3.connect(dest)
    conn.row_factory = sqlite3.Row
    return conn, dest


def names(folder):
    return sorted(p.name for p in Path(folder).iterdir())


class Folder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "wallet.db"
        self.backups = self.dir / "backups"


class SnapshotTests(Folder):
    def test_a_backup_while_the_database_is_in_use_holds_what_was_committed(self):
        conn, admin, ali = make_wallet(self.db)
        self.addCleanup(conn.close)
        self.assertGreater(os.path.getsize(f"{self.db}-wal"), 0)   # still in the -wal
        other = A.connect(self.db)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")       # a write under way, not committed
        other.execute("INSERT INTO accounts(kind, code) VALUES ('holding', 'TILL_HALF')")

        result = B.snapshot(self.db, self.backups, now=NOW)
        other.execute("ROLLBACK")

        self.assertEqual(result["problems"], [])
        self.assertEqual(result["schema_version"], L.SCHEMA_VERSION)
        unpack = self.dir / "unpack"
        unpack.mkdir()
        copy, path = open_backup(result["path"], unpack)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(L.balance(copy, ali), L.parse_lyd("25"))
        self.assertIsNone(copy.execute(
            "SELECT 1 FROM accounts WHERE code = 'TILL_HALF'").fetchone())
        self.assertFalse(Path(f"{path}-wal").exists())

    def test_named_for_the_time_in_utc_and_never_overwritten(self):
        make_wallet(self.db)[0].close()
        local = NOW.astimezone(timezone(timedelta(hours=2)))
        first = B.snapshot(self.db, self.backups, now=local)
        second = B.snapshot(self.db, self.backups, now=local)
        self.assertEqual(first["name"], "wallet-20261018T120000Z.db.gz")
        self.assertEqual(second["name"], "wallet-20261018T120000Z-2.db.gz")
        self.assertEqual(first["created_at"], "2026-10-18T12:00:00+00:00")
        self.assertEqual(names(self.backups), sorted([first["name"], second["name"]]))
        self.assertEqual([b["name"] for b in B.list_backups(self.backups)],
                         [second["name"], first["name"]])

    def test_two_backups_in_the_same_second_get_a_whole_file_each(self):
        make_wallet(self.db)[0].close()
        both_writing = threading.Barrier(2, timeout=10)
        compress, results, errors = B._compress, [], []

        def compress_together(*args):       # by now each one has chosen its name
            both_writing.wait()
            compress(*args)

        def backup():
            try:
                results.append(B.snapshot(self.db, self.backups, now=NOW))
            except Exception as e:
                errors.append(e)
        with mock.patch.object(B, "_compress", compress_together):
            threads = [threading.Thread(target=backup) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        made = sorted(r["name"] for r in results)
        self.assertEqual(made, ["wallet-20261018T120000Z-2.db.gz",
                                "wallet-20261018T120000Z.db.gz"])
        self.assertEqual(names(self.backups), made)      # no unfinished file left
        for r in results:
            self.assertTrue(B.check_file(r["path"])["ok"])

    def test_without_hard_links_a_name_taken_meanwhile_is_still_not_replaced(self):
        # A FAT or exFAT drive can't make hard links: a rename is used instead.
        make_wallet(self.db)[0].close()
        first = B.snapshot(self.db, self.backups, now=NOW)
        stale, free_name = iter([first["name"]]), B._free_name

        def chosen_before_first_was_in_place(*args):
            return next(stale, None) or free_name(*args)
        no_links = PermissionError(1, "Operation not permitted")
        with mock.patch.object(B, "_free_name", chosen_before_first_was_in_place), \
                mock.patch.object(B.os, "link", side_effect=no_links):
            second = B.snapshot(self.db, self.backups, now=NOW)
        self.assertEqual(second["name"], "wallet-20261018T120000Z-2.db.gz")
        self.assertEqual(names(self.backups), sorted([first["name"], second["name"]]))
        self.assertTrue(B.check_file(first["path"])["ok"])

    def test_a_backup_with_ledger_problems_is_kept_and_reported(self):
        conn = make_wallet(self.db)[0]
        unbalance(conn)
        conn.close()
        result = B.snapshot(self.db, self.backups, now=NOW)
        self.assertTrue(result["problems"])
        self.assertTrue(Path(result["path"]).is_file())

    def test_a_damaged_copy_is_refused_and_nothing_is_left(self):
        make_wallet(self.db)[0].close()

        def garbage(db_path, dest):
            Path(dest).write_bytes(b"SQLite format 3\x00" + b"\x01" * 4000)
        with mock.patch.object(B, "_copy_database", garbage):
            with self.assertRaises(B.BackupError):
                B.snapshot(self.db, self.backups, now=NOW)
        self.assertEqual(names(self.backups), [])

    def test_a_missing_database_is_refused_not_created(self):
        with self.assertRaises(B.BackupError):
            B.snapshot(self.db, self.backups, now=NOW)
        self.assertFalse(self.db.exists())

    def test_a_failed_write_leaves_no_partial_file(self):
        make_wallet(self.db)[0].close()
        with mock.patch.object(B, "_compress", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                B.snapshot(self.db, self.backups, now=NOW)
        self.assertEqual(names(self.backups), [])


class PruneTests(Folder):
    def name(self, when, stem="wallet"):
        return f"{stem}-{when.strftime(B.STAMP)}.db.gz"

    def fake(self, when, stem="wallet"):
        self.backups.mkdir(exist_ok=True)
        (self.backups / self.name(when, stem)).write_bytes(b"")
        return self.name(when, stem)

    def test_keeps_two_days_then_daily_for_30_days_then_monthly_for_a_year(self):
        made = [self.fake(NOW - timedelta(hours=6 * i)) for i in range(4 * 400)]
        others = ["notes.txt", "wallet.db.gz", "wallet-2026.db.gz"]
        for name in others:
            (self.backups / name).write_bytes(b"")
        deleted = B.prune(self.backups, now=NOW)

        kept = set(names(self.backups)) - set(others)
        self.assertEqual(len(deleted), len(made) - len(kept))
        # The 48 newest (12 days here), which hold all of the last 48 hours.
        expected = {self.name(NOW - timedelta(hours=6 * i))
                    for i in range(B.KEEP_ALL_HOURS)}
        for day in range(30):           # the newest of each day: 18:00 (today 12:00)
            when = (NOW - timedelta(days=day)).replace(hour=18 if day else 12)
            expected.add(self.name(when))
        for back in range(12):          # the newest of each month
            year, month = divmod(NOW.year * 12 + NOW.month - 1 - back, 12)
            first_of_next = datetime(year + (month == 11), (month + 1) % 12 + 1, 1,
                                     tzinfo=timezone.utc)
            expected.add(self.name(min(first_of_next - timedelta(hours=6), NOW)))
        self.assertEqual(kept, expected)
        self.assertEqual(B.prune(self.backups, now=NOW), [])        # nothing more
        for name in others:
            self.assertTrue((self.backups / name).exists())

    def test_keeps_every_backup_of_the_last_48_hours_however_many(self):
        made = [self.fake(NOW - timedelta(minutes=15 * i)) for i in range(4 * 50)]
        B.prune(self.backups, now=NOW)
        self.assertEqual(set(names(self.backups)), set(made[:4 * 48 + 1]))

    def test_always_keeps_the_48_newest_however_old(self):
        old = [self.fake(NOW - timedelta(days=800 + i))
               for i in range(B.KEEP_ALL_HOURS + 3)]
        B.prune(self.backups, now=NOW)
        self.assertEqual(names(self.backups), sorted(old[:B.KEEP_ALL_HOURS]))

    def test_one_run_with_the_clock_a_year_ahead_keeps_the_48_newest(self):
        make_wallet(self.db)[0].close()
        made = [self.fake(NOW - timedelta(hours=i)) for i in range(60)]
        ahead = B.run(self.db, self.backups, now=NOW + timedelta(days=365))
        self.assertEqual(set(names(self.backups)),
                         {ahead["name"], *made[:B.KEEP_ALL_HOURS - 1]})

    def test_each_database_keeps_its_own_newest(self):
        for i in range(B.KEEP_ALL_HOURS + 2):
            self.fake(NOW - timedelta(days=900 + i), stem="test")
        self.fake(NOW)
        B.prune(self.backups, now=NOW)
        self.assertEqual(len(names(self.backups)), B.KEEP_ALL_HOURS + 1)

    def test_removes_its_own_old_unfinished_files_only(self):
        self.backups.mkdir()
        ours = ["wallet-20261017T110000Z.db.gz.k2j4h5g6.partial",
                "wallet-20261017T100000Z.db.gz.partial",    # as named before
                B.TMP_PREFIX + "abc"]
        others = ["contract.pdf.partial", "notes.db.gz.partial", ".tmp-photos"]
        fresh = "wallet-20261018T115900Z.db.gz.q8w7e6r5.partial"
        day_ago = NOW.timestamp() - 25 * 3600
        for name in ours + others + [fresh]:
            path = self.backups / name
            if name.startswith("."):
                path.mkdir()
                (path / "wallet.db").write_bytes(b"x")
            else:
                path.write_bytes(b"x")
            when = NOW.timestamp() if name == fresh else day_ago
            os.utime(path, (when, when))
        deleted = B.prune(self.backups, now=NOW)
        self.assertEqual(sorted(deleted), sorted(str(self.backups / n) for n in ours))
        self.assertEqual(names(self.backups), sorted(others + [fresh]))

    def test_goes_on_past_a_file_it_cannot_remove(self):
        old = [self.backups / self.fake(NOW - timedelta(days=800 + i))
               for i in range(B.KEEP_ALL_HOURS + 3)]
        stuck = old[B.KEEP_ALL_HOURS]               # the first one it removes
        leftover = self.backups / "wallet-20261017T110000Z.db.gz.k2j4h5g6.partial"
        leftover.write_bytes(b"x")
        day_ago = NOW.timestamp() - 25 * 3600
        os.utime(leftover, (day_ago, day_ago))
        unlink = Path.unlink

        def read_only(path, missing_ok=False):
            if path == stuck:
                raise PermissionError(13, "Permission denied", str(path))
            unlink(path, missing_ok=missing_ok)
        errors = []
        with mock.patch.object(Path, "unlink", read_only):
            deleted = B.prune(self.backups, now=NOW, errors=errors)
            self.assertEqual(sorted(deleted), sorted(
                str(p) for p in old[B.KEEP_ALL_HOURS + 1:] + [leftover]))
            self.assertEqual([e.filename for e in errors], [str(stuck)])
            self.assertTrue(stuck.exists())
            # Without a list for them, the error is raised, after the rest.
            older = self.backups / self.fake(NOW - timedelta(days=900))
            with self.assertRaises(PermissionError):
                B.prune(self.backups, now=NOW)
        self.assertFalse(older.exists())


class RunTests(Folder):
    def old_backups(self, count):
        self.backups.mkdir()
        old = [self.backups / f"wallet-{(NOW - timedelta(days=400 + i)).strftime(B.STAMP)}"
               ".db.gz" for i in range(count)]
        for p in old:
            p.write_bytes(b"")
        return old

    def test_copies_into_each_folder_and_reports_one_that_fails(self):
        make_wallet(self.db)[0].close()
        usb = self.dir / "usb"
        usb.mkdir()
        blocked = self.dir / "not-a-folder"
        blocked.write_bytes(b"")
        result = B.run(self.db, self.backups, [usb, blocked], now=NOW)
        self.assertEqual(result["copies"], [str(usb / result["name"])])
        self.assertEqual(len(result["copy_errors"]), 1)
        self.assertIn("not-a-folder", result["copy_errors"][0])
        self.assertEqual((usb / result["name"]).read_bytes(),
                         Path(result["path"]).read_bytes())

    def test_a_copy_folder_that_is_not_there_is_not_made(self):
        make_wallet(self.db)[0].close()
        (self.dir / "usb").mkdir()              # the mount point, with no drive in it
        unplugged = self.dir / "usb" / "wallet"
        result = B.run(self.db, self.backups, [unplugged], now=NOW)
        self.assertEqual(result["copies"], [])
        self.assertEqual(result["copy_errors"],
                         [f"{unplugged} is not there (is the drive plugged in?)"])
        self.assertFalse(unplugged.exists())

    def test_old_backups_are_pruned_only_after_a_good_one(self):
        old = self.old_backups(B.KEEP_ALL_HOURS + 1)
        with self.assertRaises(B.BackupError):      # no database: no backup
            B.run(self.db, self.backups, now=NOW)
        self.assertTrue(all(p.exists() for p in old))
        make_wallet(self.db)[0].close()
        result = B.run(self.db, self.backups, now=NOW)
        self.assertEqual(sorted(result["pruned"]),
                         sorted(str(p) for p in old[B.KEEP_ALL_HOURS - 1:]))

    def test_a_prune_that_fails_neither_fails_the_backup_nor_stops_the_copies(self):
        make_wallet(self.db)[0].close()
        old = self.old_backups(B.KEEP_ALL_HOURS)
        usb = self.dir / "usb"
        usb.mkdir()
        unlink = Path.unlink

        def held_open(path, missing_ok=False):      # as antivirus or a sync tool can
            if path.parent == self.backups and path.name.endswith(".db.gz"):
                raise PermissionError(13, "Permission denied", str(path))
            unlink(path, missing_ok=missing_ok)
        with mock.patch.object(Path, "unlink", held_open):
            result = B.run(self.db, self.backups, [usb], now=NOW)
        self.assertEqual(result["copies"], [str(usb / result["name"])])
        self.assertEqual(result["copy_errors"], [])
        self.assertEqual(len(result["prune_errors"]), 1)
        self.assertTrue(result["prune_errors"][0].startswith(
            f"{self.backups}: could not remove old backups: "))
        self.assertTrue(old[-1].exists())

    def test_old_backups_that_cannot_be_removed_do_not_keep_the_others(self):
        make_wallet(self.db)[0].close()
        old = self.old_backups(B.KEEP_ALL_HOURS + 2)   # with the new one, 3 too many
        stuck = old[B.KEEP_ALL_HOURS - 1:B.KEEP_ALL_HOURS + 1]
        unlink = Path.unlink

        def read_only(path, missing_ok=False):
            if path in stuck:
                raise PermissionError(13, "Permission denied", str(path))
            unlink(path, missing_ok=missing_ok)
        with mock.patch.object(Path, "unlink", read_only):
            result = B.run(self.db, self.backups, now=NOW)
        self.assertEqual(result["pruned"], [str(old[-1])])     # shown as removed
        self.assertFalse(old[-1].exists())
        self.assertEqual(result["prune_errors"], [
            f"{self.backups}: could not remove old backups: [Errno 13] Permission "
            f"denied: '{stuck[0]}' (and 1 more)"])

    def test_a_name_with_an_impossible_time_is_not_a_backup(self):
        make_wallet(self.db)[0].close()
        self.backups.mkdir()
        typo = self.backups / "wallet-20261318T052600Z.db.gz"
        typo.write_bytes(b"")
        result = B.run(self.db, self.backups, now=NOW)
        self.assertEqual(result["prune_errors"], [])
        self.assertEqual([b["name"] for b in B.list_backups(self.backups)],
                         [result["name"]])
        self.assertTrue(typo.exists())


class CheckTests(Folder):
    def test_a_good_backup_and_damaged_ones(self):
        make_wallet(self.db)[0].close()
        good = B.snapshot(self.db, self.backups, now=NOW)["path"]
        self.assertEqual(B.check_file(good)["problems"], [])
        self.assertTrue(B.check_file(good)["ok"])

        data = Path(good).read_bytes()
        cut = self.dir / "cut.db.gz"
        cut.write_bytes(data[: len(data) // 2])
        flipped = self.dir / "flipped.db.gz"
        flipped.write_bytes(data[:-12] + bytes(b ^ 0xFF for b in data[-12:-8]) + data[-8:])
        text = self.dir / "text.db.gz"
        text.write_bytes(b"hello")
        not_db = self.dir / "notdb.db.gz"
        not_db.write_bytes(gzip.compress(b"not a database at all" * 100))
        for bad in (cut, flipped, text, not_db, self.dir / "missing.db.gz"):
            with self.assertRaises(B.BackupError, msg=bad.name):
                B.check_file(bad)

    @unittest.skipIf(resource is None, "needs a file size limit")
    def test_a_full_disk_while_unpacking_is_not_called_a_damaged_backup(self):
        make_wallet(self.db)[0].close()
        good = B.snapshot(self.db, self.backups, now=NOW)["path"]
        before = self.db.read_bytes()
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024, hard))    # like a full disk
        try:
            for unpack in (B.check_file, lambda path: B.restore(path, self.db, now=NOW)):
                with self.assertRaisesRegex(B.BackupError, "could not unpack the backup"):
                    unpack(good)
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(B.check_file(good)["ok"])
        cut = self.dir / "cut.db.gz"
        cut.write_bytes(Path(good).read_bytes()[:200])
        with self.assertRaisesRegex(B.BackupError, "damaged or not a backup"):
            B.check_file(cut)

    def test_the_live_database(self):
        conn = make_wallet(self.db)[0]
        self.addCleanup(conn.close)
        self.assertEqual(B.check_database(self.db)["problems"], [])
        unbalance(conn)
        self.assertTrue(B.check_database(self.db)["problems"])
        with self.assertRaises(B.BackupError):
            B.check_database(self.dir / "missing.db")
        self.assertFalse((self.dir / "missing.db").exists())


class RestoreTests(Folder):
    def setUp(self):
        super().setUp()
        conn, self.admin, self.ali = make_wallet(self.db)
        self.backup = B.snapshot(self.db, self.backups, now=NOW)["path"]
        self.sam = L.create_user(conn, "customer", "Sam", "0922222222",
                                 admin_id=self.admin)
        # The state a server leaves when it stops without closing: the last
        # changes only in the -wal. Copied while the connection is still open.
        self.live = self.dir / "live"
        self.live.mkdir()
        for suffix in ("", "-wal", "-shm"):
            shutil.copy(f"{self.db}{suffix}", self.live / f"wallet.db{suffix}")
        conn.close()
        self.db = self.live / "wallet.db"

    def users(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM users")}
        finally:
            conn.close()

    def test_puts_the_backup_back_and_keeps_the_old_files_aside(self):
        result = B.restore(self.backup, self.db, now=NOW)
        aside = f"{self.db}.before-restore-20261018T120000Z"
        self.assertEqual(result["moved_aside"][-1], aside)
        self.assertEqual(self.users(self.db), {"Admin", "Ali"})
        self.assertEqual(self.users(aside), {"Admin", "Ali", "Sam"})    # with its -wal
        self.assertTrue(all(p.startswith(aside) for p in result["moved_aside"]))
        self.assertFalse(Path(f"{self.db}-wal").exists())
        conn = A.connect(self.db)       # the app opens it as usual
        self.assertEqual(L.balance(conn, self.ali), L.parse_lyd("25"))
        conn.close()

        again = B.restore(self.backup, self.db, now=NOW)        # a second time
        self.assertTrue(again["moved_aside"][-1].startswith(aside + "-2"))
        self.assertTrue(Path(aside).exists())

    def test_everyone_logs_in_again_and_no_earlier_setup_code_works(self):
        conn = A.connect(self.db)
        code = A.issue_setup_code(conn, self.ali, admin_id=self.admin)
        token = A.set_password_with_code(conn, "0911111111", code, "ali's long secret")
        sams_code = A.issue_setup_code(conn, self.sam, admin_id=self.admin)  # never used
        backup = B.snapshot(self.db, self.backups, now=NOW + timedelta(hours=1))["path"]
        A.logout_everywhere(conn, token)        # Ali's phone was stolen
        conn.close()

        result = B.restore(backup, self.db, now=NOW + timedelta(hours=2))
        self.assertEqual((result["sessions_ended"], result["codes_voided"]), (1, 1))
        conn = A.connect(self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(conn, token)
        with self.assertRaises(A.LoginFailed):
            A.set_password_with_code(conn, "0922222222", sams_code, "sam's long secret")
        A.authenticate(conn, A.login(conn, "0911111111", "ali's long secret"))

    def test_into_a_new_place(self):
        target = self.dir / "new" / "wallet.db"
        result = B.restore(self.backup, target, now=NOW)
        self.assertEqual(result["moved_aside"], [])
        self.assertEqual(self.users(target), {"Admin", "Ali"})

    def test_refused_while_a_server_uses_the_database(self):
        before = self.db.read_bytes()
        with B.server_lock(self.db):
            with self.assertRaisesRegex(B.BackupError, "another server"):
                B.restore(self.backup, self.db, now=NOW)
        self.assertEqual(self.db.read_bytes(), before)
        B.restore(self.backup, self.db, now=NOW)        # free again afterwards

    def test_refuses_a_damaged_backup_or_one_from_a_newer_app(self):
        data = Path(self.backup).read_bytes()
        cut = self.dir / "cut.db.gz"
        cut.write_bytes(data[:100])
        newer_db = self.dir / "newer.db"
        conn = sqlite3.connect(newer_db)
        conn.execute(f"PRAGMA user_version = {L.SCHEMA_VERSION + 1}")
        conn.execute("CREATE TABLE users (name TEXT)")
        conn.close()
        newer = self.dir / "newer.db.gz"
        newer.write_bytes(gzip.compress(newer_db.read_bytes()))
        before = self.db.read_bytes()
        for bad in (cut, newer):
            with self.assertRaises(B.BackupError, msg=bad.name):
                B.restore(bad, self.db, now=NOW)
            self.assertEqual(self.db.read_bytes(), before)
            self.assertTrue(Path(f"{self.db}-wal").exists())
        self.assertEqual(sorted(p.name for p in self.live.iterdir()),
                         ["wallet.db", "wallet.db-shm", "wallet.db-wal", "wallet.db.lock"])


class FileModeTests(Folder):
    @unittest.skipIf(os.name == "nt", "Windows has no such file modes")
    def test_backups_copies_and_a_restored_database_are_for_their_owner_only(self):
        self.addCleanup(os.umask, os.umask(0o022))      # the usual umask outside Docker
        make_wallet(self.db)[0].close()
        usb = self.dir / "usb"
        usb.mkdir()
        result = B.run(self.db, self.backups, [usb], now=NOW)
        B.restore(result["path"], self.db, now=NOW)

        def mode(path):
            return stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode(self.backups), 0o700)
        for path in (result["path"], result["copies"][0], self.db, f"{self.db}.lock"):
            self.assertEqual(mode(path), 0o600, msg=path)


class ServerLockTests(Folder):
    def test_one_holder_at_a_time(self):
        with B.server_lock(self.db):
            with self.assertRaisesRegex(B.BackupError, "another server"):
                with B.server_lock(self.db):
                    pass
        with B.server_lock(self.db):
            pass
        self.assertTrue(Path(f"{self.db}.lock").exists())


if __name__ == "__main__":
    unittest.main()
