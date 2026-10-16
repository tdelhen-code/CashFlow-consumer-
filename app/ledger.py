"""Ledger core for the LYD wallet app.

Sign convention (every transaction's entries add up to zero):
- Customer accounts: positive = what the app owes that worker. Never below zero.
- Holding accounts (BANK and each TILL_*): where real money sits, stored negative.
  -1000 means 1000 dirhams is physically there. Never above zero, so the books
  can't pay out money the bank or a till doesn't have.
- System accounts:
    SYS_ORDERS   LYD collected for currency orders, waiting for supplier settlement
    SYS_FEES     commission earned
    SYS_CAPITAL  owner money put in (positive) or taken out (negative)

Other rules:
- Money is whole dirhams (1 LYD = 1000 dirhams). No floats anywhere.
- Entries, transactions and the admin log can't be edited or deleted. Mistakes
  are fixed with a reversal, which is itself a new recorded transaction.
- Admin actions need an active admin and are written to the admin log.
- Writes can carry an idempotency key, so a double tap or retry can't post twice.
"""
import json
import re
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

DIRHAMS_PER_LYD = 1000
SYSTEM_ACCOUNTS = ("SYS_ORDERS", "SYS_FEES", "SYS_CAPITAL")


class LedgerError(Exception):
    pass


class InsufficientFunds(LedgerError):
    """A worker's balance is too low."""


class NotEnoughCash(LedgerError):
    """The bank or till doesn't hold enough, according to the books."""


class InvalidAmount(LedgerError):
    pass


class UnknownAccount(LedgerError):
    pass


class PermissionDenied(LedgerError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  role TEXT NOT NULL CHECK (role IN ('customer','admin')),
  name TEXT NOT NULL,
  phone TEXT NOT NULL UNIQUE,
  language TEXT NOT NULL DEFAULT 'ar' CHECK (language IN ('ar','en','fr')),
  password_hash TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('customer','holding','system')),
  code TEXT NOT NULL UNIQUE,
  user_id INTEGER UNIQUE REFERENCES users(id),
  CHECK ((kind = 'customer') = (user_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('deposit','payroll','transfer','order',
                                     'withdrawal','move','capital','reversal')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  created_by INTEGER REFERENCES users(id),
  note TEXT,
  reference TEXT,
  idempotency_key TEXT UNIQUE,
  reverses_txn_id INTEGER UNIQUE REFERENCES transactions(id)
);

CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY,
  txn_id INTEGER NOT NULL REFERENCES transactions(id),
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount INTEGER NOT NULL CHECK (amount <> 0)
);
CREATE INDEX IF NOT EXISTS idx_entries_account ON entries(account_id);
CREATE INDEX IF NOT EXISTS idx_entries_txn ON entries(txn_id);

CREATE TABLE IF NOT EXISTS admin_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  admin_id INTEGER NOT NULL REFERENCES users(id),
  action TEXT NOT NULL,
  target TEXT,
  details TEXT
);

CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS transactions_no_update BEFORE UPDATE ON transactions
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS transactions_no_delete BEFORE DELETE ON transactions
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS admin_log_no_update BEFORE UPDATE ON admin_log
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS admin_log_no_delete BEFORE DELETE ON admin_log
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TRIGGER IF NOT EXISTS entries_no_overdraft BEFORE INSERT ON entries
WHEN (SELECT kind FROM accounts WHERE id = NEW.account_id) = 'customer'
 AND (SELECT COALESCE(SUM(amount), 0) FROM entries
      WHERE account_id = NEW.account_id) + NEW.amount < 0
BEGIN SELECT RAISE(ABORT, 'insufficient funds'); END;

CREATE TRIGGER IF NOT EXISTS entries_holding_has_money BEFORE INSERT ON entries
WHEN (SELECT kind FROM accounts WHERE id = NEW.account_id) = 'holding'
 AND (SELECT COALESCE(SUM(amount), 0) FROM entries
      WHERE account_id = NEW.account_id) + NEW.amount > 0
BEGIN SELECT RAISE(ABORT, 'not enough cash'); END;
"""


# ---------- money helpers ----------

def parse_lyd(text):
    """'12.345' -> 12345 dirhams. Rejects more than 3 decimals."""
    try:
        d = Decimal(str(text).strip())
    except InvalidOperation:
        raise InvalidAmount("not a number")
    if not d.is_finite():
        raise InvalidAmount("not a number")
    dirhams = d * DIRHAMS_PER_LYD
    if dirhams != dirhams.to_integral_value():
        raise InvalidAmount("at most 3 decimal places")
    return int(dirhams)


def format_lyd(dirhams):
    sign = "-" if dirhams < 0 else ""
    whole, part = divmod(abs(dirhams), DIRHAMS_PER_LYD)
    return f"{sign}{whole:,}.{part:03d}"


def _check_amount(amount):
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise InvalidAmount("amount must be a positive whole number of dirhams")


def _check_fee(fee):
    if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
        raise InvalidAmount("fee must be zero or a positive whole number of dirhams")


def commission(gross, rate_bp, minimum=0):
    """Commission on a gross amount. rate_bp is basis points (100 = 1%).
    Rounds half up, never below `minimum`."""
    _check_amount(gross)
    fee = (gross * rate_bp + 5000) // 10000
    return max(fee, minimum)


# ---------- database plumbing ----------

def connect(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO accounts(kind, code) VALUES ('holding', 'BANK')")
    for code in SYSTEM_ACCOUNTS:
        conn.execute(
            "INSERT OR IGNORE INTO accounts(kind, code) VALUES ('system', ?)", (code,))
    return conn


@contextmanager
def _write(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _existing(conn, key):
    if key is None:
        return None
    row = conn.execute(
        "SELECT id FROM transactions WHERE idempotency_key = ?", (key,)).fetchone()
    return row["id"] if row else None


def _customer_account(conn, user_id):
    row = conn.execute(
        "SELECT a.id FROM accounts a JOIN users u ON u.id = a.user_id "
        "WHERE a.user_id = ? AND u.active = 1", (user_id,)).fetchone()
    if not row:
        raise UnknownAccount(f"no active customer account for user {user_id}")
    return row["id"]


def _holding(conn, code):
    row = conn.execute(
        "SELECT id FROM accounts WHERE code = ? AND kind = 'holding'", (code,)).fetchone()
    if not row:
        raise UnknownAccount(f"no bank or till called {code}")
    return row["id"]


def _system(conn, code):
    return conn.execute("SELECT id FROM accounts WHERE code = ?", (code,)).fetchone()["id"]


def _require_admin(conn, admin_id):
    row = conn.execute(
        "SELECT 1 FROM users WHERE id = ? AND role = 'admin' AND active = 1",
        (admin_id,)).fetchone()
    if not row:
        raise PermissionDenied("admin only")


def _log(conn, admin_id, action, target=None, **details):
    conn.execute(
        "INSERT INTO admin_log(admin_id, action, target, details) VALUES (?,?,?,?)",
        (admin_id, action, None if target is None else str(target),
         json.dumps(details, sort_keys=True, default=str)))


def _post(conn, kind, entries, created_by, note=None, reference=None,
          idempotency_key=None, reverses_txn_id=None):
    if sum(amount for _, amount in entries) != 0:
        raise LedgerError("transaction does not balance")
    cur = conn.execute(
        "INSERT INTO transactions(kind, created_by, note, reference, idempotency_key, "
        "reverses_txn_id) VALUES (?,?,?,?,?,?)",
        (kind, created_by, note, reference, idempotency_key, reverses_txn_id))
    txn_id = cur.lastrowid
    for account_id, amount in entries:
        try:
            conn.execute(
                "INSERT INTO entries(txn_id, account_id, amount) VALUES (?,?,?)",
                (txn_id, account_id, amount))
        except sqlite3.IntegrityError as e:
            if "insufficient funds" in str(e):
                raise InsufficientFunds("balance too low") from None
            if "not enough cash" in str(e):
                raise NotEnoughCash("not enough money in that bank or till") from None
            raise
    return txn_id


# ---------- users, tills ----------

def create_user(conn, role, name, phone, language="ar", password_hash=None, *,
                admin_id=None):
    """Only an admin can create users. The very first admin is created without one."""
    with _write(conn):
        if admin_id is None:
            if role != "admin" or conn.execute(
                    "SELECT 1 FROM users WHERE role = 'admin'").fetchone():
                raise PermissionDenied("an admin must create this user")
        else:
            _require_admin(conn, admin_id)
        cur = conn.execute(
            "INSERT INTO users(role, name, phone, language, password_hash) "
            "VALUES (?,?,?,?,?)", (role, name, phone, language, password_hash))
        user_id = cur.lastrowid
        if role == "customer":
            conn.execute(
                "INSERT INTO accounts(kind, code, user_id) VALUES ('customer', ?, ?)",
                (f"CUS_{user_id}", user_id))
        _log(conn, admin_id or user_id, "create_user", user_id, role=role, phone=phone)
    return user_id


def add_till(conn, name, *, admin_id):
    """A cash till at one of the shops. Returns its code, e.g. TILL_MAIN."""
    name = name.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{1,30}", name):
        raise LedgerError("till name: letters, digits and _ only")
    code = f"TILL_{name}"
    with _write(conn):
        _require_admin(conn, admin_id)
        conn.execute("INSERT INTO accounts(kind, code) VALUES ('holding', ?)", (code,))
        _log(conn, admin_id, "add_till", code)
    return code


# ---------- money in ----------

def deposit(conn, user_id, amount, *, admin_id, into, idempotency_key=None, note=None):
    """Admin confirms LYD received from a worker, into BANK or a till."""
    _check_amount(amount)
    with _write(conn):
        _require_admin(conn, admin_id)
        existing = _existing(conn, idempotency_key)
        if existing:
            return existing
        acc = _customer_account(conn, user_id)
        hold = _holding(conn, into)
        txn = _post(conn, "deposit", [(hold, -amount), (acc, amount)], admin_id,
                    note=note, reference=into, idempotency_key=idempotency_key)
        _log(conn, admin_id, "deposit", txn, user_id=user_id, amount=amount, into=into)
        return txn


def payroll_credit(conn, items, *, admin_id, batch_ref, into="BANK",
                   idempotency_key=None, note=None):
    """Company sent one payment; credit each worker. items = [(user_id, amount)]."""
    if not items:
        raise LedgerError("payroll batch is empty")
    for _, amount in items:
        _check_amount(amount)
    with _write(conn):
        _require_admin(conn, admin_id)
        existing = _existing(conn, idempotency_key)
        if existing:
            return existing
        total = sum(amount for _, amount in items)
        entries = [(_holding(conn, into), -total)]
        entries += [(_customer_account(conn, uid), amount) for uid, amount in items]
        txn = _post(conn, "payroll", entries, admin_id, note=note, reference=batch_ref,
                    idempotency_key=idempotency_key)
        _log(conn, admin_id, "payroll", txn, batch_ref=batch_ref, workers=len(items),
             total=total, into=into)
        return txn


def capital(conn, code, amount, *, admin_id, direction, note=None, idempotency_key=None):
    """Owner puts money into the bank or a till (direction='in') or takes it out ('out')."""
    _check_amount(amount)
    if direction not in ("in", "out"):
        raise LedgerError("direction must be 'in' or 'out'")
    sign = -1 if direction == "in" else 1
    with _write(conn):
        _require_admin(conn, admin_id)
        existing = _existing(conn, idempotency_key)
        if existing:
            return existing
        txn = _post(conn, "capital",
                    [(_holding(conn, code), sign * amount),
                     (_system(conn, "SYS_CAPITAL"), -sign * amount)],
                    admin_id, note=note, reference=f"{direction}:{code}",
                    idempotency_key=idempotency_key)
        _log(conn, admin_id, "capital", txn, code=code, amount=amount, direction=direction)
        return txn


# ---------- money moving ----------

def transfer(conn, from_user, to_user, amount, *, fee=0, created_by=None,
             idempotency_key=None, note=None):
    """Worker sends LYD balance to another worker. Optional fee goes to SYS_FEES."""
    _check_amount(amount)
    _check_fee(fee)
    if from_user == to_user:
        raise LedgerError("cannot transfer to yourself")
    with _write(conn):
        existing = _existing(conn, idempotency_key)
        if existing:
            return existing
        src = _customer_account(conn, from_user)
        dst = _customer_account(conn, to_user)
        entries = [(src, -(amount + fee)), (dst, amount)]
        if fee:
            entries.append((_system(conn, "SYS_FEES"), fee))
        return _post(conn, "transfer", entries, created_by or from_user,
                     note=note, idempotency_key=idempotency_key)


def move_money(conn, from_code, to_code, amount, *, admin_id, note=None,
               idempotency_key=None):
    """Physical money moved between the bank and a till, or between tills."""
    _check_amount(amount)
    if from_code == to_code:
        raise LedgerError("from and to are the same")
    with _write(conn):
        _require_admin(conn, admin_id)
        existing = _existing(conn, idempotency_key)
        if existing:
            return existing
        src = _holding(conn, from_code)
        dst = _holding(conn, to_code)
        txn = _post(conn, "move", [(src, amount), (dst, -amount)], admin_id, note=note,
                    reference=f"{from_code}->{to_code}", idempotency_key=idempotency_key)
        _log(conn, admin_id, "move_money", txn, source=from_code, destination=to_code,
             amount=amount)
        return txn


def _charge_order(conn, user_id, amount, *, order_ref, idempotency_key=None):
    """Used by orders.place_order inside its own database transaction."""
    _check_amount(amount)
    acc = _customer_account(conn, user_id)
    return _post(conn, "order", [(acc, -amount), (_system(conn, "SYS_ORDERS"), amount)],
                 user_id, reference=order_ref, idempotency_key=idempotency_key)


# ---------- money out ----------

def withdraw_cash(conn, user_id, gross, fee, *, admin_id, from_till,
                  idempotency_key=None, note=None):
    """Worker takes LYD cash at a till. Balance drops by `gross`; the worker gets
    gross - fee in cash; the fee is the commission."""
    _check_amount(gross)
    _check_fee(fee)
    if fee >= gross:
        raise InvalidAmount("fee must be less than the amount")
    with _write(conn):
        _require_admin(conn, admin_id)
        existing = _existing(conn, idempotency_key)
        if existing:
            return existing
        acc = _customer_account(conn, user_id)
        entries = [(acc, -gross), (_holding(conn, from_till), gross - fee)]
        if fee:
            entries.append((_system(conn, "SYS_FEES"), fee))
        txn = _post(conn, "withdrawal", entries, admin_id, note=note,
                    reference=from_till, idempotency_key=idempotency_key)
        _log(conn, admin_id, "withdraw_cash", txn, user_id=user_id, gross=gross,
             fee=fee, till=from_till)
        return txn


# ---------- fixing mistakes ----------

def _txn_kind(conn, txn_id):
    row = conn.execute("SELECT kind FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if not row:
        raise LedgerError("transaction not found")
    return row["kind"]


def _reverse(conn, txn_id, admin_id, reason, idempotency_key=None):
    if not (reason or "").strip():
        raise LedgerError("a reason is required")
    if _txn_kind(conn, txn_id) == "reversal":
        raise LedgerError("a reversal cannot be reversed")
    if conn.execute("SELECT 1 FROM transactions WHERE reverses_txn_id = ?",
                    (txn_id,)).fetchone():
        raise LedgerError("transaction already reversed")
    rows = conn.execute(
        "SELECT account_id, amount FROM entries WHERE txn_id = ?", (txn_id,)).fetchall()
    entries = [(r["account_id"], -r["amount"]) for r in rows]
    return _post(conn, "reversal", entries, admin_id, note=reason.strip(),
                 reference=f"reverses #{txn_id}", idempotency_key=idempotency_key,
                 reverses_txn_id=txn_id)


def reverse(conn, txn_id, *, admin_id, reason, idempotency_key=None):
    """Post the exact opposite of a transaction. Each can be reversed once.
    Currency orders are cancelled with orders.cancel_order instead."""
    with _write(conn):
        _require_admin(conn, admin_id)
        existing = _existing(conn, idempotency_key)
        if existing:
            return existing
        if _txn_kind(conn, txn_id) == "order":
            raise LedgerError("cancel the order instead, so its status stays correct")
        txn = _reverse(conn, txn_id, admin_id, reason, idempotency_key)
        _log(conn, admin_id, "reverse", txn, reverses=txn_id, reason=reason)
        return txn


# ---------- reading ----------

def balance(conn, user_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(e.amount), 0) AS b FROM entries e "
        "JOIN accounts a ON a.id = e.account_id WHERE a.user_id = ?", (user_id,)).fetchone()
    return row["b"]


def account_balance(conn, code):
    row = conn.execute(
        "SELECT COALESCE(SUM(e.amount), 0) AS b FROM entries e "
        "JOIN accounts a ON a.id = e.account_id WHERE a.code = ?", (code,)).fetchone()
    return row["b"]


def holding_amount(conn, code):
    """Money the books say is physically in the bank or till (positive number)."""
    _holding(conn, code)
    return -account_balance(conn, code)


def owed_to_customers(conn):
    return conn.execute(
        "SELECT COALESCE(SUM(e.amount), 0) FROM entries e "
        "JOIN accounts a ON a.id = e.account_id WHERE a.kind = 'customer'").fetchone()[0]


def statement(conn, user_id, limit=50):
    acc = _customer_account(conn, user_id)
    rows = conn.execute(
        "SELECT * FROM ("
        " SELECT e.id AS entry_id, t.id AS txn_id, t.kind, t.created_at, e.amount,"
        "        t.note, t.reference,"
        "        SUM(e.amount) OVER (ORDER BY e.id) AS balance_after"
        " FROM entries e JOIN transactions t ON t.id = e.txn_id"
        " WHERE e.account_id = ?"
        ") ORDER BY entry_id DESC LIMIT ?", (acc, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------- checks ----------

def verify_ledger(conn):
    """Internal consistency check, safe to run every hour. Empty list = healthy."""
    problems = []
    total = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM entries").fetchone()[0]
    if total != 0:
        problems.append(f"ledger total is {total}, expected 0")
    for r in conn.execute(
            "SELECT txn_id, SUM(amount) AS s FROM entries GROUP BY txn_id "
            "HAVING SUM(amount) <> 0"):
        problems.append(f"transaction {r['txn_id']} does not balance ({r['s']})")
    for r in conn.execute(
            "SELECT id FROM transactions WHERE id NOT IN (SELECT txn_id FROM entries)"):
        problems.append(f"transaction {r['id']} has no entries")
    for r in conn.execute(
            "SELECT a.code, SUM(e.amount) AS s FROM entries e "
            "JOIN accounts a ON a.id = e.account_id WHERE a.kind = 'customer' "
            "GROUP BY a.id HAVING SUM(e.amount) < 0"):
        problems.append(f"account {r['code']} is negative ({r['s']})")
    for r in conn.execute(
            "SELECT a.code, SUM(e.amount) AS s FROM entries e "
            "JOIN accounts a ON a.id = e.account_id WHERE a.kind = 'holding' "
            "GROUP BY a.id HAVING SUM(e.amount) > 0"):
        problems.append(f"{r['code']} shows less than zero money ({-r['s']})")
    return problems


def reconcile(conn, counts):
    """counts = {"BANK": dirhams on the statement, "TILL_MAIN": dirhams counted}.
    One row per account: what the books say, what is really there, and the gap."""
    rows = []
    for code, actual in counts.items():
        if not isinstance(actual, int) or isinstance(actual, bool):
            raise InvalidAmount("counts must be whole dirhams")
        expected = holding_amount(conn, code)
        rows.append({"account": code, "expected": expected, "actual": actual,
                     "difference": actual - expected, "ok": actual == expected})
    return rows
