"""The workers' screens and the JSON web API over HTTP, as a WSGI app (standard
library only).

Run locally:      python3 -m app.web --db wallet.db --port 8000 --insecure-cookies
                  (then open http://127.0.0.1:8000/; without --insecure-cookies
                  the screens' requests over plain http are refused)
In production:    python3 -m app.serve (app/serve.py): this app on waitress in
                  one process, behind HTTPS (Caddy), with the hourly check and
                  backups; deploy/ sets it all up with Docker. Served another
                  way: `make_app("wallet.db", trust_proxy=True)` with a WSGI
                  server in one process, behind HTTPS that sets
                  X-Forwarded-For. Waitress drops that header unless told to
                  trust the proxy: trusted_proxy="127.0.0.1" (the proxy's
                  address) and trusted_proxy_headers="x-forwarded-for".

Rules every request follows:
- The screens are two pages, the workers' at / and the office's at /admin,
  and their files under /static/ (STATIC_FILES), read from app/static once at start, so a changed file needs
  a restart, and one missing then answers 404 and is named on stderr. The
  request's path is only looked up in that list, never joined to a folder.
  They answer GET and HEAD only (any other method is a 405), with a strict
  Content-Security-Policy (PAGE_CSP: the page's own files and API only, no
  inline script or style, no native form submit, never inside another site's
  frame) and `Cache-Control: no-cache` with an ETag: a browser keeps them and
  asks each time whether they changed (304 when they didn't). They hold no
  private data and need no database. Every other path is the API's.
- API requests and responses are JSON (an answer to HEAD has the headers
  only), and no answer is stored by a browser or proxy (no-store). POST
  bodies must be sent as application/json, which a form on another website
  can't do.
- A request that can change something (any method but GET and HEAD) is
  refused with a 403, before its body is read, when a browser sent it from
  another website: its Origin, when it has one, must be this site,
  https://<the Host header> (http:// too with --insecure-cookies), and without
  one its Sec-Fetch-Site must not say same-site or cross-site. The SameSite
  cookie alone would let a page on another port or subdomain of the same site
  send it with the session. A request with an `Authorization: Bearer` token is
  not checked: a page can't add that header to a request to another site
  (that needs a CORS answer, which this server never gives).
- The session is the `__Host-session` cookie (HttpOnly, Secure,
  SameSite=Strict; plain `session` with --insecure-cookies) or an
  `Authorization: Bearer <token>` header for a phone app. Browsers take a
  `__Host-` cookie only from this very host, so another subdomain can't plant
  one; two different session cookies in a request count as none.
- Login and setup-password give the session's token in their JSON answer only
  to a client that isn't a browser (no Origin and no Sec-Fetch-Site: a phone
  app). A browser gets only the HttpOnly cookie, so no script on the page can
  read the session.
- Amounts are typed text in LYD ("12.5"); answers carry dirhams and LYD text.
  Text fields must be JSON strings, at most MAX_TEXT characters; an optional
  field sent blank counts as left out.
- Failed login, setup-code and change-password tries are limited per internet
  address, successful ones per account, and each worker's money and order
  requests per minute. These limits are kept in memory, per server process.
- Errors come back as {"error": message, "type": kind} with a fitting status;
  unexpected errors say only "internal error" and are logged on the server.
"""
import argparse
import hashlib
import io
import ipaddress
import json
import queue
import re
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from . import api
from . import auth as A
from . import ledger as L
from . import orders as O

MAX_BODY_BYTES = 64 * 1024
LOGIN_TRIES_PER_IP = 20             # failed tries per address (IPv6: per /64)
LOGINS_PER_ACCOUNT = 10             # successful tries per account, from anywhere
LOGIN_WINDOW_SECONDS = 10 * 60
WORKER_REQUESTS_PER_MINUTE = 30     # money, order and recipient requests per worker
LIMITER_MAX_KEYS = 10_000           # addresses or workers a limiter remembers at once
DEV_SERVER_THREADS = 8
COOKIE = "session"                  # only with secure_cookies=False (plain http)
SECURE_COOKIE = "__Host-session"    # can't be set from another subdomain or path
MAX_ID = 2**63 - 1                  # the largest whole number the database stores

# The screens: each URL path and its file in STATIC_DIR. Only these are
# served; nothing else in the folder is ever read. The workers' page is /,
# the office's /admin (it shares the style sheet and the icon).
STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": "index.html",
    "/static/app.js": "app.js",
    "/static/strings.js": "strings.js",
    "/static/style.css": "style.css",
    "/static/icon.svg": "icon.svg",
    "/admin": "admin.html",
    "/static/admin.js": "admin.js",
    "/static/admin.css": "admin.css",
}
# Fixed here, not taken from the system's table, which can say text/plain for
# .js (on Windows).
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
# The screens' policy: scripts, styles, images and requests only from this
# site's own files and API; no inline script or style; no native form submit
# (the page sends everything with fetch); never inside another page's frame.
# Trusted Types make the browser refuse innerHTML and the other HTML sinks.
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; form-action 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; require-trusted-types-for 'script'; "
            "trusted-types 'none'")
PAGE_HEADERS = (
    ("Content-Security-Policy", PAGE_CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)
# The 403 for a request sent from another website (App._check_origin).
ORIGIN_REFUSED = "refused: sent from another website"

# Longest text accepted per field, in characters; any other text field is at
# most MAX_OTHER_TEXT. The one place these limits are checked (Request.get).
MAX_TEXT = {
    "name": 100, "beneficiary_name": 100, "beneficiary_id": 100, "country": 100,
    "note": 200, "reason": 500,
    "reference": 100, "payout_ref": 100, "batch_ref": 100, "receipt_code": 100,
    "idempotency_key": L.MAX_IDEMPOTENCY_KEY,
}
MAX_OTHER_TEXT = 200
# Text that is only spaces or invisible marks (direction marks, the Arabic
# letter mark, zero-width spaces) counts as blank.
_BLANK = re.compile(r"[\s\u061c\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]*")

ERROR_STATUS = [            # first match wins, so subclasses come first
    (A.WrongPassword, 403), (A.LoginFailed, 401), (A.NotLoggedIn, 401),
    (A.AccountLocked, 429),
    (L.PermissionDenied, 403), (L.AccountBlocked, 403), (L.NotFound, 404),
    (O.QuoteExpired, 409), (L.InsufficientFunds, 409), (L.NotEnoughCash, 409),
    (L.Conflict, 409), (L.LedgerError, 400),
]


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class RateLimiter:
    """At most `limit` tries per key in a sliding window. Kept in memory, so per
    server process. A key with no try left in the window is forgotten, and at
    most `max_keys` are kept (the one seen longest ago makes room)."""

    def __init__(self, limit, seconds, clock=time.monotonic, *,
                 max_keys=LIMITER_MAX_KEYS,
                 message="too many tries from this connection; wait a few minutes"):
        self.limit, self.seconds, self.clock = limit, seconds, clock
        self.max_keys, self.message = max_keys, message
        self.hits = OrderedDict()           # key -> times of its tries; least recent first
        self.lock = threading.Lock()

    def check(self, key):
        """Count a try for `key`, or refuse it (429) when the window is full.
        Returns the try's time, for forgive()."""
        now = self.clock()
        with self.lock:
            while self.hits:
                oldest = next(iter(self.hits.values()))
                if oldest and oldest[-1] > now - self.seconds:
                    break
                self.hits.popitem(last=False)
            if key not in self.hits and len(self.hits) >= self.max_keys:
                self.hits.popitem(last=False)
            hits = self.hits.setdefault(key, deque())
            self.hits.move_to_end(key)
            while hits and hits[0] <= now - self.seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                raise HttpError(429, self.message)
            hits.append(now)
            return now

    def forgive(self, key, when):
        """Take back the try made at `when`: it went fine, so it doesn't count."""
        with self.lock:
            hits = self.hits.get(key)
            if hits is not None and when in hits:
                hits.remove(when)
                if not hits:
                    del self.hits[key]


def client_key(ip):
    """What login tries are counted by: the client's address, but for IPv6 its
    /64 (one home or phone network), where anyone can pick a new address.
    Waitress gives an IPv6 address from the proxy in brackets: [2001:db8::1]."""
    try:
        address = ipaddress.ip_address(ip.removeprefix("[").removesuffix("]"))
    except ValueError:
        return ip
    if address.version == 6 and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    if address.version == 6:
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


# ---------- where a request comes from ----------

def _bearer(environ):
    """The token of an `Authorization: Bearer <token>` header, or None when
    there is no such header. The one test of whether a request carries a
    Bearer token: Request.token acts with it, and App._check_origin lets such a
    request through, so a request can't skip the check and act with the cookie."""
    header = environ.get("HTTP_AUTHORIZATION", "")
    return header[7:].strip() if header.startswith("Bearer ") else None


def _same_origin(origin, host, schemes):
    """Whether a browser's Origin header is this site: one of `schemes`, then
    the request's Host exactly, a default port (:443, :80) written or not.
    A plain comparison, with no URL parsing, so nothing can be slipped past
    it: null, a path, user info, another port or scheme, or no Host never
    match."""
    origin, host = origin.strip().lower(), host.strip().lower()
    if not host:
        return False
    for scheme in schemes:
        default = ":443" if scheme == "https" else ":80"
        if origin.removesuffix(default) == f"{scheme}://{host.removesuffix(default)}":
            return True
    return False


def _from_browser(environ):
    """Whether a browser sent the request: every current one sends Origin or
    Sec-Fetch-Site with a fetch() POST, and a phone app's HTTP client neither."""
    return "HTTP_ORIGIN" in environ or "HTTP_SEC_FETCH_SITE" in environ


def _etag_matches(header, etag):
    """Whether an If-None-Match header names `etag`, or is *. A tag counts
    with or without W/ (weak), which a proxy that compresses the file adds."""
    for tag in header.split(","):
        tag = tag.strip()
        if tag == "*" or tag.removeprefix("W/") == etag:
            return True
    return False


# ---------- fields of a JSON body ----------

def _text(name, value, required=True):
    """A text field: a JSON string of at most its MAX_TEXT characters. Left
    out, null or blank is None, or a 400 if it is required."""
    if value is None or (isinstance(value, str) and _BLANK.fullmatch(value)):
        if required:
            raise HttpError(400, f"'{name}' is required")
        return None
    if not isinstance(value, str):
        raise HttpError(400, f"'{name}' must be text")
    limit = MAX_TEXT.get(name, MAX_OTHER_TEXT)
    if len(value) > limit:
        raise HttpError(400, f"'{name}' is too long (at most {limit} characters)")
    return value


def _amount(name, value, required=True):
    """An amount or rate as typed text ("12.5"); a whole JSON number is taken
    too, but not a fraction (1.5), which may not be exactly what was typed."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text(name, value, required)


def _whole_number(name, value):
    """A whole number such as a user id: a JSON number or digits, not true or
    2.9, and small enough for the database."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise HttpError(400, f"'{name}' must be a whole number")
    try:
        number = int(value)
    except ValueError:
        raise HttpError(400, f"'{name}' must be a whole number")
    if abs(number) > MAX_ID:
        raise HttpError(400, f"'{name}' is too large")
    return number


class Request:
    def __init__(self, environ, trust_proxy, cookie=COOKIE):
        self.environ = environ
        self.cookie = cookie        # the session cookie's name
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
        # Plain digits only: int() would also take "-1", which reads to the end
        # of the stream past the size cap. No Content-Length means no body.
        text = (self.environ.get("CONTENT_LENGTH") or "0").strip()
        if not (text.isascii() and text.isdigit()):
            raise HttpError(400, "bad Content-Length")
        digits = text.lstrip("0") or "0"
        if len(digits) > len(str(MAX_BODY_BYTES)) or int(digits) > MAX_BODY_BYTES:
            raise HttpError(413, "request too large")
        length = int(digits)
        try:
            raw = self.environ["wsgi.input"].read(length) if length else b""
        except OSError:         # the client stopped sending (the server's time limit)
            raise HttpError(400, "the request body did not arrive")
        try:
            body = json.loads(raw or b"{}")
            # Every text in it must be real Unicode: a lone surrogate ("\ud800")
            # can't be stored or hashed.
            json.dumps(body, ensure_ascii=False).encode()
        except (ValueError, RecursionError):     # also bad UTF-8, or nested too deep
            raise HttpError(400, "body is not valid JSON")
        if not isinstance(body, dict):
            raise HttpError(400, "body must be a JSON object")
        return body

    @property
    def token(self):
        bearer = _bearer(self.environ)
        if bearer is not None:
            return bearer
        # Read by hand: SimpleCookie stops at the first cookie it can't parse
        # (another app's JSON, a value with a space), losing ours after it.
        # Two different values mean one was planted (browsers send a cookie
        # with a longer Path first): neither is taken.
        values = set()
        for part in self.environ.get("HTTP_COOKIE", "").split(";"):
            name, sep, value = part.partition("=")
            value = value.strip().strip('"')
            if sep and name.strip() == self.cookie and value:
                values.add(value)
        return values.pop() if len(values) == 1 else None

    def get(self, name, required=True):
        """A text field from the JSON body (see _text)."""
        return _text(name, self.body.get(name), required)

    def amount(self, name, required=True):
        """An amount or rate from the JSON body (see _amount)."""
        return _amount(name, self.body.get(name), required)

    def number(self, name, required=True):
        """A whole-number field from the JSON body, e.g. user_id. If optional,
        left out or blank is None."""
        value = self.body.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise HttpError(400, f"'{name}' is required")
            return None
        return _whole_number(name, value)

    def int(self, name):
        """A number in the path, e.g. /orders/<id>."""
        try:
            value = int(self.params[name])
        except (KeyError, ValueError):
            raise HttpError(404, "not found")
        if value > MAX_ID:
            raise HttpError(404, "not found")
        return value


ROUTES = []


def route(method, pattern):
    regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[0-9]+)", pattern) + "$")

    def register(handler):
        ROUTES.append((method, regex, handler))
        return handler
    return register


def _money(dirhams):
    return {"dirhams": dirhams, "lyd": L.format_lyd(dirhams)}


def _idem_key(req, required=True):
    """The request's idempotency_key. Admin money routes and /api/send require
    one, so a double click or a retry can't post twice. A key that is sent must
    be real text: a blank one is refused, never taken as "no key"."""
    value = req.body.get("idempotency_key")
    if value is None:
        if required:
            raise HttpError(400, "'idempotency_key' is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise HttpError(400, "'idempotency_key' must be non-empty text")
    _text("idempotency_key", value.strip())        # its length limit
    return value


def _limit(req, default=50):
    try:
        return max(1, min(int(req.query.get("limit", default)), 200))
    except ValueError:
        raise HttpError(400, "limit must be a number")


# ---------- login ----------

def _login_try(app, req, account, attempt):
    """Run a login, setup-code or change-password try for `account` (a phone),
    as attempt(client): the address it counts for in the auth lock.
    - Only a failed one (wrong, or refused because of earlier wrong ones)
      counts against the address's LOGIN_TRIES_PER_IP, so a whole office
      behind one address can log in.
    - Only one with the right password or code counts against the account's
      LOGINS_PER_ACCOUNT, so one account can't log in over and over (each a
      slow hash and a new session), and nobody without its password can use
      them up.
    Both are counted while the try runs, so tries sent all at once can't get
    past them."""
    address, tried, held = client_key(req.ip), None, None
    wrong = right = False
    try:
        tried = app.login_limiter.check(address)
        held = app.account_limiter.check(account)
        result = attempt(address)
        right = True
        return result
    except (A.LoginFailed, A.AccountLocked):
        wrong = True
        raise
    except L.AccountBlocked:            # the right password of a blocked user
        right = True
        raise
    finally:
        if tried is not None and not wrong:
            app.login_limiter.forgive(address, tried)
        if held is not None and not right:
            app.account_limiter.forgive(account, held)


@route("GET", "/api/ping")
def ping(app, conn, req):
    """For the proxy's and the container's health checks: no login needed, and
    it tells nothing but that the server answers and its database opens."""
    conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()
    return {"ok": True}


def _logged_in(req, token):
    """The answer to a login or setup-password that worked, with the session
    cookie. The token is in the JSON too only for a client that isn't a browser
    (a phone app keeps it for its Bearer header). A browser has the cookie,
    which no script on the page can read; the token in the answer could be read
    by any script that sees it, and would work from anywhere for a week."""
    return ({"ok": True} if _from_browser(req.environ) else {"ok": True, "token": token}), token


@route("POST", "/api/login")
def login(app, conn, req):
    phone, password = req.get("phone"), req.get("password")
    token = _login_try(app, req, A._login_phone(phone) or phone,
                       lambda client: A.login(conn, phone, password, client=client))
    return _logged_in(req, token)


@route("POST", "/api/setup-password")
def setup_password(app, conn, req):
    phone, code, password = req.get("phone"), req.get("code"), req.get("password")
    token = _login_try(app, req, A._login_phone(phone) or phone,
                       lambda client: A.set_password_with_code(conn, phone, code, password,
                                                               client=client))
    return _logged_in(req, token)


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
    old, new = req.get("old_password"), req.get("new_password")
    _login_try(app, req, A.authenticate(conn, req.token)["phone"],
               lambda client: A.change_password(conn, req.token, old, new, client=client))
    return {"ok": True}


# ---------- worker ----------

def _worker_limit(app, conn, req):
    """Counts a worker's money, order or recipient request: at most
    WORKER_REQUESTS_PER_MINUTE each (per process), so one account can't flood
    the books, which can't be cleaned up, or read out the staff list."""
    app.worker_limiter.check(A.authenticate(conn, req.token)["id"])


@route("GET", "/api/me")
def me(app, conn, req):
    return api.me(conn, req.token)


@route("GET", "/api/statement")
def statement(app, conn, req):
    return {"entries": api.my_statement(conn, req.token, _limit(req))}


@route("GET", "/api/recipient")
def recipient(app, conn, req):
    _worker_limit(app, conn, req)
    return api.lookup_recipient(conn, req.token, req.query.get("phone", ""))


@route("POST", "/api/send")
def send(app, conn, req):
    _worker_limit(app, conn, req)
    return api.send_money(conn, req.token, req.get("to_phone"), req.amount("amount"),
                          idempotency_key=_idem_key(req),
                          note=req.get("note", required=False),
                          from_phone=req.get("from_phone", required=False))


@route("GET", "/api/prices")
def prices(app, conn, req):
    return {"prices": api.prices(conn, req.token)}


@route("POST", "/api/quote")
def quote(app, conn, req):
    _worker_limit(app, conn, req)
    return api.get_quote(conn, req.token, req.get("currency"), req.amount("amount"))


@route("GET", "/api/orders")
def my_orders(app, conn, req):
    return {"orders": api.my_orders(conn, req.token, _limit(req))}


@route("POST", "/api/orders")
def place_order(app, conn, req):
    _worker_limit(app, conn, req)
    return api.place_order(conn, req.token, req.get("quote_id"),
                           req.get("beneficiary_name"), req.get("beneficiary_id"),
                           from_phone=req.get("from_phone", required=False))


# ---------- admin: users ----------

def _admin(conn, req):
    return api.admin_id(conn, req.token)


@route("GET", "/api/admin/me")
def admin_me(app, conn, req):
    user = conn.execute("SELECT id, name, phone, language FROM users WHERE id = ?",
                        (_admin(conn, req),)).fetchone()
    return dict(user)


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
    # Every field is checked before the user is written: a bad one is a 400
    # with nothing made, not an account without a code that a retry can't make.
    reason = req.get("reason", required=False) or "new account"
    user_id = L.create_user(conn, req.get("role", required=False) or "customer",
                            req.get("name"), req.get("phone"),
                            req.get("language", required=False) or "en",
                            admin_id=admin, whatsapp=req.get("whatsapp", required=False))
    code = A.issue_setup_code(conn, user_id, admin_id=admin, reason=reason)
    return {"id": user_id, "setup_code": code}


@route("POST", "/api/admin/users/<id>/setup-code")
def admin_setup_code(app, conn, req):
    return {"setup_code": A.issue_setup_code(conn, req.int("id"), admin_id=_admin(conn, req),
                                             reason=req.get("reason", required=False))}


@route("POST", "/api/admin/users/<id>/whatsapp")
def admin_whatsapp(app, conn, req):
    admin = _admin(conn, req)
    if "whatsapp" not in req.body:      # left out by mistake: the number stays
        raise HttpError(400, "'whatsapp' is required (null or \"\" removes the number)")
    L.set_whatsapp(conn, req.int("id"), req.get("whatsapp", required=False),
                   admin_id=admin)
    return {"ok": True}


def _user_action(func):
    def handler(app, conn, req):
        func(conn, req.int("id"), admin_id=_admin(conn, req), reason=req.get("reason"))
        return {"ok": True}
    return handler


for _name, _func in (("block", L.block_user), ("unblock", L.unblock_user),
                     ("deactivate", L.deactivate_user), ("reactivate", L.reactivate_user),
                     ("unlock", A.unlock_login)):
    route("POST", f"/api/admin/users/<id>/{_name}")(_user_action(_func))


@route("GET", "/api/admin/users/<id>/statement")
def admin_user_statement(app, conn, req):
    _admin(conn, req)
    code, limit = f"CUS_{req.int('id')}", _limit(req)
    with L._read(conn):     # the balance and the entries from the same moment
        return {"balance": _money(L.account_balance(conn, code)),
                "entries": L.account_statement(conn, code, limit)}


# ---------- admin: money ----------

@route("POST", "/api/admin/deposit")
def admin_deposit(app, conn, req):
    return {"txn_id": L.deposit(conn, req.number("user_id"), L.parse_lyd(req.amount("amount")),
                                admin_id=_admin(conn, req), into=req.get("into"),
                                idempotency_key=_idem_key(req),
                                note=req.get("note", required=False))}


@route("POST", "/api/admin/payroll")
def admin_payroll(app, conn, req):
    items = req.body.get("items")
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise HttpError(400, "'items' must be a list of {user_id, amount}")
    parsed = [(_whole_number("user_id", i.get("user_id")),
               L.parse_lyd(_amount("amount", i.get("amount")))) for i in items]
    return {"txn_id": L.payroll_credit(
        conn, parsed, admin_id=_admin(conn, req), batch_ref=req.get("batch_ref"),
        into=req.get("into", required=False) or "BANK",
        idempotency_key=_idem_key(req),
        note=req.get("note", required=False))}


@route("POST", "/api/admin/withdraw")
def admin_withdraw(app, conn, req):
    fee = req.amount("fee", required=False)
    return {"txn_id": L.withdraw_cash(
        conn, req.number("user_id"), L.parse_lyd(req.amount("amount")),
        L.parse_lyd(fee) if fee is not None else 0, admin_id=_admin(conn, req),
        from_till=req.get("from_till"),
        idempotency_key=_idem_key(req),
        note=req.get("note", required=False))}


@route("POST", "/api/admin/move")
def admin_move(app, conn, req):
    return {"txn_id": L.move_money(conn, req.get("from"), req.get("to"),
                                   L.parse_lyd(req.amount("amount")), admin_id=_admin(conn, req),
                                   note=req.get("note", required=False),
                                   idempotency_key=_idem_key(req))}


@route("POST", "/api/admin/reverse")
def admin_reverse(app, conn, req):
    return {"txn_id": L.reverse(conn, req.number("txn_id"), admin_id=_admin(conn, req),
                                reason=req.get("reason"),
                                idempotency_key=_idem_key(req, required=False))}


@route("POST", "/api/admin/tills")
def admin_add_till(app, conn, req):
    return {"code": L.add_till(conn, req.get("name"), admin_id=_admin(conn, req))}


@route("GET", "/api/admin/health")
def admin_health(app, conn, req):
    _admin(conn, req)
    with L._read(conn):     # every number below from the same moment
        holdings = conn.execute(
            "SELECT code FROM accounts WHERE kind = 'holding' ORDER BY code").fetchall()
        return {"problems": O.verify(conn),
                "owed_to_workers": _money(L.owed_to_customers(conn)),
                "holdings": {r["code"]: _money(L.holding_amount(conn, r["code"]))
                             for r in holdings},
                "margin": _money(L.account_balance(conn, "SYS_MARGIN")),
                "fees": _money(L.account_balance(conn, "SYS_FEES")),
                # the production server's hourly check and backups (app.serve)
                "maintenance": app.maintenance.status() if app.maintenance else None}


@route("POST", "/api/admin/reconcile")
def admin_reconcile(app, conn, req):
    _admin(conn, req)
    counts = req.body.get("counts")
    if not isinstance(counts, dict):
        raise HttpError(400, "'counts' must be {account: amount}")
    return {"rows": L.reconcile(conn, {k: L.parse_lyd(_amount("counts", v))
                                       for k, v in counts.items()})}


# ---------- admin: currencies, suppliers, orders ----------

@route("GET", "/api/admin/currencies")
def admin_currencies(app, conn, req):
    """Every currency, switched on or off, with its price now (null: none set)."""
    _admin(conn, req)
    with L._read(conn):
        rows = conn.execute("SELECT code, active FROM currencies ORDER BY code").fetchall()
        return {"currencies": [dict(code=r["code"], active=bool(r["active"]),
                                    price=O.current_price(conn, r["code"])) for r in rows]}


@route("POST", "/api/admin/currencies")
def admin_add_currency(app, conn, req):
    return {"code": O.add_currency(conn, req.get("code"), admin_id=_admin(conn, req))}


@route("POST", "/api/admin/currencies/active")
def admin_currency_active(app, conn, req):
    O.set_currency_active(conn, req.get("code"), req.body.get("active"),
                          admin_id=_admin(conn, req), reason=req.get("reason"))
    return {"ok": True}


@route("POST", "/api/admin/prices")
def admin_set_price(app, conn, req):
    void = req.body.get("void_quotes")
    O.set_price(conn, req.get("currency"), req.amount("rate"), admin_id=_admin(conn, req),
                void_quotes=False if void in (None, "") else void)
    return {"ok": True}


@route("GET", "/api/admin/suppliers")
def admin_suppliers(app, conn, req):
    _admin(conn, req)
    with L._read(conn):
        rows = conn.execute("SELECT id, name, country, active FROM suppliers ORDER BY id")
        codes = [r["code"] for r in conn.execute("SELECT code FROM currencies ORDER BY code")]

        def prices(supplier):       # {currency: its rate now}, for those it has one for
            now = {c: O.current_supplier_price(conn, supplier, c) for c in codes}
            return {c: price["rate"] for c, price in now.items() if price}
        return {"suppliers": [dict(r, balance=_money(O.supplier_balance(conn, r["id"])),
                                   prices=prices(r["id"])) for r in rows]}


@route("POST", "/api/admin/suppliers")
def admin_add_supplier(app, conn, req):
    return {"id": O.add_supplier(conn, req.get("name"), req.get("country", required=False),
                                 admin_id=_admin(conn, req))}


@route("POST", "/api/admin/suppliers/<id>/active")
def admin_supplier_active(app, conn, req):
    O.set_supplier_active(conn, req.int("id"), req.body.get("active"),
                          admin_id=_admin(conn, req), reason=req.get("reason"))
    return {"ok": True}


@route("POST", "/api/admin/suppliers/<id>/price")
def admin_supplier_price(app, conn, req):
    O.set_supplier_price(conn, req.int("id"), req.get("currency"), req.amount("rate"),
                         admin_id=_admin(conn, req))
    return {"ok": True}


@route("POST", "/api/admin/suppliers/<id>/pay")
def admin_pay_supplier(app, conn, req):
    return {"txn_id": O.pay_supplier(
        conn, req.int("id"), L.parse_lyd(req.amount("amount")), admin_id=_admin(conn, req),
        from_code=req.get("from"), reference=req.get("reference"),
        idempotency_key=_idem_key(req),
        note=req.get("note", required=False))}


@route("GET", "/api/admin/suppliers/<id>/statement")
def admin_supplier_statement(app, conn, req):
    _admin(conn, req)
    supplier, limit = req.int("id"), _limit(req)
    with L._read(conn):     # the balance and the entries from the same moment
        return {"balance": _money(O.supplier_balance(conn, supplier)),
                "entries": O.supplier_statement(conn, supplier, limit)}


@route("GET", "/api/admin/orders")
def admin_orders(app, conn, req):
    _admin(conn, req)
    status = req.query.get("status")
    sql, args = ("SELECT o.*, u.blocked AS customer_blocked FROM orders o "
                 "JOIN users u ON u.id = o.customer_id"), []
    if status:
        sql, args = sql + " WHERE o.status = ?", [status]
    rows = conn.execute(sql + " ORDER BY o.id DESC LIMIT ?", args + [_limit(req)])
    return {"orders": [dict(r) for r in rows]}


@route("POST", "/api/admin/orders/<id>/assign")
def admin_assign(app, conn, req):
    O.assign_supplier(conn, req.int("id"), req.number("supplier_id"),
                      admin_id=_admin(conn, req), rate=req.amount("rate", required=False))
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
    return {"txn_id": O.mark_paid(
        conn, req.get("receipt_code"), req.get("payout_ref"), admin_id=_admin(conn, req),
        supplier_id=req.number("supplier_id", required=False),  # whose confirmation
        rate=req.amount("rate", required=False))}      # only without a supplier yet


def _report_time(text):
    """A report boundary, in UTC: a date (2026-10-01) or an ISO time with Z or
    an offset; without an offset it is UTC. An offset's '+' sent without
    URL-encoding arrives as a space, so it is put back."""
    text = text.strip()
    text = re.sub(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)"
                  r" (\d{2}(?::?\d{2})?)$", r"\1+\2", text)
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)    # here, so year 10000 in UTC is a 400


@route("GET", "/api/admin/margin")
def admin_margin(app, conn, req):
    _admin(conn, req)
    try:
        since, until = (_report_time(req.query[k]) if req.query.get(k) else None
                        for k in ("since", "until"))
    except (ValueError, OverflowError):
        raise HttpError(400, "since/until must be dates like 2026-10-01 or times like "
                             "2026-10-01T00:00:00Z (no zone = UTC)")
    return {"rows": O.margin_report(conn, since, until)}


@route("GET", "/api/admin/log")
def admin_log(app, conn, req):
    """The admin log, newest first: who issued and used setup codes, unassign,
    block and cancel reasons... ?action=, ?kind= (user, order, transaction,
    supplier or code) and ?target= (that id or till/currency code) narrow it."""
    _admin(conn, req)
    return {"entries": L.admin_log(conn, action=req.query.get("action") or None,
                                   target=req.query.get("target") or None,
                                   kind=req.query.get("kind") or None,
                                   limit=_limit(req))}


# ---------- the app ----------

def _read_static(static_dir):
    """Each STATIC_FILES file, read once: {URL path: (body, content type, ETag)},
    and a list that says what is wrong with each file that couldn't be read.
    Such a file is left out, so its path answers 404."""
    files, missing = {}, []
    for path, name in STATIC_FILES.items():
        file = Path(static_dir) / name
        try:
            body = file.read_bytes()
        except OSError as e:
            missing.append(f"{file} is missing" if isinstance(e, FileNotFoundError)
                           else f"{file} can't be read ({e.strerror or e})")
            continue
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        files[path] = (body, STATIC_TYPES[file.suffix], etag)
    return files, missing


class App:
    def __init__(self, db_path, *, secure_cookies=True, trust_proxy=False,
                 login_limiter=None, account_limiter=None, worker_limiter=None,
                 static_dir=STATIC_DIR):
        self.db_path = db_path
        self.secure_cookies = secure_cookies
        self.cookie_name = SECURE_COOKIE if secure_cookies else COOKIE
        # What a browser's Origin may start with: plain http only for local
        # testing, where the cookie is allowed over it too.
        self.origin_schemes = ("https",) if secure_cookies else ("https", "http")
        # Editing a page file needs a restart. Those that couldn't be read are
        # named on stderr by warn_missing_pages().
        self.static, self.missing_pages = _read_static(static_dir)
        self.warned_missing_pages = False
        self.trust_proxy = trust_proxy
        self.login_limiter = login_limiter or RateLimiter(LOGIN_TRIES_PER_IP,
                                                          LOGIN_WINDOW_SECONDS)
        self.account_limiter = account_limiter or RateLimiter(
            LOGINS_PER_ACCOUNT, LOGIN_WINDOW_SECONDS,
            message="too many logins to this account; wait a few minutes")
        self.worker_limiter = worker_limiter or RateLimiter(
            WORKER_REQUESTS_PER_MINUTE, 60, message="too many requests; wait a minute")
        self.warned_no_forwarded_for = False
        self.maintenance = None         # app.serve's Maintenance, when it runs one
        self.local = threading.local()
        A.connect(db_path).close()      # create the tables once, up front

    def conn(self):
        """The calling thread's database connection, opened on its first request
        and kept for the next ones (sqlite needs one per thread). Servers with a
        fixed set of threads, like the bundled one, waitress or gunicorn
        --threads, so open a few connections in all, not one per request."""
        if getattr(self.local, "conn", None) is None:
            self.local.conn = A.connect(self.db_path)
        return self.local.conn

    def close_connection(self):
        """Close the calling thread's connection (when that thread ends)."""
        conn = getattr(self.local, "conn", None)
        if conn is not None:
            conn.close()
            self.local.conn = None

    def _check_forwarded_for(self, environ):
        """With trust_proxy, a request without X-Forwarded-For is counted for
        the address it came from, the proxy's: every client would share one
        login limit and lock. Said once on stderr, as it means the proxy or
        the server drops the header (waitress does unless told to trust it)."""
        if self.trust_proxy and not environ.get("HTTP_X_FORWARDED_FOR") \
                and not self.warned_no_forwarded_for:
            self.warned_no_forwarded_for = True
            print(f"warning: trust_proxy is set but a request from "
                  f"{environ.get('REMOTE_ADDR')} came without X-Forwarded-For, so it "
                  "counts for that address; make the proxy set the header (with "
                  "waitress: trusted_proxy=<the proxy's address>, "
                  "trusted_proxy_headers='x-forwarded-for')", file=sys.stderr)

    def warn_missing_pages(self):
        """Name on stderr, once, each page file that couldn't be read at start,
        so that a broken install (an image without app/static) shows in the
        log. Not when the app is made: the server's own first line, or its
        error such as an address it can't listen on, comes first. The bundled
        server (main) and app.serve call this right after their first line;
        under any other server, the first request for a page file that isn't
        there does."""
        if self.missing_pages and not self.warned_missing_pages:
            self.warned_missing_pages = True
            for problem in self.missing_pages:
                print(f"warning: {problem}: the screens won't load", file=sys.stderr)

    def _check_origin(self, environ):
        """Refuse (403) a request that can change something when a browser sent
        it from another website, before its body is read. Called for every
        method but GET and HEAD.
        - With a Bearer token: not checked. That token is what the request acts
          with, and a page on another site can't add the header without a CORS
          answer, which this server never gives.
        - With an Origin: it must be this site (_same_origin), with or without
          a cookie, so a login from another site is refused too.
        - Without one: every current browser sends Origin with a POST, so this
          is a program holding whatever cookie it sends, unless Sec-Fetch-Site
          says the request came from another site.
        "This site" is the Host header, which Caddy and Cloudflare's tunnel
        pass on as the browser sent it; never X-Forwarded-Host, which anyone
        could send to the bundled server."""
        if _bearer(environ) is not None:
            return
        origin = environ.get("HTTP_ORIGIN")
        if origin is not None:
            if not _same_origin(origin, environ.get("HTTP_HOST", ""), self.origin_schemes):
                raise HttpError(403, ORIGIN_REFUSED)
        elif environ.get("HTTP_SEC_FETCH_SITE", "none").strip().lower() \
                not in ("same-origin", "none"):
            raise HttpError(403, ORIGIN_REFUSED)

    def _static(self, environ, start_response, entry):
        """A page file, for GET or HEAD: 200 with it, or 304 with no body when
        the browser's copy is this one (If-None-Match). HEAD gets the same
        status and headers, and no body. Never a cookie."""
        body, content_type, etag = entry
        headers = [("Cache-Control", "no-cache"), ("ETag", etag), *PAGE_HEADERS]
        if _etag_matches(environ.get("HTTP_IF_NONE_MATCH", ""), etag):
            start_response("304 Not Modified", headers)
            return []
        start_response("200 OK", [("Content-Type", content_type),
                                  ("Content-Length", str(len(body))), *headers])
        return [] if environ["REQUEST_METHOD"] == "HEAD" else [body]

    def __call__(self, environ, start_response):
        cookie = None
        self._check_forwarded_for(environ)
        path = environ.get("PATH_INFO", "/")
        head = environ["REQUEST_METHOD"] == "HEAD"
        reads = head or environ["REQUEST_METHOD"] == "GET"
        try:
            # The screens first: no Origin check, no body read, no database.
            # A listed file that was missing at start goes on to the routes:
            # a 404.
            entry = self.static.get(path)
            if entry is not None and reads:
                return self._static(environ, start_response, entry)
            if reads and path in STATIC_FILES:
                self.warn_missing_pages()
            if not reads:
                if path in STATIC_FILES:
                    raise HttpError(405, "method not allowed")  # the body is never read
                self._check_origin(environ)
            req = Request(environ, self.trust_proxy, self.cookie_name)
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
        return self._respond(start_response, status, body, cookie, head)

    def _respond(self, start_response, status, body, cookie, head=False):
        """The JSON answer. For HEAD, the same status and headers without the
        JSON: neither waitress nor the bundled server leaves it out, and a
        client that reuses the connection would read it as the next answer."""
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
            parts = [f"{self.cookie_name}={cookie}", "Path=/", "HttpOnly", "SameSite=Strict"]
            if self.secure_cookies:
                parts.append("Secure")
            parts.append("Max-Age=0" if cookie == "" else
                         f"Max-Age={A.SESSION_HOURS['customer'] * 3600}")
            headers.append(("Set-Cookie", "; ".join(parts)))
        phrase = HTTPStatus(status).phrase
        start_response(f"{status} {phrase}", headers)
        return [] if head else [data]


def make_app(db_path, **options):
    """The WSGI app for a production server, e.g. in a small wsgi.py:
    application = make_app('/srv/wallet/wallet.db', trust_proxy=True)
    Run it in one process (threads are fine): the request limits are kept in
    memory, per process. With waitress, also trust the proxy, or it drops
    X-Forwarded-For and every client looks like the proxy:
    waitress.serve(application, trusted_proxy='127.0.0.1',
                   trusted_proxy_headers='x-forwarded-for')"""
    return App(db_path, **options)


class _RequestDeadline(io.RawIOBase):
    """How the bundled server reads a request: all of it (line, headers and
    body) must arrive within `seconds`, so a client that sends a byte now and
    then is let go too, not only one that sends nothing for `idle` seconds."""

    def __init__(self, sock, seconds, idle):
        self.sock, self.idle = sock, idle
        self.deadline = time.monotonic() + seconds

    def readable(self):
        return True

    def readinto(self, buffer):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the request took too long to arrive")
        self.sock.settimeout(min(left, self.idle))
        try:
            return self.sock.recv_into(buffer)
        finally:
            self.sock.settimeout(self.idle)     # sending the answer: the usual time


class _Handler(WSGIRequestHandler):
    timeout = 30            # seconds: a client that stops sending frees its thread,
    request_seconds = 30    # and so does one whose request takes longer to arrive

    def setup(self):
        super().setup()
        self.rfile.close()      # the plain reader: read through the deadline instead
        self.rfile = io.BufferedReader(
            _RequestDeadline(self.connection, self.request_seconds, self.timeout))

    def handle(self):
        try:
            super().handle()
        except TimeoutError:    # a client too slow: let go, without a traceback
            self.log_error("request timed out")


class _PooledServer(WSGIServer):
    """The bundled server: each connection is handled by one of a fixed set of
    threads, so each thread keeps its database connection from one request to
    the next, and many clients at once wait their turn instead of each
    starting a thread."""
    threads = DEV_SERVER_THREADS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waiting = queue.Queue()
        self.workers = [threading.Thread(target=self._work, daemon=True)
                        for _ in range(self.threads)]
        for worker in self.workers:
            worker.start()

    def process_request(self, request, client_address):
        self.waiting.put((request, client_address))

    def _work(self):
        while (job := self.waiting.get()) is not None:
            try:
                self.finish_request(*job)
            except Exception:
                self.handle_error(*job)
            finally:
                self.shutdown_request(job[0])
        self.get_app().close_connection()

    def server_close(self):
        super().server_close()
        for _ in self.workers:
            self.waiting.put(None)      # each thread stops after what it is doing


def dev_server(app, host="127.0.0.1", port=8000):
    """The bundled development server for `app`; call serve_forever() on it."""
    return make_server(host, port, app, server_class=_PooledServer, handler_class=_Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="LYD wallet: the workers' screens and the API (development server)")
    parser.add_argument("--db", default="wallet.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--insecure-cookies", action="store_true",
                        help="allow the session cookie and the screens over plain http "
                             "(local testing only)")
    parser.add_argument("--trust-proxy", action="store_true",
                        help="take the client address from X-Forwarded-For")
    args = parser.parse_args(argv)
    app = App(args.db, secure_cookies=not args.insecure_cookies, trust_proxy=args.trust_proxy)
    with dev_server(app, args.host, args.port) as server:
        print(f"LYD wallet on http://{args.host}:{args.port}/ (the workers' screens; "
              f"the office's at /admin; API under /api/; database {args.db})", flush=True)
        app.warn_missing_pages()
        server.serve_forever()


if __name__ == "__main__":
    main()
