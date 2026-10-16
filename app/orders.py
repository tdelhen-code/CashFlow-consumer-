"""Foreign currency orders.

Flow: admin sets a price -> worker gets a quote, locked for 10 minutes -> worker
places the order with the beneficiary's name and ID -> the LYD leaves the balance
at once and a receipt code is issued -> admin assigns a supplier -> admin marks it
paid with the supplier's confirmation reference. Paid and cancelled orders are final.

The LYD for an order stays in SYS_ORDERS until supplier settlement is recorded
(a later step, together with supplier costs and margin).
"""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext

from . import ledger as L

QUOTE_MINUTES = 10
RECEIPT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
RECEIPT_LENGTH = 10
MAX_NUMBER = Decimal(10**12)   # upper bound for rates and foreign amounts typed in


class QuoteExpired(L.LedgerError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS currencies (
  code TEXT PRIMARY KEY CHECK (length(code) = 3 AND code = upper(code)),
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prices (
  id INTEGER PRIMARY KEY,
  currency TEXT NOT NULL REFERENCES currencies(code),
  rate TEXT NOT NULL,                 -- LYD for 1 unit, exact decimal text
  set_by INTEGER NOT NULL REFERENCES users(id),
  set_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  country TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS quotes (
  id TEXT PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES users(id),
  currency TEXT NOT NULL,
  foreign_amount TEXT NOT NULL,
  rate TEXT NOT NULL,
  cost INTEGER NOT NULL CHECK (cost > 0),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  receipt_code TEXT NOT NULL UNIQUE,
  quote_id TEXT NOT NULL UNIQUE REFERENCES quotes(id),
  customer_id INTEGER NOT NULL REFERENCES users(id),
  currency TEXT NOT NULL,
  foreign_amount TEXT NOT NULL,
  rate TEXT NOT NULL,
  cost INTEGER NOT NULL,
  beneficiary_name TEXT NOT NULL,
  beneficiary_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('placed','assigned','paid','cancelled')),
  supplier_id INTEGER REFERENCES suppliers(id),
  charge_txn_id INTEGER NOT NULL REFERENCES transactions(id),
  reversal_txn_id INTEGER REFERENCES transactions(id),
  payout_ref TEXT,
  created_at TEXT NOT NULL,
  assigned_at TEXT,
  paid_at TEXT,
  cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TRIGGER IF NOT EXISTS orders_final BEFORE UPDATE ON orders
WHEN OLD.status IN ('paid','cancelled')
BEGIN SELECT RAISE(ABORT, 'order is final'); END;

CREATE TRIGGER IF NOT EXISTS orders_terms_fixed BEFORE UPDATE ON orders
WHEN NEW.receipt_code IS NOT OLD.receipt_code OR NEW.quote_id IS NOT OLD.quote_id
  OR NEW.customer_id IS NOT OLD.customer_id OR NEW.currency IS NOT OLD.currency
  OR NEW.foreign_amount IS NOT OLD.foreign_amount OR NEW.rate IS NOT OLD.rate
  OR NEW.cost IS NOT OLD.cost OR NEW.beneficiary_name IS NOT OLD.beneficiary_name
  OR NEW.beneficiary_id IS NOT OLD.beneficiary_id
  OR NEW.charge_txn_id IS NOT OLD.charge_txn_id
BEGIN SELECT RAISE(ABORT, 'order terms cannot change'); END;

CREATE TRIGGER IF NOT EXISTS orders_no_delete BEFORE DELETE ON orders
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS prices_no_update BEFORE UPDATE ON prices
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS prices_no_delete BEFORE DELETE ON prices
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS quotes_no_update BEFORE UPDATE ON quotes
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS quotes_no_delete BEFORE DELETE ON quotes
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


def connect(path=":memory:"):
    conn = L.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _now(now):
    return now or datetime.now(timezone.utc)


def _decimal(text, places, what):
    try:
        d = Decimal(str(text).strip())
    except DecimalException:
        raise L.InvalidAmount(f"{what} is not a number")
    if not d.is_finite() or d <= 0:
        raise L.InvalidAmount(f"{what} must be more than zero")
    if d > MAX_NUMBER:
        raise L.InvalidAmount(f"{what} is too large")
    if d != d.quantize(Decimal(1).scaleb(-places)):
        raise L.InvalidAmount(f"{what}: at most {places} decimal places")
    return d


def _get_order(conn, order_id):
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        raise L.LedgerError("order not found")
    return row


def _active_currency(conn, code):
    if not conn.execute("SELECT 1 FROM currencies WHERE code = ? AND active = 1",
                        (code,)).fetchone():
        raise L.LedgerError(f"{code} is not available")


# ---------- setup by admin ----------

def add_currency(conn, code, *, admin_id):
    code = code.strip().upper()
    with L._write(conn):
        L._require_admin(conn, admin_id)
        conn.execute("INSERT INTO currencies(code) VALUES (?)", (code,))
        L._log(conn, admin_id, "add_currency", code)
    return code


def set_price(conn, currency, rate, *, admin_id, now=None):
    """rate = LYD for 1 unit of the currency, up to 6 decimals, e.g. '7.85'.
    Every price ever set is kept."""
    r = _decimal(rate, 6, "rate")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        _active_currency(conn, currency)
        cur = conn.execute(
            "INSERT INTO prices(currency, rate, set_by, set_at) VALUES (?,?,?,?)",
            (currency, str(r), admin_id, _now(now).isoformat()))
        L._log(conn, admin_id, "set_price", currency, rate=str(r))
        return cur.lastrowid


def current_price(conn, currency):
    row = conn.execute(
        "SELECT rate, set_at FROM prices WHERE currency = ? ORDER BY id DESC LIMIT 1",
        (currency,)).fetchone()
    return dict(row) if row else None


def add_supplier(conn, name, country=None, *, admin_id):
    with L._write(conn):
        L._require_admin(conn, admin_id)
        cur = conn.execute("INSERT INTO suppliers(name, country) VALUES (?,?)",
                           (name.strip(), country))
        L._log(conn, admin_id, "add_supplier", cur.lastrowid, name=name, country=country)
        return cur.lastrowid


# ---------- worker side ----------

def get_quote(conn, customer_id, currency, foreign_amount, *, now=None):
    """Price an order and lock that price for QUOTE_MINUTES."""
    amount = _decimal(foreign_amount, 2, "amount")
    now = _now(now)
    with L._write(conn):
        L._customer_account(conn, customer_id)
        _active_currency(conn, currency)
        price = current_price(conn, currency)
        if not price:
            raise L.LedgerError(f"no price set for {currency}")
        rate = Decimal(price["rate"])
        with localcontext() as ctx:
            ctx.prec = 60   # exact for any amount and rate that pass _decimal
            cost = int((amount * rate * L.DIRHAMS_PER_LYD)
                       .quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if cost <= 0:
            raise L.InvalidAmount("amount too small")
        if cost > L.MAX_DIRHAMS:
            raise L.InvalidAmount("amount too large")
        quote_id = secrets.token_urlsafe(12)
        expires = now + timedelta(minutes=QUOTE_MINUTES)
        conn.execute(
            "INSERT INTO quotes(id, customer_id, currency, foreign_amount, rate, cost, "
            "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (quote_id, customer_id, currency, str(amount), str(rate), cost,
             now.isoformat(), expires.isoformat()))
    return {"quote_id": quote_id, "currency": currency, "amount": str(amount),
            "rate": str(rate), "cost": cost, "expires_at": expires.isoformat()}


def _new_receipt_code(conn):
    for _ in range(10):
        code = "".join(secrets.choice(RECEIPT_ALPHABET) for _ in range(RECEIPT_LENGTH))
        if not conn.execute("SELECT 1 FROM orders WHERE receipt_code = ?",
                            (code,)).fetchone():
            return code
    raise L.LedgerError("could not create a receipt code")


def place_order(conn, customer_id, quote_id, beneficiary_name, beneficiary_id, *,
                now=None):
    """Charge the quote's LYD cost and issue a receipt code. Placing the same
    quote twice (double tap) returns the first order and charges once."""
    name = (beneficiary_name or "").strip()
    id_number = (beneficiary_id or "").strip()
    if not name or not id_number:
        raise L.LedgerError("beneficiary name and ID are required")
    now = _now(now)
    with L._write(conn):
        q = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if not q or q["customer_id"] != customer_id:
            raise L.LedgerError("quote not found")
        existing = conn.execute("SELECT * FROM orders WHERE quote_id = ?",
                                (quote_id,)).fetchone()
        if existing:
            return dict(existing)
        if now >= datetime.fromisoformat(q["expires_at"]):
            raise QuoteExpired("price expired, get a new one")
        _active_currency(conn, q["currency"])
        receipt = _new_receipt_code(conn)
        txn = L._charge_order(conn, customer_id, q["cost"], order_ref=receipt,
                              idempotency_key=f"quote:{quote_id}")
        cur = conn.execute(
            "INSERT INTO orders(receipt_code, quote_id, customer_id, currency, "
            "foreign_amount, rate, cost, beneficiary_name, beneficiary_id, status, "
            "charge_txn_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,'placed',?,?)",
            (receipt, quote_id, customer_id, q["currency"], q["foreign_amount"],
             q["rate"], q["cost"], name, id_number, txn, now.isoformat()))
        return dict(_get_order(conn, cur.lastrowid))


# ---------- admin side ----------

def assign_supplier(conn, order_id, supplier_id, *, admin_id, now=None):
    with L._write(conn):
        L._require_admin(conn, admin_id)
        o = _get_order(conn, order_id)
        if o["status"] not in ("placed", "assigned"):
            raise L.LedgerError(f"order is {o['status']}")
        if not conn.execute("SELECT 1 FROM suppliers WHERE id = ? AND active = 1",
                            (supplier_id,)).fetchone():
            raise L.LedgerError("supplier not found")
        conn.execute(
            "UPDATE orders SET status = 'assigned', supplier_id = ?, assigned_at = ? "
            "WHERE id = ?", (supplier_id, _now(now).isoformat(), order_id))
        L._log(conn, admin_id, "assign_supplier", order_id, supplier_id=supplier_id)


def mark_paid(conn, receipt_code, payout_ref, *, admin_id, now=None):
    """Record that the supplier paid the beneficiary. Works once per order."""
    ref = (payout_ref or "").strip()
    if not ref:
        raise L.LedgerError("the supplier's confirmation reference is required")
    code = (receipt_code or "").strip().upper()
    with L._write(conn):
        L._require_admin(conn, admin_id)
        o = conn.execute("SELECT * FROM orders WHERE receipt_code = ?",
                         (code,)).fetchone()
        if not o:
            raise L.LedgerError("receipt code not found")
        if o["status"] == "placed":
            raise L.LedgerError("assign a supplier first")
        if o["status"] != "assigned":
            raise L.LedgerError(f"order is already {o['status']}")
        conn.execute(
            "UPDATE orders SET status = 'paid', payout_ref = ?, paid_at = ? WHERE id = ?",
            (ref, _now(now).isoformat(), o["id"]))
        L._log(conn, admin_id, "mark_paid", o["id"], receipt=code, payout_ref=ref)


def cancel_order(conn, order_id, *, admin_id, reason, now=None):
    """Cancel an unpaid order and return the LYD to the worker's balance."""
    with L._write(conn):
        L._require_admin(conn, admin_id)
        o = _get_order(conn, order_id)
        if o["status"] not in ("placed", "assigned"):
            raise L.LedgerError(f"order is {o['status']}")
        rev = L._reverse(conn, o["charge_txn_id"], admin_id, reason)
        conn.execute(
            "UPDATE orders SET status = 'cancelled', reversal_txn_id = ?, "
            "cancelled_at = ? WHERE id = ?", (rev, _now(now).isoformat(), order_id))
        L._log(conn, admin_id, "cancel_order", order_id, reason=reason)
        return rev


def find_by_receipt(conn, receipt_code):
    row = conn.execute("SELECT * FROM orders WHERE receipt_code = ?",
                       ((receipt_code or "").strip().upper(),)).fetchone()
    return dict(row) if row else None


# ---------- checks ----------

def verify(conn):
    """Ledger check plus order check. Empty list = healthy."""
    problems = L.verify_ledger(conn)
    held = L.account_balance(conn, "SYS_ORDERS")
    open_total = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM orders WHERE status <> 'cancelled'"
    ).fetchone()[0]
    if held != open_total:
        problems.append(f"orders account holds {held} but orders total {open_total}")
    for o in conn.execute(
            "SELECT o.id, o.status, EXISTS (SELECT 1 FROM transactions t "
            "WHERE t.reverses_txn_id = o.charge_txn_id) AS reversed FROM orders o"):
        if o["status"] == "cancelled" and not o["reversed"]:
            problems.append(f"order {o['id']} is cancelled but was not refunded")
        if o["status"] != "cancelled" and o["reversed"]:
            problems.append(f"order {o['id']} was refunded but is still {o['status']}")
    return problems
