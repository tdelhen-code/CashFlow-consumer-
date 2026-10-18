import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import auth as A
from app import ledger as L


def version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def set_version(path, n):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {n}")
    conn.close()


class SchemaVersionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "wallet.db"

    def make(self):
        conn = A.connect(self.db)
        A.create_first_admin(conn, "Admin", "0910000000", "a long admin secret")
        conn.close()

    def test_a_new_database_is_stamped(self):
        self.make()
        self.assertEqual(version(self.db), L.SCHEMA_VERSION)
        memory = A.connect(":memory:")
        self.assertEqual(L.schema_version(memory), L.SCHEMA_VERSION)
        memory.close()

    def test_one_made_before_versions_were_kept_is_stamped_and_kept(self):
        self.make()
        set_version(self.db, 0)
        conn = A.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(L.schema_version(conn), L.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT name FROM users").fetchone()[0], "Admin")

    def test_one_from_a_newer_app_is_refused_untouched(self):
        self.make()
        set_version(self.db, L.SCHEMA_VERSION + 1)
        before = self.db.read_bytes()
        with self.assertRaises(L.DatabaseTooNew):
            A.connect(self.db)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(version(self.db), L.SCHEMA_VERSION + 1)

    def test_migrations_run_in_order_in_one_write(self):
        self.make()
        done = []

        def two(conn):
            self.assertTrue(conn.in_transaction)
            conn.execute("CREATE TABLE notes (text TEXT)")
            done.append(2)

        def three(conn):
            conn.execute("INSERT INTO notes VALUES ('three')")
            done.append(3)

        with mock.patch.object(L, "SCHEMA_VERSION", 3), \
                mock.patch.object(L, "MIGRATIONS", {2: two, 3: three}):
            conn = A.connect(self.db)
            self.assertEqual(done, [2, 3])
            self.assertEqual(L.schema_version(conn), 3)
            conn.close()
            A.connect(self.db).close()          # already done: not again
            self.assertEqual(done, [2, 3])

    def test_a_failing_migration_changes_nothing(self):
        self.make()

        def two(conn):
            conn.execute("CREATE TABLE notes (text TEXT)")
            raise sqlite3.OperationalError("step failed")

        with mock.patch.object(L, "SCHEMA_VERSION", 2), \
                mock.patch.object(L, "MIGRATIONS", {2: two}):
            with self.assertRaises(sqlite3.OperationalError):
                A.connect(self.db)
        self.assertEqual(version(self.db), 1)
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'notes'").fetchone())


if __name__ == "__main__":
    unittest.main()
