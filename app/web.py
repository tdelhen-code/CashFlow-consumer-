"""JSON web API over HTTP, as a WSGI app (standard library only).

Run locally:      python3 -m app.web --db wallet.db --port 8000 --insecure-cookies
In production:    serve `app.web:make_app("wallet.db")` with a WSGI server such as
                  waitress or gunicorn, behind HTTPS (nginx, Caddy, ...).

Rules every request follows:
- Requests and responses are JSON. POST bodies must be sent as
  application/json, which a form on another website can't do, so a logged-in
  browser can't be tricked into sending money (with the SameSite cookie).
- The session is the `session` cookie (HttpOnly, Secure, SameSite=Strict) or
  an `Authorization: Bearer <token>` header for a phone app.
- Amounts are typed text in LYD ("12.5"); answers carry dirhams and LYD text.
- Login and setup-code tries are also limited per internet address.
- Errors come back as {"error": message, "type": kind} with a fitting status;
  unexpected errors say only "internal error" and are logged on the server.
"""
import argparse
import json
import re
import sys
import threading
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server

from . import api
from . import auth as A
from . import ledger as L
from . import orders as O

MAX_BODY_BYTES = 64 * 1024
LOGIN_TRIES_PER_IP = 20
LOGIN_WINDOW_SECONDS = 10 * 60
COOKIE = "session"

ERROR_STATUS = [            # first match wins, so subclasses come first
    (A.LoginFailed, 401), (A.NotLoggedIn, 401), (A.AccountLocked, 429),
    (L.PermissionDenied, 403), (L.AccountBlocked, 403), (L.UnknownAccount, 404),
    (O.QuoteExpired, 409), (L.InsufficientFunds, 409), (L.NotEnoughCash, 409),
    (L.LedgerError, 400),
]


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class RateLimiter:
    """At most `limit` tries per key in a sliding window (in memory, per process)."""

    def __init__(self, limit, seconds, clock=time.monotonic):
        self.limit, self.seconds, self.clock = limit, seconds, clock
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key):
        now = self.clock()
        with self.lock:
            hits = self.hits[key]
            while hits and hits[0] <= now - self.seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                raise HttpError(429, "too many tries from this connection; wait a few minutes")
            hits.append(now)


class Request:
    def __init__(self, environ, trust_proxy):
        self.environ = environ
        self.method = environ["REQUEST_METHOD"]
        self.path = environ.get("PATH_INFO", "/")
        self.query = {k: v[-1] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        self.ip = environ.get("REMOTE_ADDR", "")
        if trust_proxy and environ.get("HTTP_X_FORWARDED_FOR"):
            self.ip = environ["HTTP_X_FORWARDED_FOR"].split(",")[-1].strip()
        self.params = {}
        self.body = self._read_body() if self.method == "POST" else {}

    def _read_body(self):
        if self.environ.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/json":
            raise HttpError(415, "send JSON with Content-Type: application/json")
        try:
            length = int(self.environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise HttpError(400, "bad Content-Length")
        if length > MAX_BODY_BYTES:
            raise HttpError(413, "request too large")
        raw = self.environ["wsgi.input"].read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "body is not valid JSON")
        if not isinstance(body, dict):
            raise HttpError(400, "body must be a JSON object")
        return body

    @property
    def token(self):
        header = self.environ.get("HTTP_AUTHORIZATION", "")
        if header.startswith("Bearer "):
            return header[7:].strip()
        cookie = SimpleCookie()
        try:
            cookie.load(self.environ.get("HTTP_COOKIE", ""))
        except Exception:
            return None
        return cookie[COOKIE].value if COOKIE in cookie else None

    def get(self, name, required=True):
        value = self.body.get(name)
        if required and (value is None or value == ""):
            raise HttpError(400, f"'{name}' is required")
        return value

    def number(self, name):
        """A whole-number field from the JSON body, e.g. user_id."""
        value = self.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise HttpError(400, f"'{name}' must be a whole number")
        try:
            return int(value)
        except ValueError:
            raise HttpError(400, f"'{name}' must be a whole number")

    def int(self, name):
        try:
            return int(self.params[name])
        except (KeyError, ValueError):
            raise HttpError(404, "not found")


ROUTES = []


def route(method, pattern):
    regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[0-9]+)", pattern) + "$")

    def register(handler):
        ROUTES.append((method, regex, handler))
        return handler
    return register


def _money(dirhams):
    return {"dirhams": dirhams, "lyd": L.format_lyd(dirhams)}


def _limit(req, default=50):
    try:
        return max(1, min(int(req.query.get("limit", default)), 200))
    except ValueError:
        raise HttpError(400, "limit must be a number")


# ---------- login ----------

@route("POST", "/api/login")
def login(app, conn, req):
    app.login_limiter.check(req.ip)
    token = A.login(conn, req.get("phone"), req.get("password"))
    return {"ok": True, "token": token}, token


@route("POST", "/api/setup-password")
def setup_password(app, conn, req):
    app.login_limiter.check(req.ip)
    token = A.set_password_with_code(conn, req.get("phone"), req.get("code"),
                                     req.get("password"))
    return {"ok": True, "token": token}, token


@route("POST", "/api/logout")
def logout(app, conn, req):
    A.logout(conn, req.token)
    return {"ok": True}, ""


@route("POST", "/api/logout-everywhere")
def logout_everywhere(app, conn, req):
    A.logout_everywhere(conn, req.token)
    return {"ok": True}, ""


@route("POST", "/api/change-password")
def change_password(app, conn, req):
    A.change_password(conn, req.token, req.get("old_password"), req.get("new_password"))
    return {"ok": True}


# ---------- worker ----------

@route("GET", "/api/me")
def me(app, conn, req):
    return api.me(conn, req.token)


@route("GET", "/api/statement")
def statement(app, conn, req):
    return {"entries": api.my_statement(conn, req.token, _limit(req))}


@route("GET", "/api/recipient")
def recipient(app, conn, req):
    return api.lookup_recipient(conn, req.token, req.query.get("phone", ""))


@route("POST", "/api/send")
def send(app, conn, req):
    return api.send_money(conn, req.token, req.get("to_phone"), req.get("amount"),
                          idempotency_key=req.get("idempotency_key"),
                          note=req.get("note", required=False))


@route("GET", "/api/prices")
def prices(app, conn, req):
    return {"prices": api.prices(conn, req.token)}


@route("POST", "/api/quote")
def quote(app, conn, req):
    return api.get_quote(conn, req.token, req.get("currency"), req.get("amount"))


@route("GET", "/api/orders")
def my_orders(app, conn, req):
    return {"orders": api.my_orders(conn, req.token, _limit(req))}


@route("POST", "/api/orders")
def place_order(app, conn, req):
    return api.place_order(conn, req.token, req.get("quote_id"),
                           req.get("beneficiary_name"), req.get("beneficiary_id"))


# ---------- admin: users ----------

def _admin(conn, req):
    return api.admin_id(conn, req.token)


@route("GET", "/api/admin/users")
def admin_users(app, conn, req):
    _admin(conn, req)
    rows = conn.execute(
        "SELECT u.id, u.role, u.name, u.phone, u.whatsapp, u.language, u.active, "
        "u.blocked, u.blocked_reason, u.blocked_at, u.password_hash IS NOT NULL AS has_password, "
        "COALESCE((SELECT SUM(e.amount) FROM entries e JOIN accounts a "
        "ON a.id = e.account_id WHERE a.user_id = u.id), 0) AS balance "
        "FROM users u ORDER BY u.id").fetchall()
    return {"users": [dict(r, balance_lyd=L.format_lyd(r["balance"])) for r in rows]}


@route("POST", "/api/admin/users")
def admin_create_user(app, conn, req):
    admin = _admin(conn, req)
    user_id = L.create_user(conn, req.get("role", required=False) or "customer",
                            req.get("name"), req.get("phone"),
                            req.get("language", required=False) or "ar",
                            admin_id=admin, whatsapp=req.get("whatsapp", required=False))
    code = A.issue_setup_code(conn, user_id, admin_id=admin)
    return {"id": user_id, "setup_code": code}


@route("POST", "/api/admin/users/<id>/setup-code")
def admin_setup_code(app, conn, req):
    return {"setup_code": A.issue_setup_code(conn, req.int("id"),
                                             admin_id=_admin(conn, req))}


@route("POST", "/api/admin/users/<id>/whatsapp")
def admin_whatsapp(app, conn, req):
    L.set_whatsapp(conn, req.int("id"), req.get("whatsapp", required=False),
                   admin_id=_admin(conn, req))
    return {"ok": True}


def _user_action(func):
    def handler(app, conn, req):
        func(conn, req.int("id"), admin_id=_admin(conn, req), reason=req.get("reason"))
        return {"ok": True}
    return handler


for _name, _func in (("block", L.block_user), ("unblock", L.unblock_user),
                     ("deactivate", L.deactivate_user), ("reactivate", L.reactivate_user)):
    route("POST", f"/api/admin/users/<id>/{_name}")(_user_action(_func))


@route("GET", "/api/admin/users/<id>/statement")
def admin_user_statement(app, conn, req):
    _admin(conn, req)
    code = f"CUS_{req.int('id')}"
    return {"balance": _money(L.account_balance(conn, code)),
            "entries": L.account_statement(conn, code, _limit(req))}


# ---------- admin: money ----------

@route("POST", "/api/admin/deposit")
def admin_deposit(app, conn, req):
    return {"txn_id": L.deposit(conn, req.number("user_id"), L.parse_lyd(req.get("amount")),
                                admin_id=_admin(conn, req), into=req.get("into"),
                                idempotency_key=req.get("idempotency_key", required=False),
                                note=req.get("note", required=False))}


@route("POST", "/api/admin/payroll")
def admin_payroll(app, conn, req):
    items = req.get("items")
    if not isinstance(items, list):
        raise HttpError(400, "'items' must be a list of {user_id, amount}")
    try:
        parsed = [(int(i["user_id"]), L.parse_lyd(i["amount"])) for i in items]
    except (KeyError, TypeError, ValueError):
        raise HttpError(400, "'items' must be a list of {user_id, amount}")
    return {"txn_id": L.payroll_credit(
        conn, parsed, admin_id=_admin(conn, req), batch_ref=req.get("batch_ref"),
        into=req.get("into", required=False) or "BANK",
        idempotency_key=req.get("idempotency_key", required=False),
        note=req.get("note", required=False))}


@route("POST", "/api/admin/withdraw")
def admin_withdraw(app, conn, req):
    fee = req.get("fee", required=False)
    return {"txn_id": L.withdraw_cash(
        conn, req.number("user_id"), L.parse_lyd(req.get("amount")),
        L.parse_lyd(fee) if fee not in (None, "") else 0, admin_id=_admin(conn, req),
        from_till=req.get("from_till"),
        idempotency_key=req.get("idempotency_key", required=False),
        note=req.get("note", required=False))}


@route("POST", "/api/admin/move")
def admin_move(app, conn, req):
    return {"txn_id": L.move_money(conn, req.get("from"), req.get("to"),
                                   L.parse_lyd(req.get("amount")), admin_id=_admin(conn, req),
                                   note=req.get("note", required=False),
                                   idempotency_key=req.get("idempotency_key", required=False))}


@route("POST", "/api/admin/reverse")
def admin_reverse(app, conn, req):
    return {"txn_id": L.reverse(conn, req.number("txn_id"), admin_id=_admin(conn, req),
                                reason=req.get("reason"),
                                idempotency_key=req.get("idempotency_key", required=False))}


@route("POST", "/api/admin/tills")
def admin_add_till(app, conn, req):
    return {"code": L.add_till(conn, req.get("name"), admin_id=_admin(conn, req))}


@route("GET", "/api/admin/health")
def admin_health(app, conn, req):
    _admin(conn, req)
    holdings = conn.execute("SELECT code FROM accounts WHERE kind = 'holding' ORDER BY code")
    return {"problems": O.verify(conn),
            "owed_to_workers": _money(L.owed_to_customers(conn)),
            "holdings": {r["code"]: _money(L.holding_amount(conn, r["code"])) for r in holdings},
            "margin": _money(L.account_balance(conn, "SYS_MARGIN")),
            "fees": _money(L.account_balance(conn, "SYS_FEES"))}


@route("POST", "/api/admin/reconcile")
def admin_reconcile(app, conn, req):
    _admin(conn, req)
    counts = req.get("counts")
    if not isinstance(counts, dict):
        raise HttpError(400, "'counts' must be {account: amount}")
    return {"rows": L.reconcile(conn, {k: L.parse_lyd(v) for k, v in counts.items()})}


# ---------- admin: currencies, suppliers, orders ----------

@route("POST", "/api/admin/currencies")
def admin_add_currency(app, conn, req):
    return {"code": O.add_currency(conn, req.get("code"), admin_id=_admin(conn, req))}


@route("POST", "/api/admin/prices")
def admin_set_price(app, conn, req):
    O.set_price(conn, req.get("currency"), req.get("rate"), admin_id=_admin(conn, req))
    return {"ok": True}


@route("GET", "/api/admin/suppliers")
def admin_suppliers(app, conn, req):
    _admin(conn, req)
    rows = conn.execute("SELECT id, name, country, active FROM suppliers ORDER BY id")
    return {"suppliers": [dict(r, balance=_money(O.supplier_balance(conn, r["id"])))
                          for r in rows]}


@route("POST", "/api/admin/suppliers")
def admin_add_supplier(app, conn, req):
    return {"id": O.add_supplier(conn, req.get("name"), req.get("country", required=False),
                                 admin_id=_admin(conn, req))}


@route("POST", "/api/admin/suppliers/<id>/price")
def admin_supplier_price(app, conn, req):
    O.set_supplier_price(conn, req.int("id"), req.get("currency"), req.get("rate"),
                         admin_id=_admin(conn, req))
    return {"ok": True}


@route("POST", "/api/admin/suppliers/<id>/pay")
def admin_pay_supplier(app, conn, req):
    return {"txn_id": O.pay_supplier(
        conn, req.int("id"), L.parse_lyd(req.get("amount")), admin_id=_admin(conn, req),
        from_code=req.get("from"), reference=req.get("reference"),
        idempotency_key=req.get("idempotency_key", required=False),
        note=req.get("note", required=False))}


@route("GET", "/api/admin/suppliers/<id>/statement")
def admin_supplier_statement(app, conn, req):
    _admin(conn, req)
    return {"balance": _money(O.supplier_balance(conn, req.int("id"))),
            "entries": O.supplier_statement(conn, req.int("id"), _limit(req))}


@route("GET", "/api/admin/orders")
def admin_orders(app, conn, req):
    _admin(conn, req)
    status = req.query.get("status")
    sql, args = "SELECT * FROM orders", []
    if status:
        sql, args = sql + " WHERE status = ?", [status]
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + [_limit(req)])
    return {"orders": [dict(r) for r in rows]}


@route("POST", "/api/admin/orders/<id>/assign")
def admin_assign(app, conn, req):
    O.assign_supplier(conn, req.int("id"), req.number("supplier_id"),
                      admin_id=_admin(conn, req), rate=req.get("rate", required=False))
    return {"ok": True}


@route("POST", "/api/admin/orders/<id>/unassign")
def admin_unassign(app, conn, req):
    O.unassign_supplier(conn, req.int("id"), admin_id=_admin(conn, req),
                        reason=req.get("reason"))
    return {"ok": True}


@route("POST", "/api/admin/orders/<id>/cancel")
def admin_cancel(app, conn, req):
    return {"txn_id": O.cancel_order(conn, req.int("id"), admin_id=_admin(conn, req),
                                     reason=req.get("reason"))}


@route("POST", "/api/admin/orders/paid")
def admin_mark_paid(app, conn, req):
    return {"txn_id": O.mark_paid(conn, req.get("receipt_code"), req.get("payout_ref"),
                                  admin_id=_admin(conn, req))}


@route("GET", "/api/admin/margin")
def admin_margin(app, conn, req):
    _admin(conn, req)
    try:
        since, until = (datetime.fromisoformat(req.query[k]) if req.query.get(k) else None
                        for k in ("since", "until"))
    except ValueError:
        raise HttpError(400, "since/until must be ISO dates like 2026-10-01T00:00:00+00:00")
    return {"rows": O.margin_report(conn, since, until)}


# ---------- the app ----------

class App:
    def __init__(self, db_path, *, secure_cookies=True, trust_proxy=False,
                 login_limiter=None):
        self.db_path = db_path
        self.secure_cookies = secure_cookies
        self.trust_proxy = trust_proxy
        self.login_limiter = login_limiter or RateLimiter(LOGIN_TRIES_PER_IP,
                                                          LOGIN_WINDOW_SECONDS)
        self.local = threading.local()
        A.connect(db_path).close()      # create the tables once, up front

    def conn(self):
        """One database connection per server thread (sqlite needs that)."""
        if getattr(self.local, "conn", None) is None:
            self.local.conn = A.connect(self.db_path)
        return self.local.conn

    def __call__(self, environ, start_response):
        cookie = None
        try:
            req = Request(environ, self.trust_proxy)
            for method, regex, handler in ROUTES:
                match = regex.match(req.path)
                if match:
                    if method != req.method:
                        continue
                    req.params = match.groupdict()
                    result = handler(self, self.conn(), req)
                    if isinstance(result, tuple):
                        result, cookie = result
                    status, body = 200, result
                    break
            else:
                allowed = any(regex.match(req.path) for _, regex, _ in ROUTES)
                raise HttpError(405 if allowed else 404,
                                "method not allowed" if allowed else "not found")
        except HttpError as e:
            status, body = e.status, {"error": str(e), "type": "http"}
        except L.LedgerError as e:
            status = next(code for cls, code in ERROR_STATUS if isinstance(e, cls))
            body = {"error": str(e), "type": type(e).__name__}
        except Exception:
            traceback.print_exc(file=sys.stderr)
            status, body = 500, {"error": "internal error", "type": "internal"}
        return self._respond(start_response, status, body, cookie)

    def _respond(self, start_response, status, body, cookie):
        data = json.dumps(body, ensure_ascii=False, default=str).encode()
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
        ]
        if cookie is not None:
            parts = [f"{COOKIE}={cookie}", "Path=/", "HttpOnly", "SameSite=Strict"]
            if self.secure_cookies:
                parts.append("Secure")
            parts.append("Max-Age=0" if cookie == "" else
                         f"Max-Age={A.SESSION_HOURS['customer'] * 3600}")
            headers.append(("Set-Cookie", "; ".join(parts)))
        phrase = HTTPStatus(status).phrase
        start_response(f"{status} {phrase}", headers)
        return [data]


def make_app(db_path, **options):
    """The WSGI app for a production server, e.g. in a small wsgi.py:
    application = make_app('/srv/wallet/wallet.db', trust_proxy=True)"""
    return App(db_path, **options)


class _ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="LYD wallet API server (development)")
    parser.add_argument("--db", default="wallet.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--insecure-cookies", action="store_true",
                        help="allow the session cookie over plain http (local testing only)")
    parser.add_argument("--trust-proxy", action="store_true",
                        help="take the client address from X-Forwarded-For")
    args = parser.parse_args(argv)
    app = App(args.db, secure_cookies=not args.insecure_cookies, trust_proxy=args.trust_proxy)
    with make_server(args.host, args.port, app, server_class=_ThreadingServer) as server:
        print(f"LYD wallet API on http://{args.host}:{args.port} (database {args.db})")
        server.serve_forever()


if __name__ == "__main__":
    main()
