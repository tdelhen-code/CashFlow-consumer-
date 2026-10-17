"""Ledger core for the LYD wallet app.

Sign convention (every transaction's entries add up to zero):
- Customer accounts: positive = what the app owes that worker. Never below zero.
- Holding accounts (BANK and each TILL_*): where real money sits, stored negative.
  -1000 means 1000 dirhams is physically there. Never above zero, so the books
  can't pay out money the bank or a till doesn't have.
- Supplier accounts (SUP_<id>): positive = what the app owes that supplier for
  orders they paid out. Negative = the supplier holds a prepayment from us.
- System accounts:
    SYS_ORDERS   LYD collected for currency orders not paid out yet
    SYS_FEES     commission earned
    SYS_MARGIN   difference between what workers paid and what suppliers charge
    SYS_CAPITAL  owner money put in (positive) or taken out (negative)

Other rules:
- Money is whole dirhams (1 LYD = 1000 dirhams). No floats anywhere.
- Entries, transactions and the admin log can't be edited or deleted. Mistakes
  are fixed with a reversal, which is itself a new recorded transaction.
- Admin actions need an active admin and are written to the admin log.
- Writes can carry an idempotency key, so a double tap or retry can't post twice.
  Keys are scoped to the person sending them and the kind of action, and a key
  reused with different details is refused instead of answered with the old result.
  A key is non-empty text (spaces around it are dropped), at most 200 characters.
- Each payroll batch reference can be posted once (again only after a reversal).
- Any single amount is at most MAX_DIRHAMS (1 billion LYD).

Users:
- Phone numbers are Libyan mobiles stored as +2189XXXXXXXX, whatever way they
  were typed. A WhatsApp number, if different, can be from any country.
- Deactivate = the worker has left. Only allowed at a zero balance with no open
  orders.
- Block = freeze while a problem or doubt is looked into. A blocked worker can't
  send, receive, order or take cash, but salary and admin deposits still come in.
  Their open currency orders are held too: no supplier can be assigned until
  they are unblocked (an admin can still unassign and cancel, which refunds to
  the frozen balance). A payout the supplier already made is still recorded.
  Blocked admins lose admin rights. Both need a reason and are logged.
"""
import json
import re
import sqlite3
from collections import namedtuple
from contextlib import contextmanager
from decimal import Decimal, DecimalException

DIRHAMS_PER_LYD = 1000
MAX_DIRHAMS = 10**12
SYSTEM_ACCOUNTS = ("SYS_ORDERS", "SYS_FEES", "SYS_MARGIN", "SYS_CAPITAL")
ROLES = ("customer", "admin")
LANGUAGES = ("ar", "en", "fr")
MOBILE_PREFIXES = ("91", "92", "93", "94", "95")   # Libyan mobile networks
MAX_IDEMPOTENCY_KEY = 200


class LedgerError(Exception):
    pass


class InsufficientFunds(LedgerError):
    """A worker's balance is too low."""


class NotEnoughCash(LedgerError):
    """The bank or till doesn't hold enough, according to the books."""


class InvalidAmount(LedgerError):
    pass


class NotFound(LedgerError):
    """No such user, account, supplier, order, quote, receipt code or transaction."""


class UnknownAccount(NotFound):
    pass


class PermissionDenied(LedgerError):
    pass


class AccountBlocked(LedgerError):
    """The worker is blocked by an admin; their money is frozen."""


class Conflict(LedgerError):
    """Clashes with something already recorded: an existing till, currency,
    supplier or phone number, a payroll batch that was already posted, a quote
    already used for another beneficiary, or an order that is (now) with a
    supplier, or with a different one."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  role TEXT NOT NULL CHECK (role IN ('customer','admin')),
  name TEXT NOT NULL,
  phone TEXT NOT NULL UNIQUE,
  language TEXT NOT NULL DEFAULT 'ar' CHECK (language IN ('ar','en','fr')),
  whatsapp TEXT,                      -- international format, only if not the phone
  password_hash TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  blocked INTEGER NOT NULL DEFAULT 0,
  blocked_reason TEXT,
  blocked_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('customer','holding','system','supplier')),
  code TEXT NOT NULL UNIQUE,
  user_id INTEGER UNIQUE REFERENCES users(id),
  CHECK ((kind = 'customer') = (user_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('deposit','payroll','transfer','order',
                                     'withdrawal','move','capital','reversal',
                                     'payout','settlement')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  created_by INTEGER REFERENCES users(id),
  note TEXT,
  reference TEXT,
  idempotency_key TEXT UNIQUE,
  request TEXT,                       -- details the idempotency key was used for
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
    except DecimalException:
        raise InvalidAmount("not a number")
    if not d.is_finite():
        raise InvalidAmount("not a number")
    if d.copy_abs() > MAX_DIRHAMS // DIRHAMS_PER_LYD:
        raise InvalidAmount("amount too large")
    dirhams = d * DIRHAMS_PER_LYD
    if dirhams != dirhams.to_integral_value():
        raise InvalidAmount("at most 3 decimal places")
    return int(dirhams)


# Arabic-Indic (٠-٩) and Extended Arabic-Indic (۰-۹, Persian) digits, as typed
# on Arabic and Persian keyboards, are the digits 0-9.
_EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)
# What may stand between the digits of a phone number: spaces, dashes, dots,
# brackets, and the invisible direction marks a number copied from
# right-to-left text carries.
_PHONE_SEPARATORS = re.compile(
    r"[\s\-().\u2010-\u2015\u2212\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _phone_text(text, what="phone number"):
    """A typed number with the digits in 0-9 and no separators. Anything else
    that is not ASCII (digits of other scripts, ²...) is refused, so a number
    is never stored as typed, past the duplicate check."""
    text = _PHONE_SEPARATORS.sub("", str(text or "").translate(_EASTERN_DIGITS))
    if not text.isascii():
        raise LedgerError(f"{what}: digits only")
    return text


def normalize_phone(text):
    """Any usual way of writing a Libyan mobile -> '+2189XXXXXXXX'.
    Accepts 091-1234567, 0911234567, 911234567, +218 91 123 4567, 00218...,
    also in Arabic-Indic digits (٠٩١ ١٢٣ ٤٥٦٧)."""
    digits = _phone_text(text)
    if digits.startswith("+"):
        digits = digits[1:]
    elif digits.startswith("00"):
        digits = digits[2:]
    if not digits.isdigit():
        raise LedgerError("phone number: digits only")
    if digits.startswith("218"):
        digits = digits[3:]
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 9 or digits[:2] not in MOBILE_PREFIXES:
        raise LedgerError("phone number must be a Libyan mobile, e.g. 091 234 5678")
    return "+218" + digits


def normalize_whatsapp(text):
    """WhatsApp number from any country -> '+<country><number>'. A number written
    the local way (starting with a single 0) is taken as Libyan."""
    raw = _phone_text(text, "WhatsApp number")
    if raw.startswith("+"):
        digits = raw[1:]
    elif raw.startswith("00"):
        digits = raw[2:]
    elif raw.startswith("0"):
        return normalize_phone(raw)
    else:
        raise LedgerError("WhatsApp number: start with + and the country code")
    if not digits.isdigit() or not 8 <= len(digits) <= 15 or digits[0] == "0":
        raise LedgerError("WhatsApp number: + then country code and number")
    if digits.startswith("218"):
        return normalize_phone(digits)
    return "+" + digits


def format_lyd(dirhams):
    sign = "-" if dirhams < 0 else ""
    whole, part = divmod(abs(dirhams), DIRHAMS_PER_LYD)
    return f"{sign}{whole:,}.{part:03d}"


def _check_amount(amount):
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise InvalidAmount("amount must be a positive whole number of dirhams")
    if amount > MAX_DIRHAMS:
        raise InvalidAmount("amount too large")


def _check_fee(fee):
    if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
        raise InvalidAmount("fee must be zero or a positive whole number of dirhams")
    if fee > MAX_DIRHAMS:
        raise InvalidAmount("fee too large")


def commission(gross, rate_bp, minimum=0):
    """Commission on a gross amount. rate_bp is basis points (100 = 1%).
    Rounds half up, never below `minimum`."""
    _check_amount(gross)
    if not isinstance(rate_bp, int) or isinstance(rate_bp, bool) or not 0 <= rate_bp <= 10000:
        raise InvalidAmount("rate must be whole basis points from 0 to 10000")
    _check_fee(minimum)
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


@contextmanager
def _read(conn):
    """Several reads from one snapshot, so a check never compares a number from
    before another connection's write with one from after it. Inside a
    transaction already (such as _write), that transaction is the snapshot."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN DEFERRED")
    try:
        yield
    finally:
        conn.execute("COMMIT")      # nothing was written; this just ends the read


Idem = namedtuple("Idem", "key request existing")
NO_IDEM = Idem(None, None, None)


def _idempotent(conn, key, actor, kind, **details):
    """Scope a caller's idempotency key to who sent it and what it is for.
    `existing` is the transaction already posted with it, if any. The same key
    with different details is an error, so two phones that happen to pick the
    same key can't make one of them think its money moved when it didn't."""
    if key is None:
        return NO_IDEM
    # A blank key must not count as a key: every blank-key request would then
    # look like a repeat of the first one and be silently dropped.
    if not isinstance(key, str) or not key.strip():
        raise LedgerError("idempotency key must be non-empty text")
    key = key.strip()
    if len(key) > MAX_IDEMPOTENCY_KEY:
        raise LedgerError(f"idempotency key: at most {MAX_IDEMPOTENCY_KEY} characters")
    scoped = f"{actor}:{kind}:{key}"
    request = json.dumps(details, sort_keys=True, default=str)
    row = conn.execute("SELECT id, request FROM transactions WHERE idempotency_key = ?",
                       (scoped,)).fetchone()
    if row and row["request"] != request:
        raise LedgerError("idempotency key already used for a different request")
    return Idem(scoped, request, row["id"] if row else None)


def _customer_account(conn, user_id):
    row = conn.execute(
        "SELECT a.id FROM accounts a JOIN users u ON u.id = a.user_id "
        "WHERE a.user_id = ? AND u.active = 1", (user_id,)).fetchone()
    if not row:
        raise UnknownAccount(f"no active customer account for user {user_id}")
    return row["id"]


def _usable_account(conn, user_id):
    """Account of a worker who may move money themselves (not blocked)."""
    acc = _customer_account(conn, user_id)
    if conn.execute("SELECT blocked FROM users WHERE id = ?", (user_id,)).fetchone()[0]:
        raise AccountBlocked(f"user {user_id} is blocked")
    return acc


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
        "SELECT 1 FROM users WHERE id = ? AND role = 'admin' AND active = 1 "
        "AND blocked = 0",
        (admin_id,)).fetchone()
    if not row:
        raise PermissionDenied("admin only")


def _log(conn, admin_id, action, target=None, **details):
    conn.execute(
        "INSERT INTO admin_log(admin_id, action, target, details) VALUES (?,?,?,?)",
        (admin_id, action, None if target is None else str(target),
         json.dumps(details, sort_keys=True, default=str)))


def _post(conn, kind, entries, created_by, note=None, reference=None,
          idem=NO_IDEM, reverses_txn_id=None):
    if sum(amount for _, amount in entries) != 0:
        raise LedgerError("transaction does not balance")
    entries = [(account_id, amount) for account_id, amount in entries if amount]
    cur = conn.execute(
        "INSERT INTO transactions(kind, created_by, note, reference, idempotency_key, "
        "request, reverses_txn_id) VALUES (?,?,?,?,?,?,?)",
        (kind, created_by, note, reference, idem.key, idem.request, reverses_txn_id))
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
                admin_id=None, whatsapp=None):
    """Only an admin can create users. The very first admin is created without one.
    `whatsapp` is only needed when it differs from the phone number."""
    phone = normalize_phone(phone)
    whatsapp = None if whatsapp is None else normalize_whatsapp(whatsapp)
    if whatsapp == phone:
        whatsapp = None
    if role not in ROLES:
        raise LedgerError(f"role must be one of {', '.join(ROLES)}")
    if language not in LANGUAGES:
        raise LedgerError(f"language must be one of {', '.join(LANGUAGES)}")
    with _write(conn):
        if admin_id is None:
            if role != "admin" or conn.execute(
                    "SELECT 1 FROM users WHERE role = 'admin'").fetchone():
                raise PermissionDenied("an admin must create this user")
        else:
            _require_admin(conn, admin_id)
        if conn.execute("SELECT 1 FROM users WHERE phone = ?", (phone,)).fetchone():
            raise Conflict(f"{phone} is already registered")
        cur = conn.execute(
            "INSERT INTO users(role, name, phone, whatsapp, language, password_hash) "
            "VALUES (?,?,?,?,?,?)", (role, name, phone, whatsapp, language, password_hash))
        user_id = cur.lastrowid
        if role == "customer":
            conn.execute(
                "INSERT INTO accounts(kind, code, user_id) VALUES ('customer', ?, ?)",
                (f"CUS_{user_id}", user_id))
        _log(conn, admin_id or user_id, "create_user", user_id, role=role, phone=phone)
    return user_id


def find_user_by_phone(conn, phone):
    """Look a user up by phone however it is typed. None if not registered."""
    row = conn.execute(
        "SELECT id, role, name, phone, whatsapp, language, active, blocked, "
        "blocked_reason, blocked_at FROM users WHERE phone = ?",
        (normalize_phone(phone),)).fetchone()
    return dict(row) if row else None


def set_whatsapp(conn, user_id, whatsapp, *, admin_id):
    """Set or clear (None) a user's WhatsApp number."""
    number = None if whatsapp is None else normalize_whatsapp(whatsapp)
    with _write(conn):
        _require_admin(conn, admin_id)
        user = _user(conn, user_id)
        if number == user["phone"]:
            number = None
        conn.execute("UPDATE users SET whatsapp = ? WHERE id = ?", (number, user_id))
        _log(conn, admin_id, "set_whatsapp", user_id, whatsapp=number)


def _user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise UnknownAccount(f"no user {user_id}")
    return row


def _end_sessions(conn, user_id):
    """Log the user out everywhere (when the login tables exist)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'sessions'").fetchone():
        conn.execute("UPDATE sessions SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                     "WHERE user_id = ? AND ended_at IS NULL", (user_id,))


def _reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise LedgerError("a reason is required")
    return reason


def block_user(conn, user_id, *, admin_id, reason):
    """Freeze a user while a problem or suspicion is checked. Their balance stays
    as it is; they can't send, receive, order or take cash until unblocked."""
    reason = _reason(reason)
    if user_id == admin_id:
        raise LedgerError("you cannot block yourself")
    with _write(conn):
        _require_admin(conn, admin_id)
        if _user(conn, user_id)["blocked"]:
            raise LedgerError("user is already blocked")
        conn.execute(
            "UPDATE users SET blocked = 1, blocked_reason = ?, "
            "blocked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (reason, user_id))
        _end_sessions(conn, user_id)
        _log(conn, admin_id, "block_user", user_id, reason=reason)


def unblock_user(conn, user_id, *, admin_id, reason):
    reason = _reason(reason)
    with _write(conn):
        _require_admin(conn, admin_id)
        if not _user(conn, user_id)["blocked"]:
            raise LedgerError("user is not blocked")
        conn.execute("UPDATE users SET blocked = 0, blocked_reason = NULL, "
                     "blocked_at = NULL WHERE id = ?", (user_id,))
        _log(conn, admin_id, "unblock_user", user_id, reason=reason)


def deactivate_user(conn, user_id, *, admin_id, reason):
    """The user has left. Only at a zero balance and with no unfinished orders,
    so no money is left behind on a closed account."""
    reason = _reason(reason)
    if user_id == admin_id:
        raise LedgerError("you cannot deactivate yourself")
    with _write(conn):
        _require_admin(conn, admin_id)
        if not _user(conn, user_id)["active"]:
            raise LedgerError("user is already inactive")
        left = balance(conn, user_id)
        if left:
            raise LedgerError(f"balance is {format_lyd(left)} LYD; pay it out first")
        has_orders = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()
        if has_orders and conn.execute(
                "SELECT 1 FROM orders WHERE customer_id = ? "
                "AND status IN ('placed','assigned')", (user_id,)).fetchone():
            raise LedgerError("user has unfinished orders")
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
        _end_sessions(conn, user_id)
        _log(conn, admin_id, "deactivate_user", user_id, reason=reason)


def reactivate_user(conn, user_id, *, admin_id, reason):
    reason = _reason(reason)
    with _write(conn):
        _require_admin(conn, admin_id)
        if _user(conn, user_id)["active"]:
            raise LedgerError("user is already active")
        conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
        _log(conn, admin_id, "reactivate_user", user_id, reason=reason)


def add_till(conn, name, *, admin_id):
    """A cash till at one of the shops. Returns its code, e.g. TILL_MAIN."""
    name = name.strip().upper() if isinstance(name, str) else ""
    if not re.fullmatch(r"[A-Z0-9_]{1,30}", name):
        raise LedgerError("till name: letters, digits and _ only")
    code = f"TILL_{name}"
    with _write(conn):
        _require_admin(conn, admin_id)
        if conn.execute("SELECT 1 FROM accounts WHERE code = ?", (code,)).fetchone():
            raise Conflict(f"{code} already exists")
        conn.execute("INSERT INTO accounts(kind, code) VALUES ('holding', ?)", (code,))
        _log(conn, admin_id, "add_till", code)
    return code


# ---------- money in ----------

def deposit(conn, user_id, amount, *, admin_id, into, idempotency_key=None, note=None):
    """Admin confirms LYD received from a worker, into BANK or a till."""
    _check_amount(amount)
    with _write(conn):
        _require_admin(conn, admin_id)
        idem = _idempotent(conn, idempotency_key, admin_id, "deposit",
                           user_id=user_id, amount=amount, into=into)
        if idem.existing:
            return idem.existing
        acc = _customer_account(conn, user_id)
        hold = _holding(conn, into)
        txn = _post(conn, "deposit", [(hold, -amount), (acc, amount)], admin_id,
                    note=note, reference=into, idem=idem)
        _log(conn, admin_id, "deposit", txn, user_id=user_id, amount=amount, into=into)
        return txn


def payroll_credit(conn, items, *, admin_id, batch_ref, into="BANK",
                   idempotency_key=None, note=None):
    """Company sent one payment; credit each worker. items = [(user_id, amount)].
    batch_ref (the company's payment reference) can be posted only once, whoever
    posts it, so a retried or re-entered batch can't pay everyone twice. After a
    reversal of that batch it can be posted again."""
    if not items:
        raise LedgerError("payroll batch is empty")
    for _, amount in items:
        _check_amount(amount)
    if not isinstance(batch_ref, str) or not batch_ref.strip():
        raise LedgerError("a payroll batch reference is required")
    batch_ref = batch_ref.strip()
    with _write(conn):
        _require_admin(conn, admin_id)
        idem = _idempotent(conn, idempotency_key, admin_id, "payroll", items=list(items),
                           batch_ref=batch_ref, into=into)
        if idem.existing:
            return idem.existing
        posted = conn.execute(
            "SELECT t.id FROM transactions t WHERE t.kind = 'payroll' "
            "AND t.reference = ? COLLATE NOCASE AND NOT EXISTS (SELECT 1 FROM "
            "transactions r WHERE r.reverses_txn_id = t.id)", (batch_ref,)).fetchone()
        if posted:
            raise Conflict(f"payroll batch {batch_ref} was already posted "
                           f"(transaction {posted['id']})")
        total = sum(amount for _, amount in items)
        entries = [(_holding(conn, into), -total)]
        entries += [(_customer_account(conn, uid), amount) for uid, amount in items]
        txn = _post(conn, "payroll", entries, admin_id, note=note, reference=batch_ref,
                    idem=idem)
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
        idem = _idempotent(conn, idempotency_key, admin_id, "capital", code=code,
                           amount=amount, direction=direction)
        if idem.existing:
            return idem.existing
        txn = _post(conn, "capital",
                    [(_holding(conn, code), sign * amount),
                     (_system(conn, "SYS_CAPITAL"), -sign * amount)],
                    admin_id, note=note, reference=f"{direction}:{code}", idem=idem)
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
    actor = created_by or from_user
    with _write(conn):
        idem = _idempotent(conn, idempotency_key, actor, "transfer", from_user=from_user,
                           to_user=to_user, amount=amount, fee=fee)
        if idem.existing:
            return idem.existing
        src = _usable_account(conn, from_user)
        dst = _usable_account(conn, to_user)
        entries = [(src, -(amount + fee)), (dst, amount)]
        if fee:
            entries.append((_system(conn, "SYS_FEES"), fee))
        return _post(conn, "transfer", entries, actor, note=note, idem=idem)


def move_money(conn, from_code, to_code, amount, *, admin_id, note=None,
               idempotency_key=None):
    """Physical money moved between the bank and a till, or between tills."""
    _check_amount(amount)
    if from_code == to_code:
        raise LedgerError("from and to are the same")
    with _write(conn):
        _require_admin(conn, admin_id)
        idem = _idempotent(conn, idempotency_key, admin_id, "move", from_code=from_code,
                           to_code=to_code, amount=amount)
        if idem.existing:
            return idem.existing
        src = _holding(conn, from_code)
        dst = _holding(conn, to_code)
        txn = _post(conn, "move", [(src, amount), (dst, -amount)], admin_id, note=note,
                    reference=f"{from_code}->{to_code}", idem=idem)
        _log(conn, admin_id, "move_money", txn, source=from_code, destination=to_code,
             amount=amount)
        return txn


def _charge_order(conn, user_id, amount, *, order_ref, idempotency_key=None):
    """Used by orders.place_order inside its own database transaction."""
    _check_amount(amount)
    acc = _usable_account(conn, user_id)
    idem = _idempotent(conn, idempotency_key, user_id, "order", amount=amount)
    if idem.existing:
        raise LedgerError("this order was already charged")
    return _post(conn, "order", [(acc, -amount), (_system(conn, "SYS_ORDERS"), amount)],
                 user_id, reference=order_ref, idem=idem)


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
        idem = _idempotent(conn, idempotency_key, admin_id, "withdrawal", user_id=user_id,
                           gross=gross, fee=fee, from_till=from_till)
        if idem.existing:
            return idem.existing
        acc = _usable_account(conn, user_id)
        entries = [(acc, -gross), (_holding(conn, from_till), gross - fee)]
        if fee:
            entries.append((_system(conn, "SYS_FEES"), fee))
        txn = _post(conn, "withdrawal", entries, admin_id, note=note,
                    reference=from_till, idem=idem)
        _log(conn, admin_id, "withdraw_cash", txn, user_id=user_id, gross=gross,
             fee=fee, till=from_till)
        return txn


# ---------- fixing mistakes ----------

def _txn_kind(conn, txn_id):
    row = conn.execute("SELECT kind FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if not row:
        raise NotFound("transaction not found")
    return row["kind"]


def _reverse(conn, txn_id, admin_id, reason, idem=NO_IDEM):
    if not (reason or "").strip():
        raise LedgerError("a reason is required")
    if _txn_kind(conn, txn_id) == "reversal":
        raise LedgerError("a reversal cannot be reversed")
    if conn.execute("SELECT 1 FROM transactions WHERE reverses_txn_id = ?",
                    (txn_id,)).fetchone():
        raise LedgerError("transaction already reversed")
    closed = conn.execute(
        "SELECT u.id FROM entries e JOIN accounts a ON a.id = e.account_id "
        "JOIN users u ON u.id = a.user_id WHERE e.txn_id = ? AND u.active = 0",
        (txn_id,)).fetchone()
    if closed:   # would leave money on (or take it from) a closed account
        raise LedgerError(f"user {closed['id']} is deactivated; reactivate them first")
    rows = conn.execute(
        "SELECT account_id, amount FROM entries WHERE txn_id = ?", (txn_id,)).fetchall()
    entries = [(r["account_id"], -r["amount"]) for r in rows]
    return _post(conn, "reversal", entries, admin_id, note=reason.strip(),
                 reference=f"reverses #{txn_id}", idem=idem, reverses_txn_id=txn_id)


def reverse(conn, txn_id, *, admin_id, reason, idempotency_key=None):
    """Post the exact opposite of a transaction. Each can be reversed once.
    Currency orders are cancelled with orders.cancel_order instead. Refused while
    a worker it touches is deactivated, so a closed account stays at zero."""
    with _write(conn):
        _require_admin(conn, admin_id)
        idem = _idempotent(conn, idempotency_key, admin_id, "reversal", txn_id=txn_id)
        if idem.existing:
            return idem.existing
        kind = _txn_kind(conn, txn_id)
        if kind == "order":
            raise LedgerError("cancel the order instead, so its status stays correct")
        if kind == "payout":
            raise LedgerError("a paid order is final")
        txn = _reverse(conn, txn_id, admin_id, reason, idem)
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
    return _statement(conn, _customer_account(conn, user_id), limit)


def account_statement(conn, code, limit=50):
    """Statement for any account by code, e.g. BANK, TILL_MAIN, SUP_3, SYS_MARGIN."""
    row = conn.execute("SELECT id FROM accounts WHERE code = ?", (code,)).fetchone()
    if not row:
        raise UnknownAccount(f"no account called {code}")
    return _statement(conn, row["id"], limit)


def _statement(conn, acc, limit):
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
    """Internal consistency check, safe to run every hour. Empty list = healthy.
    All of it reads one snapshot, so writes going on meanwhile can't confuse it."""
    with _read(conn):
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
        for r in conn.execute(
                "SELECT a.code, SUM(e.amount) AS s FROM entries e "
                "JOIN accounts a ON a.id = e.account_id JOIN users u ON u.id = a.user_id "
                "WHERE u.active = 0 GROUP BY a.id HAVING SUM(e.amount) <> 0"):
            problems.append(f"account {r['code']} of a deactivated user holds {r['s']}")
        return problems


def reconcile(conn, counts):
    """counts = {"BANK": dirhams on the statement, "TILL_MAIN": dirhams counted}.
    One row per account: what the books say, what is really there, and the gap."""
    rows = []
    for code, actual in counts.items():
        if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
            raise InvalidAmount("counts must be zero or more whole dirhams")
        expected = holding_amount(conn, code)
        rows.append({"account": code, "expected": expected, "actual": actual,
                     "difference": actual - expected, "ok": actual == expected})
    return rows
