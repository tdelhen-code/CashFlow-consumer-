"""Looking after the wallet from the command line.

    python3 -m app.manage first-admin --db wallet.db --name "Name" --phone "091 000 0000"
    python3 -m app.manage backup --db wallet.db --dir backups [--copy-to FOLDER ...]
    python3 -m app.manage list --dir backups
    python3 -m app.manage check --db wallet.db
    python3 -m app.manage check-backup backups/wallet-20261018T052600Z.db.gz
    python3 -m app.manage restore backups/wallet-20261018T052600Z.db.gz --db wallet.db

Exit status: 0 done; 1 failed (a plain message on stderr); 2 for backup and
check when the ledger check found problems (a backup is still made: it shows
how things were).
"""
import argparse
import getpass
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
    if result["copy_errors"]:
        raise Failed("the backup was made, but copying it failed: "
                     + "; ".join(result["copy_errors"]))
    if result["problems"]:
        _problems(result["problems"])
        return PROBLEMS
    return OK


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
             "database (the current file is kept aside). The server must be stopped.")
        if input("Type 'restore' to go ahead: ").strip() != "restore":
            raise Failed("not restored")
    result = B.restore(args.file, args.db)
    for path in result["moved_aside"]:
        _say(f"kept the replaced file as {path}")
    _say(f"restored {args.db} from {args.file}")
    if result["problems"]:
        _problems(result["problems"])
    return OK


def _parser():
    parser = argparse.ArgumentParser(prog="python3 -m app.manage",
                                     description="Look after the LYD wallet database.")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("first-admin", help="create the first admin (asks for the password)")
    p.add_argument("--db", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--phone", required=True)
    p.add_argument("--language", choices=L.LANGUAGES, default="ar")
    p.set_defaults(func=first_admin)

    p = commands.add_parser("backup", help="back up the database now")
    p.add_argument("--db", required=True)
    p.add_argument("--dir", required=True, help="folder for the backups")
    p.add_argument("--copy-to", action="append", default=[], metavar="FOLDER",
                   help="also copy the backup here (can be given more than once)")
    p.set_defaults(func=backup)

    p = commands.add_parser("list", help="list the backups in a folder, newest first")
    p.add_argument("--dir", required=True)
    p.set_defaults(func=list_backups)

    p = commands.add_parser("check", help="check the database (sound, ledger and orders)")
    p.add_argument("--db", required=True)
    p.set_defaults(func=check)

    p = commands.add_parser("check-backup", help="check a backup file without restoring it")
    p.add_argument("file")
    p.set_defaults(func=check_backup)

    p = commands.add_parser("restore", help="put a backup in place of the database "
                                            "(stop the server first)")
    p.add_argument("file")
    p.add_argument("--db", required=True)
    p.add_argument("--yes", action="store_true", help="don't ask to confirm")
    p.set_defaults(func=restore)
    return parser


def main(argv=None):
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
