import io
import os
import stat
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
        # main() takes --db and --dir from WALLET_DB and WALLET_BACKUP_DIR, which
        # the shell running the tests may have set, and sets its own umask.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        for name in ("WALLET_DB", "WALLET_BACKUP_DIR"):
            os.environ.pop(name, None)
        umask = os.umask(0o077)
        os.umask(umask)
        self.addCleanup(os.umask, umask)

    def run_cli(self, *argv, passwords=(), typed=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch("getpass.getpass", side_effect=list(passwords)), \
                mock.patch("builtins.input", return_value=typed):
            try:
                status = M.main([str(a) for a in argv])
            except SystemExit as e:     # argparse refusing the command line
                status = e.code
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
        usb.mkdir()
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

    def test_backup_problems_are_shown_when_a_copy_fails_too(self):
        conn = make_wallet(self.db)[0]
        unbalance(conn)
        conn.close()
        (self.dir / "file").write_bytes(b"")
        usb = self.dir / "file" / "usb"             # not there, and can't be made
        status, _, err = self.run_cli("backup", "--db", self.db, "--dir", self.backups,
                                      "--copy-to", usb)
        self.assertEqual(status, 1)
        self.assertIn("PROBLEM: transaction 2 does not balance", err)
        self.assertIn(f"error: the backup was made, but copying it failed: {usb}", err)
        self.assertEqual(len(B.list_backups(self.backups)), 1)

    def test_backup_fails_when_old_backups_could_not_be_removed(self):
        made = {"path": str(self.backups / "wallet-20261018T120000Z.db.gz"), "size": 99,
                "problems": ["ledger total is -5, expected 0"], "copies": [],
                "copy_errors": [], "pruned": [],
                "prune_errors": [f"{self.backups}: could not remove old backups: "
                                 "[Errno 13] Permission denied"]}
        with mock.patch.object(B, "run", return_value=made):
            status, out, err = self.run_cli("backup", "--db", self.db, "--dir", self.backups)
        self.assertEqual(status, 1)
        self.assertIn(f"backup {made['path']}", out)
        self.assertIn("PROBLEM: ledger total is -5", err)
        self.assertIn(f"error: the backup was made, but {self.backups}: could not remove "
                      "old backups", err)

    def test_a_mistyped_command_exits_1(self):
        # Not argparse's 2, which would read as 'a backup was made, and the
        # ledger has problems'.
        for argv, said in ((["backup", "--dir", self.backups], "required: --db"),
                           (["check", "--db", self.db, "--verbose"], "unrecognized arguments"),
                           (["bakup", "--db", self.db], "invalid choice")):
            status, _, err = self.run_cli(*argv)
            self.assertEqual(status, 1, argv)
            self.assertIn(said, err)
        self.assertFalse(self.backups.exists())

    def test_db_and_dir_can_come_from_the_environment(self):
        make_wallet(self.db)[0].close()
        os.environ.update(WALLET_DB=str(self.db), WALLET_BACKUP_DIR=str(self.backups))
        self.assertEqual(self.run_cli("backup")[0], 0)
        [backup] = B.list_backups(self.backups)
        status, out, _ = self.run_cli("list")
        self.assertEqual(status, 0)
        self.assertIn(backup["name"], out)
        # A flag wins over the variable.
        self.assertEqual(self.run_cli("check", "--db", self.dir / "none.db")[0], 1)

    @unittest.skipIf(os.name == "nt", "no umask on Windows")
    def test_what_it_makes_only_its_user_can_read(self):
        secret = "a long admin secret"
        usb = self.dir / "usb"
        usb.mkdir()
        os.umask(0o022)                             # as most shells have it
        self.assertEqual(self.run_cli("first-admin", "--db", self.db, "--name", "Huda",
                                      "--phone", "0910000000",
                                      passwords=[secret, secret])[0], 0)
        # Only the umask covers this one: connecting makes the new database.
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode) & 0o077, 0)
        os.umask(0o022)
        self.assertEqual(self.run_cli("backup", "--db", self.db, "--dir", self.backups,
                                      "--copy-to", usb)[0], 0)
        [backup] = B.list_backups(self.backups)
        os.umask(0o022)
        self.assertEqual(self.run_cli("restore", backup["path"], "--db", self.db,
                                      "--yes")[0], 0)
        for path in (self.backups, Path(backup["path"]), usb / backup["name"], self.db):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0, path)

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

    def test_restore_says_what_is_undone_and_that_everyone_logs_in_again(self):
        make_wallet(self.db)[0].close()
        backup = B.snapshot(self.db, self.backups, now=NOW)["path"]
        restored = {"restored_from": backup, "db_path": str(self.db), "moved_aside": [],
                    "problems": [], "sessions_ended": 3, "codes_voided": 1}
        with mock.patch.object(B, "restore", return_value=restored):
            status, out, _ = self.run_cli("restore", backup, "--db", self.db,
                                          typed="restore")
        self.assertEqual(status, 0)
        said = " ".join(out.split())
        self.assertIn("Blocks, deactivations, password and setup-code changes and new "
                      "accounts made since are undone too, and must be done again.", said)
        self.assertIn("everyone must log in again (3 session(s) ended)", said)
        self.assertIn("setup codes given out before the backup no longer work "
                      "(1 voided)", said)

    def test_restore_refused_while_the_server_runs(self):
        make_wallet(self.db)[0].close()
        backup = B.snapshot(self.db, self.backups, now=NOW)["path"]
        with B.server_lock(self.db):
            status, _, err = self.run_cli("restore", backup, "--db", self.db, "--yes")
        self.assertEqual(status, 1)
        self.assertIn("another server", err)


if __name__ == "__main__":
    unittest.main()
