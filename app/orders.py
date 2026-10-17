"""Foreign currency orders.

Flow: admin sets a price -> worker gets a quote, locked for 10 minutes -> worker
places the order with the beneficiary's name and ID -> the LYD leaves the balance
at once and a receipt code is issued -> admin assigns a supplier -> admin marks it
paid with the supplier's confirmation reference. Paid and cancelled orders are final.
An order with a supplier can't be cancelled or given to another supplier
directly: first unassign it (after checking with the supplier that they have
not paid), then cancel or assign again. A blocked worker's orders are held: no
supplier is assigned until they are unblocked. A payout the supplier already
made is still recorded (mark_paid), so the books say what really happened,
also on an order unassigned meanwhile (mark_paid with that supplier_id).
set_supplier_active switches a supplier off: no new orders are assigned to
it, while its open orders can still be paid, unassigned and settled.

A wrong price: set the right one with void_quotes=True, which also makes every
quote taken before it unusable (by default a quote keeps its price for its 10
minutes). set_currency_active switches a currency off at once: no quotes or
orders for it, even on quotes already issued, until it is switched back on.

Money: placing an order moves its cost from the worker to SYS_ORDERS. Assigning a
supplier locks the supplier's agreed rate onto the order (supplier_cost). Marking
it paid moves the cost out of SYS_ORDERS: supplier_cost to the supplier's account
(what we now owe them) and the rest to SYS_MARGIN (negative if we lost on it).
Paying the supplier from the bank or a till is recorded with pay_supplier.
"""
import json
import re
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
  set_at TEXT NOT NULL,
  voids_quotes INTEGER NOT NULL DEFAULT 0   -- quotes from earlier prices can't be used
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
  price_id INTEGER NOT NULL REFERENCES prices(id),
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


def _utc(when):
    """The time in UTC. A time without a zone is taken as UTC, never as the
    server's local time (which is what astimezone would assume)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _now(now):
    """Always UTC, so stored times compare correctly as text."""
    return _utc(now or datetime.now(timezone.utc))


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
    # One text per value, in plain digits and at least 2 decimals, so what is
    # stored and shown never depends on how it was typed: '1E+1' -> '10.00',
    # '7.850' -> '7.85'.
    d = d.normalize()
    return d if d.as_tuple().exponent <= -2 else d.quantize(Decimal("0.01"))


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
        raise L.NotFound("order not found")
    return row


def _customer_not_blocked(conn, order):
    """A blocked worker's orders are held: no supplier is assigned until they
    are unblocked. Unassigning, cancelling (a refund) and recording a payout
    the supplier already made still work."""
    if conn.execute("SELECT blocked FROM users WHERE id = ?",
                    (order["customer_id"],)).fetchone()["blocked"]:
        raise L.AccountBlocked("the worker is blocked: no supplier can be assigned until "
                               "they are unblocked; if a supplier already paid this "
                               "order, mark it paid with that supplier_id")


def _active_currency(conn, code):
    if not conn.execute("SELECT 1 FROM currencies WHERE code = ? AND active = 1",
                        (code,)).fetchone():
        raise L.LedgerError(f"{code} is not available")


def _currency_code(code):
    """'usd ' -> 'USD'. Exactly 3 letters A-Z (checked before upper-casing, which
    turns some other letters into A-Z)."""
    code = code.strip() if isinstance(code, str) else ""
    if not re.fullmatch(r"[A-Za-z]{3}", code):
        raise L.LedgerError("currency code must be 3 letters, e.g. USD")
    return code.upper()


# ---------- setup by admin ----------

def add_currency(conn, code, *, admin_id):
    code = _currency_code(code)
    with L._write(conn):
        L._require_admin(conn, admin_id)
        if conn.execute("SELECT 1 FROM currencies WHERE code = ?", (code,)).fetchone():
            raise L.Conflict(f"{code} already exists")
        conn.execute("INSERT INTO currencies(code) VALUES (?)", (code,))
        L._log(conn, admin_id, "add_currency", code)
    return code


def set_currency_active(conn, code, active, *, admin_id, reason):
    """Switch a currency off (active=False) or back on. While it is off workers
    can't see its price, get quotes or place orders, not even on quotes taken
    before. Orders already placed are not touched. The reason is logged."""
    code = _currency_code(code)
    if not isinstance(active, bool):
        raise L.LedgerError("active must be true or false")
    reason = L._reason(reason)
    with L._write(conn):
        L._require_admin(conn, admin_id)
        row = conn.execute("SELECT active FROM currencies WHERE code = ?",
                           (code,)).fetchone()
        if not row:
            raise L.LedgerError(f"no currency {code}")
        if bool(row["active"]) == active:
            raise L.LedgerError(f"{code} is already {'on' if active else 'off'}")
        conn.execute("UPDATE currencies SET active = ? WHERE code = ?", (int(active), code))
        L._log(conn, admin_id, "set_currency_active", code, active=active, reason=reason)


def set_price(conn, currency, rate, *, admin_id, now=None, void_quotes=False):
    """rate = LYD for 1 unit of the currency, up to 6 decimals, e.g. '7.85'.
    Every price ever set is kept. Quotes keep their price for QUOTE_MINUTES,
    unless void_quotes=True: then no quote taken before this price can be used
    (to correct a wrong price). Can be set while the currency is switched off."""
    currency = _currency_code(currency)
    r = _decimal(rate, 6, "rate")
    if not isinstance(void_quotes, bool):
        raise L.LedgerError("void_quotes must be true or false")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        if not conn.execute("SELECT 1 FROM currencies WHERE code = ?",
                            (currency,)).fetchone():
            raise L.LedgerError(f"{currency} is not available")
        cur = conn.execute(
            "INSERT INTO prices(currency, rate, set_by, set_at, voids_quotes) "
            "VALUES (?,?,?,?,?)",
            (currency, str(r), admin_id, _now(now).isoformat(), int(void_quotes)))
        L._log(conn, admin_id, "set_price", currency, rate=str(r), void_quotes=void_quotes)
        return cur.lastrowid


def current_price(conn, currency):
    row = conn.execute(
        "SELECT id, rate, set_at FROM prices WHERE currency = ? ORDER BY id DESC LIMIT 1",
        (currency,)).fetchone()
    return dict(row) if row else None


def add_supplier(conn, name, country=None, *, admin_id):
    """A supplier who pays beneficiaries abroad. Gets a ledger account SUP_<id>.
    Names are unique (ignoring case), so a double submit can't make two."""
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise L.LedgerError("supplier name is required")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        if conn.execute("SELECT 1 FROM suppliers WHERE lower(name) = lower(?)",
                        (name,)).fetchone():
            raise L.Conflict(f"a supplier called {name} already exists")
        cur = conn.execute("INSERT INTO suppliers(name, country) VALUES (?,?)",
                           (name, country))
        supplier_id = cur.lastrowid
        conn.execute("INSERT INTO accounts(kind, code) VALUES ('supplier', ?)",
                     (f"SUP_{supplier_id}",))
        L._log(conn, admin_id, "add_supplier", supplier_id, name=name, country=country)
        return supplier_id


def set_supplier_active(conn, supplier_id, active, *, admin_id, reason):
    """Switch a supplier off (active=False), e.g. one that stopped working or
    is not trusted, or back on. While it is off no order can be assigned to
    it; its orders already assigned can still be marked paid or unassigned,
    and it can still be paid (pay_supplier). The reason is logged."""
    if not isinstance(active, bool):
        raise L.LedgerError("active must be true or false")
    reason = L._reason(reason)
    with L._write(conn):
        L._require_admin(conn, admin_id)
        row = conn.execute("SELECT active FROM suppliers WHERE id = ?",
                           (supplier_id,)).fetchone()
        if not row:
            raise L.NotFound("supplier not found")
        if bool(row["active"]) == active:
            raise L.LedgerError(f"the supplier is already {'on' if active else 'off'}")
        conn.execute("UPDATE suppliers SET active = ? WHERE id = ?",
                     (int(active), supplier_id))
        L._log(conn, admin_id, "set_supplier_active", supplier_id, active=active,
               reason=reason)


def set_supplier_price(conn, supplier_id, currency, rate, *, admin_id, now=None):
    """rate = LYD this supplier charges us for 1 unit of the currency, up to 6
    decimals. Every rate ever agreed is kept. Can be set while the currency is
    switched off, so orders placed before are assigned at the right rate."""
    currency = _currency_code(currency)
    r = _decimal(rate, 6, "rate")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        if not conn.execute("SELECT 1 FROM currencies WHERE code = ?",
                            (currency,)).fetchone():
            raise L.LedgerError(f"{currency} is not available")
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
        L._usable_account(conn, customer_id)
        _active_currency(conn, currency)
        price = current_price(conn, currency)
        if not price:
            raise L.LedgerError(f"no price set for {currency}")
        rate = Decimal(price["rate"])
        cost = _lyd_cost(amount, rate)
        quote_id = secrets.token_urlsafe(12)
        expires = now + timedelta(minutes=QUOTE_MINUTES)
        conn.execute(
            "INSERT INTO quotes(id, customer_id, currency, foreign_amount, rate, price_id, "
            "cost, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (quote_id, customer_id, currency, str(amount), str(rate), price["id"], cost,
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
    quote twice (double tap) returns the first order and charges once; the same
    quote with other beneficiary details is refused (Conflict)."""
    name = (beneficiary_name or "").strip()
    id_number = (beneficiary_id or "").strip()
    if not name or not id_number:
        raise L.LedgerError("beneficiary name and ID are required")
    now = _now(now)
    with L._write(conn):
        q = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if not q or q["customer_id"] != customer_id:
            raise L.NotFound("quote not found")
        existing = conn.execute("SELECT * FROM orders WHERE quote_id = ?",
                                (quote_id,)).fetchone()
        if existing:
            # A retry must be the same order: other beneficiary details would be
            # silently dropped if we answered with the first one.
            if (existing["beneficiary_name"], existing["beneficiary_id"]) != \
                    (name, id_number):
                raise L.Conflict("this quote was already used for an order to another "
                                 "beneficiary; get a new quote")
            return dict(existing)
        if now >= datetime.fromisoformat(q["expires_at"]):
            raise QuoteExpired("price expired, get a new one")
        _active_currency(conn, q["currency"])
        if conn.execute("SELECT 1 FROM prices WHERE currency = ? AND id > ? "
                        "AND voids_quotes = 1", (q["currency"], q["price_id"])).fetchone():
            raise QuoteExpired("the price was corrected, get a new one")
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

def _lock_supplier(conn, order, supplier_id, rate, now):
    """Put a placed order with a supplier at what they charge for it: `rate`
    (a one-off deal) or their current agreed rate for the currency. Returns
    (rate, supplier_cost)."""
    if rate is None:
        price = current_supplier_price(conn, supplier_id, order["currency"])
        if not price:
            raise L.LedgerError(f"no agreed {order['currency']} rate with this supplier")
        rate = Decimal(price["rate"])
    supplier_cost = _lyd_cost(order["foreign_amount"], rate)
    conn.execute(
        "UPDATE orders SET status = 'assigned', supplier_id = ?, supplier_rate = ?, "
        "supplier_cost = ?, assigned_at = ? WHERE id = ?",
        (supplier_id, str(rate), supplier_cost, _now(now).isoformat(), order["id"]))
    return rate, supplier_cost


def _rate_when_assigned(conn, order_id, supplier_id):
    """The rate locked when `supplier_id` last held this order (the admin log
    keeps it after an unassign), or None if it never did."""
    for row in conn.execute(
            "SELECT details FROM admin_log WHERE action = 'assign_supplier' AND target = ? "
            "ORDER BY id DESC", (str(order_id),)):
        details = json.loads(row["details"])
        if details.get("supplier_id") == supplier_id:
            return Decimal(details["rate"])
    return None


def assign_supplier(conn, order_id, supplier_id, *, admin_id, rate=None, now=None):
    """Give a placed order to a supplier and lock what they charge for it: `rate`
    if given (a one-off deal), otherwise their current agreed rate for the
    currency. An order already with a supplier is refused: unassign_supplier it
    first (with a reason, after checking they have not paid). Refused while the
    worker is blocked, and for a supplier that is switched off."""
    r = None if rate is None else _decimal(rate, 6, "rate")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        o = _get_order(conn, order_id)
        if o["status"] == "assigned":
            raise L.Conflict("order is already with a supplier: check they have not "
                             "paid, unassign it with a reason, then assign it again")
        if o["status"] != "placed":
            raise L.LedgerError(f"order is {o['status']}")
        _customer_not_blocked(conn, o)
        supplier = conn.execute("SELECT active FROM suppliers WHERE id = ?",
                                (supplier_id,)).fetchone()
        if not supplier:
            raise L.NotFound("supplier not found")
        if not supplier["active"]:
            raise L.LedgerError("this supplier is switched off")
        r, supplier_cost = _lock_supplier(conn, o, supplier_id, r, now)
        L._log(conn, admin_id, "assign_supplier", order_id, supplier_id=supplier_id,
               rate=str(r), supplier_cost=supplier_cost)


def mark_paid(conn, receipt_code, payout_ref, *, admin_id, supplier_id=None, rate=None,
              now=None):
    """Record that the supplier paid the beneficiary. Works once per order.
    `supplier_id`, if given, is the supplier whose confirmation this is: refused
    (Conflict) if the order is with another supplier by now. Also while the
    worker is blocked: the money has left by then, so it is recorded (and the
    log says the worker was blocked); refusing it would leave the books wrong.
    An order with no supplier (placed, e.g. unassigned on the supplier's word
    that it had not paid, then it sends proof it did) is marked paid only with
    that `supplier_id`, at `rate` if given (a one-off deal), otherwise the rate
    locked when that supplier last held the order (the payout was made at it),
    otherwise its agreed rate; it is locked onto the order as assign_supplier
    does, in the same step. That orders no new payout, so it works while the
    worker is blocked, and for a supplier that is switched off."""
    ref = (payout_ref or "").strip()
    if not ref:
        raise L.LedgerError("the supplier's confirmation reference is required")
    code = (receipt_code or "").strip().upper()
    r = None if rate is None else _decimal(rate, 6, "rate")
    with L._write(conn):
        L._require_admin(conn, admin_id)
        o = conn.execute("SELECT * FROM orders WHERE receipt_code = ?",
                         (code,)).fetchone()
        if not o:
            raise L.NotFound("receipt code not found")
        if o["status"] not in ("placed", "assigned"):
            raise L.LedgerError(f"order is already {o['status']}")
        assigned_now = {}
        if o["status"] == "placed":
            if supplier_id is None:
                raise L.LedgerError("assign a supplier first, or give the supplier_id "
                                    "whose confirmation this is")
            if not conn.execute("SELECT 1 FROM suppliers WHERE id = ?",
                                (supplier_id,)).fetchone():
                raise L.NotFound("supplier not found")
            rate_from = "one-off"
            if r is None:
                r = _rate_when_assigned(conn, o["id"], supplier_id)
                rate_from = "agreed" if r is None else "assignment"
            r, cost = _lock_supplier(conn, o, supplier_id, r, now)
            assigned_now = {"assigned_with_payout": True, "rate": str(r),
                            "rate_from": rate_from, "supplier_cost": cost}
            o = _get_order(conn, o["id"])
        elif r is not None:
            raise L.LedgerError("this order's supplier rate was set when it was assigned")
        if supplier_id is not None and o["supplier_id"] != supplier_id:
            raise L.Conflict("this order is with another supplier now: check which "
                             "supplier paid it before marking it paid")
        margin = o["cost"] - o["supplier_cost"]
        txn = L._post(conn, "payout",
                      [(L._system(conn, "SYS_ORDERS"), -o["cost"]),
                       (_supplier_account(conn, o["supplier_id"]), o["supplier_cost"]),
                       (L._system(conn, "SYS_MARGIN"), margin)],
                      admin_id, note=ref, reference=code)
        conn.execute(
            "UPDATE orders SET status = 'paid', payout_ref = ?, paid_at = ?, "
            "payout_txn_id = ? WHERE id = ?", (ref, _now(now).isoformat(), txn, o["id"]))
        blocked = conn.execute("SELECT blocked FROM users WHERE id = ?",
                               (o["customer_id"],)).fetchone()["blocked"]
        L._log(conn, admin_id, "mark_paid", o["id"], receipt=code, payout_ref=ref,
               supplier_id=o["supplier_id"], margin=margin, customer_blocked=bool(blocked),
               **assigned_now)
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
                             supplier_id=supplier_id, amount=amount, from_code=from_code,
                             reference=ref)
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
    orders, foreign total, charged to workers, supplier cost and margin (dirhams).
    A since/until without a time zone is UTC."""
    sql = ("SELECT o.currency, o.supplier_id, s.name AS supplier, "
           "o.foreign_amount, o.cost, o.supplier_cost "
           "FROM orders o JOIN suppliers s ON s.id = o.supplier_id WHERE o.status = 'paid'")
    args = []
    if since is not None:
        sql += " AND o.paid_at >= ?"
        args.append(_utc(since).isoformat())
    if until is not None:
        sql += " AND o.paid_at < ?"
        args.append(_utc(until).isoformat())
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


def unassign_supplier(conn, order_id, *, admin_id, reason, now=None):
    """Take an unpaid order back from its supplier. Only after confirming with
    them that they have not paid it out; the reason is logged."""
    reason = L._reason(reason)
    with L._write(conn):
        L._require_admin(conn, admin_id)
        o = _get_order(conn, order_id)
        if o["status"] != "assigned":
            raise L.LedgerError(f"order is {o['status']}, not with a supplier")
        conn.execute(
            "UPDATE orders SET status = 'placed', supplier_id = NULL, supplier_rate = NULL, "
            "supplier_cost = NULL, assigned_at = NULL WHERE id = ?", (order_id,))
        L._log(conn, admin_id, "unassign_supplier", order_id,
               supplier_id=o["supplier_id"], rate=o["supplier_rate"],
               supplier_cost=o["supplier_cost"], reason=reason)


def cancel_order(conn, order_id, *, admin_id, reason, now=None):
    """Cancel an order no supplier has yet and return the LYD to the worker
    (also while they are blocked: it stays frozen on their balance). For an
    order with a supplier, unassign_supplier first. The reason is for admins:
    the worker's statement shows only that the order was cancelled."""
    with L._write(conn):
        L._require_admin(conn, admin_id)
        o = _get_order(conn, order_id)
        if o["status"] == "assigned":
            raise L.Conflict("order is with a supplier: check they have not paid, "
                             "unassign it, then cancel")
        if o["status"] != "placed":
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
    """Ledger check plus order check. Empty list = healthy. It all reads one
    snapshot, so orders placed or paid meanwhile can't show as a mismatch."""
    with L._read(conn):
        problems = L.verify_ledger(conn)
        held = L.account_balance(conn, "SYS_ORDERS")
        open_total = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM orders "
            "WHERE status IN ('placed','assigned')").fetchone()[0]
        if held != open_total:
            problems.append(f"orders account holds {held} but open orders total "
                            f"{open_total}")
        margin = L.account_balance(conn, "SYS_MARGIN")
        paid_margin = conn.execute(
            "SELECT COALESCE(SUM(cost - supplier_cost), 0) FROM orders "
            "WHERE status = 'paid'").fetchone()[0]
        if margin != paid_margin:
            problems.append(f"margin account holds {margin} but paid orders made "
                            f"{paid_margin}")
        for s in conn.execute(
                "SELECT s.id, COALESCE((SELECT SUM(supplier_cost) FROM orders o "
                " WHERE o.supplier_id = s.id AND o.status = 'paid'), 0) "
                " AS owed_for_orders, "
                "COALESCE((SELECT -SUM(e.amount) FROM entries e JOIN transactions t "
                " ON t.id = e.txn_id WHERE e.account_id = a.id AND t.kind <> 'payout'), "
                " 0) AS paid, "
                "COALESCE((SELECT SUM(e.amount) FROM entries e "
                " WHERE e.account_id = a.id), 0) AS balance "
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
                problems.append(f"order {o['id']} was refunded but is still "
                                f"{o['status']}")
        return problems
