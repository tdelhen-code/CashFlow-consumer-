"""Looking after the wallet from the command line.

    python3 -m app.manage first-admin --db wallet.db --name "Name" --phone "091 000 0000"
    python3 -m app.manage backup --db wallet.db --dir backups [--copy-to FOLDER ...]
    python3 -m app.manage list --dir backups
    python3 -m app.manage check --db wallet.db
    python3 -m app.manage check-backup backups/wallet-20261018T052600Z.db.gz
    python3 -m app.manage restore backups/wallet-20261018T052600Z.db.gz --db wallet.db

Exit status: 0 done; 1 failed, including a mistyped command (a plain message
on stderr); 2 for backup and check when the ledger or order checks found
problems (a backup is still made: it shows how things were). A backup that
was made but could not be copied, or its old ones removed, is 1, and still
shows the problems.

The files and folders it makes (a database, backups) are for the user it
runs as only (umask 077; not on Windows).

--db and --dir can be left out where WALLET_DB and WALLET_BACKUP_DIR are set
(as in the container: docker compose exec app manage backup).
"""
import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from . import auth as A
from . import backup as B
from . import ledger as L

OK, FAILED, PROBLEMS = 0, 1, 2


class Failed(Exception):
    """A plain message for the person at the keyboard, and exit status 1."""


def _say(*lines):
    for line in lines:
        print(line)


def _problems(problems):
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)


def first_admin(args):
    if not args.name.strip():
        raise Failed("the name is required")
    conn = A.connect(args.db)
    try:
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
            raise Failed("this database already has an admin; they can add others "
                         "from the admin screens")
        password = getpass.getpass("Password for the first admin: ")
        if getpass.getpass("Type it again: ") != password:
            raise Failed("the two passwords are not the same")
        user_id = A.create_first_admin(conn, args.name, args.phone, password,
                                       language=args.language)
    finally:
        conn.close()
    _say(f"first admin created (user {user_id}); log in with that phone and password")
    return OK


def backup(args):
    result = B.run(args.db, args.dir, args.copy_to)
    _say(f"backup {result['path']} ({result['size']} bytes)")
    for copy in result["copies"]:
        _say(f"copied to {copy}")
    for path in result["pruned"]:
        _say(f"removed old {path}")
    # The ledger problems first, so a copy that failed as well doesn't hide them.
    _problems(result["problems"])
    failed = []
    if result["copy_errors"]:
        failed.append("copying it failed: " + "; ".join(result["copy_errors"]))
    failed += result.get("prune_errors", [])
    if failed:
        raise Failed("the backup was made, but " + "; ".join(failed))
    return PROBLEMS if result["problems"] else OK


def list_backups(args):
    backups = B.list_backups(args.dir)
    if not backups:
        _say(f"no backups in {args.dir}")
    for b in backups:
        _say(f"{b['name']}  {b['created_at']}  {b['size']} bytes")
    return OK


def _report(report, what):
    if report["problems"]:
        _problems(report["problems"])
        return PROBLEMS
    _say(f"{what}: sound, and the ledger and order checks found nothing wrong "
         f"(schema {report['schema_version']})")
    return OK


def check(args):
    return _report(B.check_database(args.db), args.db)


def check_backup(args):
    return _report(B.check_file(args.file), args.file)


def restore(args):
    if not Path(args.file).is_file():
        raise Failed(f"no backup file at {args.file}")
    if not args.yes:
        _say(f"This puts {args.file} in place of {args.db}.",
             "Everything recorded after that backup was taken will be gone from the",
             "database (the current file is kept aside). Blocks, deactivations,",
             "password and setup-code changes and new accounts made since are undone",
             "too, and must be done again. Everyone will have to log in again.",
             "The server must be stopped.")
        if input("Type 'restore' to go ahead: ").strip() != "restore":
            raise Failed("not restored")
    result = B.restore(args.file, args.db)
    for path in result["moved_aside"]:
        _say(f"kept the replaced file as {path}")
    ended, voided = result.get("sessions_ended", 0), result.get("codes_voided", 0)
    _say(f"restored {args.db} from {args.file}",
         f"everyone must log in again ({ended} session(s) ended)",
         f"setup codes given out before the backup no longer work ({voided} voided)")
    if result["problems"]:
        _problems(result["problems"])
    return OK


def _env(name):
    return os.environ.get(name) or None


def _db(p):
    p.add_argument("--db", default=_env("WALLET_DB"), required=not _env("WALLET_DB"),
                   help="the database file (or WALLET_DB)")


def _dir(p):
    p.add_argument("--dir", default=_env("WALLET_BACKUP_DIR"),
                   required=not _env("WALLET_BACKUP_DIR"),
                   help="the backups folder (or WALLET_BACKUP_DIR)")


class _Parser(argparse.ArgumentParser):
    """argparse, but a mistyped command exits 1 like any failure: its own 2
    would read as 'a backup was made, and the ledger has problems'. The
    subcommands' parsers are made of this class too (add_subparsers takes
    the class of the parser it is called on)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(FAILED, f"{self.prog}: error: {message}\n")


def _parser():
    parser = _Parser(prog="python3 -m app.manage",
                     description="Look after the LYD wallet database.")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("first-admin", help="create the first admin (asks for the password)")
    _db(p)
    p.add_argument("--name", required=True)
    p.add_argument("--phone", required=True)
    p.add_argument("--language", choices=L.LANGUAGES, default="ar")
    p.set_defaults(func=first_admin)

    p = commands.add_parser("backup", help="back up the database now")
    _db(p)
    _dir(p)
    p.add_argument("--copy-to", action="append", default=[], metavar="FOLDER",
                   help="also copy the backup here (can be given more than once)")
    p.set_defaults(func=backup)

    p = commands.add_parser("list", help="list the backups in a folder, newest first")
    _dir(p)
    p.set_defaults(func=list_backups)

    p = commands.add_parser("check", help="check the database (sound, ledger and orders)")
    _db(p)
    p.set_defaults(func=check)

    p = commands.add_parser("check-backup", help="check a backup file without restoring it")
    p.add_argument("file")
    p.set_defaults(func=check_backup)

    p = commands.add_parser("restore", help="put a backup in place of the database "
                                            "(stop the server first)")
    p.add_argument("file")
    _db(p)
    p.add_argument("--yes", action="store_true", help="don't ask to confirm")
    p.set_defaults(func=restore)
    return parser


def main(argv=None):
    if os.name != "nt":
        os.umask(0o077)         # the database and its backups: for this user only
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except (Failed, B.BackupError, L.LedgerError, sqlite3.Error, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return FAILED
    except (KeyboardInterrupt, EOFError):
        print("\nstopped", file=sys.stderr)
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
