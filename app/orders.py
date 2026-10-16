"""Foreign currency orders.

Flow: admin sets a price -> worker gets a quote, locked for 10 minutes -> worker
places the order with the beneficiary's name and ID -> the LYD leaves the balance
at once and a receipt code is issued -> admin assigns a supplier -> admin marks it
paid with the supplier's confirmation reference. Paid and cancelled orders are final.

Money: placing an order moves its cost from the worker to SYS_ORDERS. Assigning a
supplier locks the supplier's agreed rate onto the order (supplier_cost). Marking
it paid moves the cost out of SYS_ORDERS: supplier_cost to the supplier's account
(what we now owe them) and the rest to SYS_MARGIN (negative if we lost on it).
Paying the supplier from the bank or a till is recorded with pay_supplier.
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

CREATE TABLE IF NOT EXISTS supplier_prices (
  id INTEGER PRIMARY KEY,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  currency TEXT NOT NULL REFERENCES currencies(code),
  rate TEXT NOT NULL,                 -- LYD the supplier charges for 1 unit
  set_by INTEGER NOT NULL REFERENCES users(id),
  set_at TEXT NOT NULL
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
  supplier_rate TEXT,
  supplier_cost INTEGER CHECK (supplier_cost > 0),
  charge_txn_id INTEGER NOT NULL REFERENCES transactions(id),
  reversal_txn_id INTEGER REFERENCES transactions(id),
  payout_txn_id INTEGER UNIQUE REFERENCES transactions(id),
  payout_ref TEXT,
  created_at TEXT NOT NULL,
  assigned_at TEXT,
  paid_at TEXT,
  cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_supplier ON orders(supplier_id);

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
CREATE TRIGGER IF NOT EXISTS orders_paid_needs_payout BEFORE UPDATE ON orders
WHEN NEW.status = 'paid' AND (NEW.payout_txn_id IS NULL OR NEW.supplier_cost IS NULL)
BEGIN SELECT RAISE(ABORT, 'paid order needs its payout posting'); END;
CREATE TRIGGER IF NOT EXISTS supplier_prices_no_update BEFORE UPDATE ON supplier_prices
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS supplier_prices_no_delete BEFORE DELETE ON supplier_prices
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
    """Always UTC, so stored times compare correctly as text."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


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


def _lyd_cost(foreign_amount, rate):
    """Dirhams for foreign_amount at rate (LYD per unit), rounded half up."""
    with localcontext() as ctx:
        ctx.prec = 60   # exact for any amount and rate that pass _decimal
        cost = int((Decimal(foreign_amount) * Decimal(rate) * L.DIRHAMS_PER_LYD)
                   .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if cost <= 0:
        raise L.InvalidAmount("amount too small")
    if cost > L.MAX_DIRHAMS:
        raise L.InvalidAmount("amount too large")
    return cost


def _supplier_account(conn, supplier_id):
    row = conn.execute("SELECT id FROM accounts WHERE code = ? AND kind = 'supplier'",
                       (f"SUP_{supplier_id}",)).fetchone()
    if not row:
        raise L.UnknownAccount(f"no account for supplier {supplier_id}")
    return row["id"]


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
    """A supplier who pays beneficiaries abroad. Gets a ledger account SUP_<id>."""
    with L._write(conn):
        L._require_admin(conn, admin_id)
        cur = conn.execute("INSERT INTO suppliers(name, country) VALUES (?,?)",
                           (name.strip(), country))
        supplier_id = cur.lastrowid
        conn.execute("INSERT INTO accounts(kind, code) VALUES ('supplier', ?)",
                     (f"SUP_{supplier_id}",))
        L._log(conn, admin_id, "add_supplier", supplier_id, name=name, country=country)
        return supplier_id


def set_supplier_price(conn, supplier_id, currency, rate, *, admin_id, now=None):
    """rate = LYD this supplier charges us for 1 unit of the currency, up to 6
    decimals. Every rate ever agreed is kept."""
    r = _decimal(rate, 6, "rate")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        _active_currency(conn, currency)
        _supplier_account(conn, supplier_id)
        cur = conn.execute(
            "INSERT INTO supplier_prices(supplier_id, currency, rate, set_by, set_at) "
            "VALUES (?,?,?,?,?)", (supplier_id, currency, str(r), admin_id,
                                   _now(now).isoformat()))
        L._log(conn, admin_id, "set_supplier_price", supplier_id, currency=currency,
               rate=str(r))
        return cur.lastrowid


def current_supplier_price(conn, supplier_id, currency):
    row = conn.execute(
        "SELECT rate, set_at FROM supplier_prices WHERE supplier_id = ? AND currency = ? "
        "ORDER BY id DESC LIMIT 1", (supplier_id, currency)).fetchone()
    return dict(row) if row else None


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
        cost = _lyd_cost(amount, rate)
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

def assign_supplier(conn, order_id, supplier_id, *, admin_id, rate=None, now=None):
    """Give the order to a supplier and lock what they charge for it: `rate` if
    given (a one-off deal), otherwise their current agreed rate for the currency.
    Can be called again to move an unpaid order to another supplier."""
    r = None if rate is None else _decimal(rate, 6, "rate")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        o = _get_order(conn, order_id)
        if o["status"] not in ("placed", "assigned"):
            raise L.LedgerError(f"order is {o['status']}")
        if not conn.execute("SELECT 1 FROM suppliers WHERE id = ? AND active = 1",
                            (supplier_id,)).fetchone():
            raise L.LedgerError("supplier not found")
        if r is None:
            price = current_supplier_price(conn, supplier_id, o["currency"])
            if not price:
                raise L.LedgerError(f"no agreed {o['currency']} rate with this supplier")
            r = Decimal(price["rate"])
        supplier_cost = _lyd_cost(o["foreign_amount"], r)
        conn.execute(
            "UPDATE orders SET status = 'assigned', supplier_id = ?, supplier_rate = ?, "
            "supplier_cost = ?, assigned_at = ? WHERE id = ?",
            (supplier_id, str(r), supplier_cost, _now(now).isoformat(), order_id))
        L._log(conn, admin_id, "assign_supplier", order_id, supplier_id=supplier_id,
               rate=str(r), supplier_cost=supplier_cost)


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
        margin = o["cost"] - o["supplier_cost"]
        txn = L._post(conn, "payout",
                      [(L._system(conn, "SYS_ORDERS"), -o["cost"]),
                       (_supplier_account(conn, o["supplier_id"]), o["supplier_cost"]),
                       (L._system(conn, "SYS_MARGIN"), margin)],
                      admin_id, note=ref, reference=code)
        conn.execute(
            "UPDATE orders SET status = 'paid', payout_ref = ?, paid_at = ?, "
            "payout_txn_id = ? WHERE id = ?", (ref, _now(now).isoformat(), txn, o["id"]))
        L._log(conn, admin_id, "mark_paid", o["id"], receipt=code, payout_ref=ref,
               margin=margin)
        return txn


# ---------- paying suppliers ----------

def pay_supplier(conn, supplier_id, amount, *, admin_id, from_code, reference,
                 idempotency_key=None, note=None):
    """Record LYD paid to a supplier from the bank or a till. Paying more than we
    owe is allowed: the supplier then holds a prepayment (negative balance)."""
    L._check_amount(amount)
    ref = (reference or "").strip()
    if not ref:
        raise L.LedgerError("a payment reference is required")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        idem = L._idempotent(conn, idempotency_key, admin_id, "settlement",
                             supplier_id=supplier_id, amount=amount, from_code=from_code)
        if idem.existing:
            return idem.existing
        txn = L._post(conn, "settlement",
                      [(_supplier_account(conn, supplier_id), -amount),
                       (L._holding(conn, from_code), amount)],
                      admin_id, note=note, reference=ref, idem=idem)
        L._log(conn, admin_id, "pay_supplier", txn, supplier_id=supplier_id,
               amount=amount, source=from_code, reference=ref)
        return txn


def supplier_balance(conn, supplier_id):
    """Dirhams we owe the supplier (negative = they hold a prepayment from us)."""
    _supplier_account(conn, supplier_id)
    return L.account_balance(conn, f"SUP_{supplier_id}")


def supplier_statement(conn, supplier_id, limit=50):
    _supplier_account(conn, supplier_id)
    return L.account_statement(conn, f"SUP_{supplier_id}", limit)


def margin_report(conn, since=None, until=None):
    """Paid orders with paid_at in [since, until), per currency and supplier:
    orders, foreign total, charged to workers, supplier cost and margin (dirhams)."""
    sql = ("SELECT o.currency, o.supplier_id, s.name AS supplier, "
           "o.foreign_amount, o.cost, o.supplier_cost "
           "FROM orders o JOIN suppliers s ON s.id = o.supplier_id WHERE o.status = 'paid'")
    args = []
    if since is not None:
        sql += " AND o.paid_at >= ?"
        args.append(since.astimezone(timezone.utc).isoformat())
    if until is not None:
        sql += " AND o.paid_at < ?"
        args.append(until.astimezone(timezone.utc).isoformat())
    totals = {}
    for o in conn.execute(sql + " ORDER BY o.currency, o.supplier_id", args):
        row = totals.setdefault((o["currency"], o["supplier_id"]), {
            "currency": o["currency"], "supplier_id": o["supplier_id"],
            "supplier": o["supplier"], "orders": 0, "foreign_amount": Decimal(0),
            "charged": 0, "supplier_cost": 0, "margin": 0})
        row["orders"] += 1
        row["foreign_amount"] += Decimal(o["foreign_amount"])
        row["charged"] += o["cost"]
        row["supplier_cost"] += o["supplier_cost"]
        row["margin"] += o["cost"] - o["supplier_cost"]
    rows = list(totals.values())
    for row in rows:
        row["foreign_amount"] = str(row["foreign_amount"].quantize(Decimal("0.01")))
    return rows


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
        "SELECT COALESCE(SUM(cost), 0) FROM orders WHERE status IN ('placed','assigned')"
    ).fetchone()[0]
    if held != open_total:
        problems.append(f"orders account holds {held} but open orders total {open_total}")
    margin = L.account_balance(conn, "SYS_MARGIN")
    paid_margin = conn.execute(
        "SELECT COALESCE(SUM(cost - supplier_cost), 0) FROM orders WHERE status = 'paid'"
    ).fetchone()[0]
    if margin != paid_margin:
        problems.append(f"margin account holds {margin} but paid orders made {paid_margin}")
    for s in conn.execute(
            "SELECT s.id, COALESCE((SELECT SUM(supplier_cost) FROM orders o "
            " WHERE o.supplier_id = s.id AND o.status = 'paid'), 0) AS owed_for_orders, "
            "COALESCE((SELECT -SUM(e.amount) FROM entries e JOIN transactions t "
            " ON t.id = e.txn_id WHERE e.account_id = a.id AND t.kind <> 'payout'), 0) "
            " AS paid, "
            "COALESCE((SELECT SUM(e.amount) FROM entries e WHERE e.account_id = a.id), 0) "
            " AS balance "
            "FROM suppliers s JOIN accounts a ON a.code = 'SUP_' || s.id"):
        if s["balance"] != s["owed_for_orders"] - s["paid"]:
            problems.append(f"supplier {s['id']} balance {s['balance']} does not match "
                            f"orders {s['owed_for_orders']} minus payments {s['paid']}")
    for o in conn.execute(
            "SELECT o.id, o.status, EXISTS (SELECT 1 FROM transactions t "
            "WHERE t.reverses_txn_id = o.charge_txn_id) AS reversed FROM orders o"):
        if o["status"] == "cancelled" and not o["reversed"]:
            problems.append(f"order {o['id']} is cancelled but was not refunded")
        if o["status"] != "cancelled" and o["reversed"]:
            problems.append(f"order {o['id']} was refunded but is still {o['status']}")
    return problems
