"""What the web app calls. Every function takes the caller's session token and
acts as that user, so nobody can move another worker's money by sending a
different user id. Amounts come in as typed text ("12.5") and go out as both
dirhams and formatted LYD.

Admin screens call the ledger/orders functions directly with
admin_id=admin_id(conn, token).
"""
from . import auth as A
from . import ledger as L
from . import orders as O


def _worker(conn, token):
    user = A.authenticate(conn, token)
    if user["role"] != "customer":
        raise L.PermissionDenied("this is for workers' accounts")
    return user


def admin_id(conn, token):
    """The logged-in admin's id, for admin actions. Refuses anyone else."""
    user = A.authenticate(conn, token)
    if user["role"] != "admin":
        raise L.PermissionDenied("admin only")
    return user["id"]


def _money(dirhams):
    return {"dirhams": dirhams, "lyd": L.format_lyd(dirhams)}


# ---------- worker: account ----------

def me(conn, token):
    user = _worker(conn, token)
    return {"name": user["name"], "phone": user["phone"], "whatsapp": user["whatsapp"],
            "language": user["language"], "balance": _money(L.balance(conn, user["id"]))}


def my_statement(conn, token, limit=50):
    user = _worker(conn, token)
    rows = L.statement(conn, user["id"], min(int(limit), 200))
    for row in rows:
        row["amount_lyd"] = L.format_lyd(row["amount"])
        row["balance_after_lyd"] = L.format_lyd(row["balance_after"])
        if row["kind"] == "reversal":
            row["note"] = _reversal_note(conn, row["txn_id"])
    return rows


def _reversal_note(conn, txn_id):
    """What a worker sees for a cancelled order or a correction, instead of the
    admin's reason, which can name a supplier or a rate. The reason stays in
    the admin log and on the admin's view of the account."""
    row = conn.execute(
        "SELECT r.kind FROM transactions t JOIN transactions r ON r.id = t.reverses_txn_id "
        "WHERE t.id = ?", (txn_id,)).fetchone()
    return "Order cancelled" if row and row["kind"] == "order" else "Correction by admin"


# ---------- worker: sending ----------

def _recipient(conn, phone, sender_id):
    other = L.find_user_by_phone(conn, phone)
    if other is None or other["role"] != "customer" or not other["active"]:
        raise L.UnknownAccount("no worker with that phone number")
    if other["id"] == sender_id:
        raise L.LedgerError("cannot send to yourself")
    return other


def lookup_recipient(conn, token, phone):
    """Name to show before the worker confirms a transfer."""
    user = _worker(conn, token)
    other = _recipient(conn, phone, user["id"])
    return {"name": other["name"], "phone": other["phone"]}


def send_money(conn, token, to_phone, amount, *, idempotency_key, note=None):
    """Send LYD to another worker. The app must create one idempotency key per
    confirm screen, so a double tap or a retry can't send twice."""
    user = _worker(conn, token)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise L.LedgerError("idempotency key is required")
    other = _recipient(conn, to_phone, user["id"])
    txn = L.transfer(conn, user["id"], other["id"], L.parse_lyd(amount),
                     idempotency_key=idempotency_key, note=note)
    return {"txn_id": txn, "to": other["name"],
            "balance": _money(L.balance(conn, user["id"]))}


# ---------- worker: currency orders ----------

def prices(conn, token):
    A.authenticate(conn, token)
    rows = conn.execute("SELECT code FROM currencies WHERE active = 1 ORDER BY code")
    out = []
    for row in rows:
        price = O.current_price(conn, row["code"])
        if price:
            out.append({"currency": row["code"], "rate": price["rate"],
                        "set_at": price["set_at"]})
    return out


def get_quote(conn, token, currency, amount):
    user = _worker(conn, token)
    quote = O.get_quote(conn, user["id"], (currency or "").strip().upper(), amount)
    quote["cost_lyd"] = L.format_lyd(quote["cost"])
    return quote


def place_order(conn, token, quote_id, beneficiary_name, beneficiary_id):
    user = _worker(conn, token)
    return _order_view(O.place_order(conn, user["id"], quote_id, beneficiary_name,
                                     beneficiary_id))


def my_orders(conn, token, limit=50):
    user = _worker(conn, token)
    rows = conn.execute(
        "SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC LIMIT ?",
        (user["id"], min(int(limit), 200))).fetchall()
    return [_order_view(dict(r)) for r in rows]


def _order_view(order):
    """What a worker may see of an order: not supplier, supplier cost or margin."""
    keep = ("id", "receipt_code", "currency", "foreign_amount", "rate", "cost",
            "beneficiary_name", "beneficiary_id", "status", "created_at", "paid_at",
            "cancelled_at")
    view = {k: order[k] for k in keep}
    view["cost_lyd"] = L.format_lyd(order["cost"])
    return view
