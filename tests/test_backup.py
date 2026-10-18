import gzip
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import auth as A
from app import backup as B
from app import ledger as L

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
        expected = {self.name(NOW - timedelta(hours=6 * i)) for i in range(9)}  # 48 hours
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

    def test_always_keeps_the_newest_however_old(self):
        old = [self.fake(NOW - timedelta(days=800 + i)) for i in range(3)]
        B.prune(self.backups, now=NOW)
        self.assertEqual(names(self.backups), [old[0]])

    def test_each_database_keeps_its_own_newest(self):
        self.fake(NOW - timedelta(days=900), stem="test")
        self.fake(NOW - timedelta(days=901), stem="test")
        self.fake(NOW)
        B.prune(self.backups, now=NOW)
        self.assertEqual(len(names(self.backups)), 2)

    def test_removes_old_unfinished_files_only(self):
        self.backups.mkdir()
        old, fresh = self.backups / "a.db.gz.partial", self.backups / "b.db.gz.partial"
        old_dir = self.backups / ".tmp-abc"
        old_dir.mkdir()
        (old_dir / "wallet.db").write_bytes(b"x")
        for p in (old, fresh):
            p.write_bytes(b"x")
        day_ago = NOW.timestamp() - 25 * 3600
        for p in (old, old_dir):
            os.utime(p, (day_ago, day_ago))
        os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
        B.prune(self.backups, now=NOW)
        self.assertEqual(names(self.backups), ["b.db.gz.partial"])


class RunTests(Folder):
    def test_copies_into_each_folder_and_reports_one_that_fails(self):
        make_wallet(self.db)[0].close()
        usb = self.dir / "usb"
        blocked = self.dir / "not-a-folder"
        blocked.write_bytes(b"")
        result = B.run(self.db, self.backups, [usb, blocked], now=NOW)
        self.assertEqual(result["copies"], [str(usb / result["name"])])
        self.assertEqual(len(result["copy_errors"]), 1)
        self.assertIn("not-a-folder", result["copy_errors"][0])
        self.assertEqual((usb / result["name"]).read_bytes(),
                         Path(result["path"]).read_bytes())

    def test_old_backups_are_pruned_only_after_a_good_one(self):
        self.backups.mkdir()
        old = [self.backups / f"wallet-{(NOW - timedelta(days=d)).strftime(B.STAMP)}.db.gz"
               for d in (400, 401)]
        for p in old:
            p.write_bytes(b"")
        with self.assertRaises(B.BackupError):      # no database: no backup
            B.run(self.db, self.backups, now=NOW)
        self.assertTrue(all(p.exists() for p in old))
        make_wallet(self.db)[0].close()
        result = B.run(self.db, self.backups, now=NOW)
        self.assertEqual(sorted(result["pruned"]), sorted(str(p) for p in old))


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
