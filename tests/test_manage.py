import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app import auth as A
from app import backup as B
from app import ledger as L
from app import manage as M

from .test_backup import make_wallet, unbalance

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


class ManageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "wallet.db"
        self.backups = self.dir / "backups"

    def run_cli(self, *argv, passwords=(), typed=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch("getpass.getpass", side_effect=list(passwords)), \
                mock.patch("builtins.input", return_value=typed):
            status = M.main([str(a) for a in argv])
        return status, out.getvalue(), err.getvalue()

    def admins(self):
        conn = A.connect(self.db)
        try:
            return [r["name"] for r in conn.execute(
                "SELECT name FROM users WHERE role = 'admin'")]
        finally:
            conn.close()

    def test_first_admin(self):
        secret = "a long admin secret"
        status, out, _ = self.run_cli("first-admin", "--db", self.db, "--name", "Huda",
                                      "--phone", "091 000 0000",
                                      passwords=[secret, secret])
        self.assertEqual(status, 0)
        self.assertIn("first admin created", out)
        conn = A.connect(self.db)
        self.addCleanup(conn.close)
        self.assertTrue(A.login(conn, "0910000000", secret, client="test"))

        status, _, err = self.run_cli("first-admin", "--db", self.db, "--name", "Other",
                                      "--phone", "0920000000", passwords=[secret, secret])
        self.assertEqual((status, self.admins()), (1, ["Huda"]))
        self.assertIn("already has an admin", err)

    def test_first_admin_refusals_are_one_plain_line(self):
        for passwords, said in ((["a long admin secret", "another one entirely"], "not the same"),
                                (["1234567890", "1234567890"], "error:")):
            status, _, err = self.run_cli("first-admin", "--db", self.db, "--name", "Huda",
                                          "--phone", "0910000000", passwords=passwords)
            self.assertEqual(status, 1)
            self.assertIn(said, err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(self.admins(), [])

    def test_backup_list_and_check_backup(self):
        make_wallet(self.db)[0].close()
        usb = self.dir / "usb"
        status, out, _ = self.run_cli("backup", "--db", self.db, "--dir", self.backups,
                                      "--copy-to", usb)
        self.assertEqual(status, 0)
        [backup] = B.list_backups(self.backups)
        self.assertTrue((usb / backup["name"]).is_file())
        self.assertIn(backup["name"], out)

        status, out, _ = self.run_cli("list", "--dir", self.backups)
        self.assertEqual(status, 0)
        self.assertIn(backup["name"], out)
        self.assertEqual(self.run_cli("check-backup", backup["path"])[0], 0)
        damaged = self.dir / "damaged.db.gz"
        damaged.write_bytes(Path(backup["path"]).read_bytes()[:50])
        status, _, err = self.run_cli("check-backup", damaged)
        self.assertEqual(status, 1)
        self.assertIn("damaged", err)

    def test_backup_exit_status(self):
        status, _, err = self.run_cli("backup", "--db", self.db, "--dir", self.backups)
        self.assertEqual(status, 1)                 # no database
        self.assertIn("no database", err)
        self.assertFalse(self.db.exists())

        conn = make_wallet(self.db)[0]
        blocked = self.dir / "file"
        blocked.write_bytes(b"")
        status, _, err = self.run_cli("backup", "--db", self.db, "--dir", self.backups,
                                      "--copy-to", blocked)
        self.assertEqual(status, 1)                 # made, but the copy failed
        self.assertIn("copying it failed", err)
        self.assertEqual(len(B.list_backups(self.backups)), 1)

        unbalance(conn)
        conn.close()
        status, _, err = self.run_cli("backup", "--db", self.db, "--dir", self.backups)
        self.assertEqual(status, 2)                 # made, with ledger problems
        self.assertIn("PROBLEM:", err)
        self.assertEqual(len(B.list_backups(self.backups)), 2)

    def test_check(self):
        conn = make_wallet(self.db)[0]
        self.addCleanup(conn.close)
        self.assertEqual(self.run_cli("check", "--db", self.db)[0], 0)
        unbalance(conn)
        status, _, err = self.run_cli("check", "--db", self.db)
        self.assertEqual(status, 2)
        self.assertIn("PROBLEM:", err)
        self.assertEqual(self.run_cli("check", "--db", self.dir / "none.db")[0], 1)

    def test_restore_asks_first(self):
        conn, admin, _ = make_wallet(self.db)
        backup = B.snapshot(self.db, self.backups, now=NOW)["path"]
        L.create_user(conn, "customer", "Sam", "0922222222", admin_id=admin)
        conn.close()

        status, _, err = self.run_cli("restore", backup, "--db", self.db, typed="no")
        self.assertEqual(status, 1)
        self.assertIn("not restored", err)
        status, out, _ = self.run_cli("restore", backup, "--db", self.db, typed="restore")
        self.assertEqual(status, 0)
        self.assertIn("kept the replaced file as", out)
        conn = A.connect(self.db)
        self.addCleanup(conn.close)
        self.assertIsNone(L.find_user_by_phone(conn, "0922222222"))

    def test_restore_refused_while_the_server_runs(self):
        make_wallet(self.db)[0].close()
        backup = B.snapshot(self.db, self.backups, now=NOW)["path"]
        with B.server_lock(self.db):
            status, _, err = self.run_cli("restore", backup, "--db", self.db, "--yes")
        self.assertEqual(status, 1)
        self.assertIn("another server", err)


if __name__ == "__main__":
    unittest.main()
