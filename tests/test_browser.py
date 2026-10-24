"""The workers' screens in a real browser: Chromium, driven by Playwright, on a
phone-sized screen, against the bundled server on a free port.

Run them from the repository's folder, with a Python that has Playwright:
    .venv/bin/pip install playwright
    .venv/bin/python -m unittest tests.test_browser -v
Chromium is taken from /opt/pw-browsers/chromium, or from WALLET_TEST_CHROMIUM.
Without Playwright or that Chromium the browser tests are skipped, so that the
usual `python3 -m unittest` passes without them. RealPagesServedTests needs
no browser and always runs.

The texts the tests expect are read from app/static/strings.js (text()), so
these tests check which line a screen shows, not its wording."""
import functools
import http.server
import io
import itertools
import json
import os
import re
import tempfile
import threading
import unittest
import unittest.mock
from collections import namedtuple
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from wsgiref.util import setup_testing_defaults

from app import auth as A
from app import ledger as L
from app import orders as O
from app import web

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None

STATIC = Path(web.__file__).resolve().parent / "static"     # app/static
CHROMIUM = os.environ.get("WALLET_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
if sync_playwright is None:
    SKIP = "needs Playwright: .venv/bin/pip install playwright"
elif not os.path.exists(CHROMIUM):
    SKIP = f"needs Chromium at {CHROMIUM} (or set WALLET_TEST_CHROMIUM)"
else:
    SKIP = None

# Each page file's type, by its name's ending, as the app must serve it.
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
PHONE_SCREEN = {"width": 360, "height": 740}
WAIT_MS = 10_000                # the longest any step may take
PASSWORD = "Garden-Lamp-4821"   # every test worker's, unless a test says otherwise
PHONES = itertools.count(1_000_000)     # 0921000000, 0921000001, ...: one per worker

Worker = namedtuple("Worker", "id phone password code")

# Every error answer of the wallet that the screens tell apart, (status,
# type, text), and the key of the line it shows. tests/test_web_screens.py
# checks that the wallet still answers with these texts.
WALLET_ERRORS = [
    (401, "NotLoggedIn", "this account can't be used; please contact the office",
     "error.account_unusable"),
    (401, "NotLoggedIn", "please log in", "error.session_ended"),
    (401, "NotLoggedIn", "session expired, please log in again", "error.session_ended"),
    (401, "NotLoggedIn", "logged in as another worker; please log in", "error.session_ended"),
    (403, "PermissionDenied", "this is for workers' accounts", "error.workers_only"),
    (403, "http", "refused: sent from another website", "error.other_site"),
    (401, "LoginFailed", "wrong phone number or password, or too many wrong tries: "
                         "wait 15 minutes", "error.login_failed"),
    (401, "LoginFailed", "wrong phone number or code", "error.setup_failed"),
    (401, "LoginFailed", "this account is closed", "error.account_closed"),
    (403, "AccountBlocked", "this account is blocked; please contact the office",
     "error.account_blocked"),
    (403, "AccountBlocked", "user 7 is blocked", "error.account_blocked"),
    (429, "http", "too many tries from this connection; wait a few minutes",
     "error.too_many_tries"),
    (429, "http", "too many logins to this account; wait a few minutes",
     "error.too_many_logins"),
    (429, "AccountLocked", "too many wrong tries; try again after 09:41 UTC", "error.locked"),
    (403, "WrongPassword", "current password is wrong", "error.wrong_password"),
    (409, "Conflict", "the password was changed meanwhile; try again",
     "error.changed_meanwhile"),
    (400, "LedgerError", "password must be at least 10 characters", "error.password_short"),
    (400, "LedgerError", "password is too long", "error.password_long"),
    (400, "LedgerError", "password is too simple", "error.password_simple"),
    (400, "LedgerError", "password is too common; choose another", "error.password_common"),
    (400, "LedgerError", "password can't contain your phone number", "error.password_phone"),
    (429, "http", "too many requests; wait a minute", "error.too_many_requests"),
    (400, "LedgerError", "phone number: digits only", "error.phone_format"),
    (400, "LedgerError", "phone number must be a Libyan mobile, e.g. 091 234 5678",
     "error.phone_format"),
    (404, "UnknownAccount", "no worker with that phone number", "error.unknown_number"),
    (400, "LedgerError", "cannot send to yourself", "error.send_to_self"),
    (400, "InvalidAmount", "not a number", "error.amount_format"),
    (400, "InvalidAmount", "amount must be a positive whole number of dirhams",
     "error.amount_format"),
    (400, "InvalidAmount", "amount is not a number", "error.amount_format"),
    (400, "InvalidAmount", "amount must be more than zero", "error.amount_format"),
    (400, "InvalidAmount", "amount too large", "error.amount_too_large"),
    (400, "InvalidAmount", "amount is too large", "error.amount_too_large"),
    (400, "InvalidAmount", "at most 3 decimal places", "error.amount_decimals"),
    (400, "InvalidAmount", "amount: at most 2 decimal places", "error.amount_decimals_foreign"),
    (400, "InvalidAmount", "amount too small", "error.amount_too_small"),
    (409, "InsufficientFunds", "balance too low", "error.balance_too_low"),
    (400, "LedgerError", "XX is not available", "error.currency_unavailable"),
    (400, "LedgerError", "no price set for USD", "error.currency_unavailable"),
    (409, "QuoteExpired", "price expired, get a new one", "error.quote_expired"),
    (409, "QuoteExpired", "the price was corrected, get a new one", "error.price_corrected"),
    (409, "Conflict", "this quote was already used for an order to another beneficiary; "
                      "get a new quote", "error.quote_used"),
    (404, "NotFound", "quote not found", "error.quote_not_found"),
    (400, "http", "'phone' is required", "error.fields_missing"),
    (400, "http", "'note' is too long (at most 200 characters)", "error.field_too_long"),
    (500, "internal", "internal error", "error.server"),
    # Any other answer of the wallet's below 500.
    (400, "LedgerError", "idempotency key already used for a different request",
     "error.unknown"),
    (400, "LedgerError", "could not create a receipt code", "error.unknown"),
    (404, "UnknownAccount", "no active customer account for user 7", "error.unknown"),
    (404, "http", "not found", "error.unknown"),
    (405, "http", "method not allowed", "error.unknown"),
    (413, "http", "request too large", "error.unknown"),
    (415, "http", "send JSON with Content-Type: application/json", "error.unknown"),
    (400, "http", "body is not valid JSON", "error.unknown"),
    (418, "Teapot", "short and stout", "error.unknown"),
    # Of 500 or more, but not the wallet's own 500.
    (503, "http", "busy", "error.unavailable"),
]
# Answers that are not the wallet's (a proxy's error page, a captive portal's
# page), and the key of the line each shows.
OTHER_ANSWERS = [
    (502, "<html><body><h1>502 Bad Gateway</h1></body></html>", "error.unavailable"),
    (504, "<html><body><h1>504 Gateway Time-out</h1></body></html>", "error.unavailable"),
    (524, "<html><body><h1>A timeout occurred</h1></body></html>", "error.unavailable"),
    (200, "<html><body><h1>Log in to the hotel Wi-Fi</h1></body></html>",
     "error.unavailable"),
    (200, "", "error.unavailable"),
    (200, {"ok": False}, "error.unavailable"),
    (404, "<html><body>Not Found</body></html>", "error.unavailable"),
    (400, {"error": "no type"}, "error.unavailable"),
]


@functools.lru_cache(maxsize=None)
def strings():
    """STRINGS from app/static/strings.js: the JSON object after
    `window.STRINGS =`."""
    source = (STATIC / "strings.js").read_text("utf-8")
    start = source.index("{", source.index("window.STRINGS ="))
    return json.loads(source[start:source.rindex("}") + 1])


def text(key, **params):
    """A key's English text with its {placeholders} filled in, as the page
    shows it."""
    return re.sub(r"\{([a-z_]+)\}",
                  lambda m: str(params[m[1]]) if m[1] in params else m[0],
                  strings()["en"][key])


def lyd(amount):
    """An amount as the screens show money: "87.500 LYD"."""
    return text("money.lyd", amount=amount)


def answering(status, body, content_type="text/html"):
    """A route handler that answers in the wallet's place, without asking it:
    `body` as JSON when it is a dict, else as it is. (Playwright passes a
    handler the request too when it takes two arguments: this one takes one.)"""
    def handler(route):
        if isinstance(body, dict):
            route.fulfill(status=status, json=body)
        else:
            route.fulfill(status=status, content_type=content_type, body=body)
    return handler


def wallet_error(status, error, kind):
    """A handler answering with an error as the wallet writes one."""
    return answering(status, {"error": error, "type": kind})


class _Links(HTMLParser):
    """The src of every <script> and the href of every <link> in a page."""

    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "src" in attrs:
            self.found.append(attrs["src"])
        if tag == "link" and "href" in attrs:
            self.found.append(attrs["href"])


class RealPagesServedTests(unittest.TestCase):
    """The real files in app/static, as the app serves them. No browser is
    needed, so these run in every suite, also where the browser tests are
    skipped; they are never skipped."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.app = web.App(os.path.join(tmp.name, "wallet.db"))

    def get(self, path):
        """GET `path` from the app in-process: (status, headers, body)."""
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "wsgi.input": io.BytesIO()}
        setup_testing_defaults(environ)
        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"], captured["headers"] = int(status.split()[0]), dict(headers)
        body = b"".join(self.app(environ, start_response))
        return captured["status"], captured["headers"], body

    def test_every_page_file_is_in_app_static(self):
        for name in web.STATIC_FILES.values():
            with self.subTest(name):
                self.assertTrue((STATIC / name).is_file(), f"app/static/{name} is missing")

    def test_every_page_file_is_served_with_its_type_and_policy(self):
        for path, name in web.STATIC_FILES.items():
            with self.subTest(path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], TYPES[Path(name).suffix])
                self.assertEqual(headers["Content-Security-Policy"], web.PAGE_CSP)
                self.assertEqual(headers["Cache-Control"], "no-cache")
                self.assertRegex(headers["ETag"], r'^"[0-9a-f]+"$')
                self.assertEqual(body, (STATIC / name).read_bytes())

    def test_the_page_loads_exactly_the_served_files(self):
        links = _Links()
        links.feed((STATIC / "index.html").read_text("utf-8"))
        self.assertEqual(sorted(links.found),
                         sorted(p for p in web.STATIC_FILES if p.startswith("/static/")))


class _Recorder:
    """The app as the bundled server runs it, noting what each request was
    answered: (method, path, status, whether it came with a cookie, its
    Origin), so a test can see what the server itself saw. It forwards
    close_connection, which each of the server's threads calls when it stops."""

    def __init__(self, app, seen):
        self.app, self.seen = app, seen

    def __call__(self, environ, start_response):
        def noting(status, headers, exc_info=None):
            self.seen.append((environ["REQUEST_METHOD"], environ.get("PATH_INFO"),
                              int(status.split()[0]), "HTTP_COOKIE" in environ,
                              environ.get("HTTP_ORIGIN")))
            return start_response(status, headers, exc_info)
        return self.app(environ, noting)

    def close_connection(self):
        self.app.close_connection()


class _PageLog:
    """What one page did: its requests (method, URL, body), console lines,
    script errors and dialogs. `own` is false for a page of another site."""

    def __init__(self, page, own):
        self.own, self.requests, self.console, self.errors, self.dialogs = own, [], [], [], []
        page.on("request", lambda r: self.requests.append((r.method, r.url, r.post_data)))
        page.on("console", lambda m: self.console.append(m.text))
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", self.on_dialog)

    def on_dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.dismiss()


class _OtherSite(http.server.BaseHTTPRequestHandler):
    """Another website's page, served on another port: an empty page whose
    script (run by the test) posts to the wallet."""

    def do_GET(self):
        body = b"<!doctype html><title>Another site</title><p>Another site</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipIf(SKIP is not None, SKIP or "")
class BrowserTests(unittest.TestCase):
    """Every flow of the workers' screens, as a worker goes through it on a
    phone. One server and one browser for all the tests; each test has its
    own browser context (cookies, storage) and its own workers. After each
    test, no page may have broken the page's policy or thrown an error, and
    the wallet's pages may have asked nothing but the wallet."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.db = os.path.join(tmp.name, "wallet.db")
        cls.conn = A.connect(cls.db)
        cls.addClassCleanup(cls.conn.close)
        A.create_first_admin(cls.conn, "Office", "0910000000", "Office-Pass-2026")
        # Limits high enough for the whole class; a test lowers one only
        # inside try/finally.
        cls.app = web.App(cls.db, secure_cookies=False,
                          login_limiter=web.RateLimiter(1000, 600),
                          account_limiter=web.RateLimiter(1000, 600),
                          worker_limiter=web.RateLimiter(
                              1000, 60, message="too many requests; wait a minute"))
        quiet = unittest.mock.patch.object(web._Handler, "log_message", lambda *a: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)
        cls.seen = []
        cls.server = web.dev_server(_Recorder(cls.app, cls.seen), "127.0.0.1", 0)
        thread = threading.Thread(target=cls.server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        cls.addClassCleanup(cls.stop_server, thread)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=CHROMIUM)
        cls.addClassCleanup(cls.browser.close)
        expect.set_options(timeout=WAIT_MS)

    @classmethod
    def stop_server(cls, thread):
        cls.server.shutdown()
        thread.join(5)
        cls.server.server_close()       # each of its threads stops after what it is doing
        for worker in cls.server.workers:
            worker.join(5)

    def setUp(self):
        self.logs = {}
        self.seen_from = len(self.seen)
        self.page = self.new_page()

    def tearDown(self):
        """No page broke the policy or threw, nothing asked for a dialog, the
        wallet's pages asked only the wallet, and the last screen fits."""
        for page, log in self.logs.items():
            self.assertEqual([line for line in log.console
                              if "Content Security Policy" in line or "TrustedHTML" in line],
                             [])
            self.assertEqual(log.errors, [])
            self.assertEqual(log.dialogs, [])
            if log.own:
                self.assertEqual([url for _, url, _ in log.requests
                                  if not url.startswith(self.base + "/")], [])
                if not page.is_closed():
                    self.assert_fits(page)

    # ---------- pages and waiting ----------

    def new_page(self, context=None, own=True):
        """A page in `context`, or in a new phone-sized one, with its log.
        `own` is false for a page of another site."""
        if context is None:
            context = self.browser.new_context(viewport=PHONE_SCREEN, is_mobile=True,
                                               has_touch=True)
            context.set_default_timeout(WAIT_MS)
            self.addCleanup(context.close)
        page = context.new_page()
        self.logs[page] = _PageLog(page, own)
        return page

    def open(self, page):
        """Opens the wallet and waits until its start-up is over."""
        page.goto(self.base + "/")
        self.wait_idle(page)

    def wait_idle(self, page):
        """Until no user action is under way: every answer handled."""
        page.locator('body[data-pending="0"]').wait_for(state="attached")

    def on_screen(self, page, name):
        """Waits for the screen and its loading, and checks it fits the phone."""
        page.locator(f'body[data-screen="{name}"]').wait_for(state="attached")
        self.wait_idle(page)
        self.assert_fits(page)

    def tap(self, page, selector):
        """Taps (a touch, as on a phone) and waits until what it started is
        over."""
        page.tap(selector)
        self.wait_idle(page)

    def go(self, page, name):
        """Opens a screen with the menu at the bottom."""
        self.tap(page, f'#nav a[data-nav="{name}"]')
        self.on_screen(page, name)

    def assert_fits(self, page):
        width, inner = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
        self.assertLessEqual(width, inner, "the screen scrolls sideways")

    def assert_above_the_bar(self, page, selector):
        """The element is in view whole, not hidden under the bottom bar."""
        top, bottom, bar = page.evaluate(
            "s => [document.querySelector(s).getBoundingClientRect().top,"
            " document.querySelector(s).getBoundingClientRect().bottom,"
            " document.querySelector('#nav').getBoundingClientRect().top]", selector)
        self.assertGreaterEqual(top, 0, f"{selector} is above the screen")
        self.assertLessEqual(bottom, bar, f"{selector} is under the bottom bar")

    def assert_text(self, page, selector, expected):
        """The element shows exactly `expected` (waiting for it a while)."""
        expect(page.locator(selector)).to_have_text(expected)
        self.assertEqual(page.locator(selector).text_content(), expected)

    def assert_step(self, page, screen, step):
        expect(page.locator(f"#screen-{screen}")).to_have_attribute("data-step", step)

    def log_in(self, page, phone, password=PASSWORD):
        """Logs in from the login screen and waits for the home screen."""
        self.on_screen(page, "login")
        page.fill("#login-phone", phone)
        page.fill("#login-password", password)
        self.tap(page, "#login-submit")
        self.on_screen(page, "home")

    def requests(self, page, method, path):
        """The page's requests so far to `path` (any query): [(URL, body)]."""
        return [(url, body) for m, url, body in self.logs[page].requests
                if m == method and urlsplit(url).path == path]

    def server_saw(self, path):
        """What the server answered to `path` during this test:
        [(method, status, with a cookie, Origin)]."""
        return [(method, status, cookie, origin)
                for method, p, status, cookie, origin in self.seen[self.seen_from:]
                if p == path]

    def intercept(self, page, path, handler):
        """Gives the page's requests to `path` (any query) to handler(route)
        until the function returned is called."""
        def matches(url):
            return urlsplit(url).path == path
        page.route(matches, handler)
        return lambda: page.unroute(matches, handler)

    # ---------- the wallet's data ----------

    def worker(self, name="Ali", password=PASSWORD, lyd=0, language="en"):
        """A new worker with a phone of their own (the lock after 5 wrong tries
        is per phone), `lyd` LYD deposited, and the password chosen with a
        setup code; with password=None the code is left unused (Worker.code)."""
        phone = f"092{next(PHONES):07d}"
        uid = L.create_user(self.conn, "customer", name, phone, language, admin_id=1)
        code = A.issue_setup_code(self.conn, uid, admin_id=1)
        if password is not None:
            A.set_password_with_code(self.conn, phone, code, password)
            code = None
        if lyd:
            L.deposit(self.conn, uid, lyd * 1000, admin_id=1, into="BANK",
                      idempotency_key=f"start-{uid}")
        return Worker(uid, phone, password, code)

    def prices(self):
        """USD at 7.85 LYD and EUR at 8.40, set once for the class."""
        if not self.count("SELECT COUNT(*) FROM currencies"):
            for code, rate in (("USD", "7.85"), ("EUR", "8.40")):
                O.add_currency(self.conn, code, admin_id=1)
                O.set_price(self.conn, code, rate, admin_id=1)

    def count(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def transactions(self, kind, user_id):
        """How many `kind` transactions touched the worker's account."""
        return self.count("SELECT COUNT(DISTINCT t.id) FROM transactions t "
                          "JOIN entries e ON e.txn_id = t.id "
                          "JOIN accounts a ON a.id = e.account_id "
                          "WHERE t.kind = ? AND a.user_id = ?", kind, user_id)

    def quotes(self, user_id):
        return self.count("SELECT COUNT(*) FROM quotes WHERE customer_id = ?", user_id)

    def orders(self, user_id):
        return self.count("SELECT COUNT(*) FROM orders WHERE customer_id = ?", user_id)

    # ---------- steps several tests take ----------

    def to_confirm(self, page, phone, amount, note=""):
        """From the send form to its confirm step."""
        page.fill("#send-phone", phone)
        page.fill("#send-amount", amount)
        page.fill("#send-note", note)
        self.tap(page, "#send-next")
        self.assert_step(page, "send", "confirm")

    def to_quote(self, page, amount="10", currency="USD"):
        """From the currency screen's ask step to a quote."""
        page.select_option("#quote-currency", currency)
        page.fill("#quote-amount", amount)
        self.tap(page, "#quote-submit")
        self.assert_step(page, "currency", "quote")

    def place_order(self, page, name="Ahmed Salem", id_number="A1234567"):
        page.fill("#order-name", name)
        page.fill("#order-id", id_number)
        self.tap(page, "#order-submit")

    # ---------- log in, choose a password ----------

    def test_log_in(self):
        ali = self.worker("Ali", lyd=100)
        page = self.page
        self.open(page)
        self.on_screen(page, "login")
        expect(page.locator("#nav")).to_be_hidden()
        page.fill("#login-phone", ali.phone)
        self.tap(page, "#login-submit")
        self.assert_text(page, "#login-error", text("check.required"))
        self.assertEqual(self.requests(page, "POST", "/api/login"), [])
        page.fill("#login-password", "Not-The-Password-1")
        self.tap(page, "#login-submit")
        self.assert_text(page, "#login-error", text("error.login_failed"))
        self.assertEqual(page.input_value("#login-password"), "")
        page.fill("#login-password", ali.password)
        with page.expect_response(lambda r: urlsplit(r.url).path == "/api/login") as answer:
            page.tap("#login-submit")
        self.assertEqual(answer.value.status, 200)
        self.assertNotIn("token", answer.value.json())      # the cookie is enough
        self.on_screen(page, "home")
        self.assert_text(page, "#home-balance", lyd("100.000"))
        self.assert_text(page, "#home-name", text("home.hello", name="Ali"))
        self.assertEqual(page.get_attribute("html", "lang"), "en")
        expect(page.locator("#login-error")).to_be_hidden()

    def test_choose_a_password_with_a_setup_code(self):
        sara = self.worker("Sara", password=None)
        page = self.page
        self.open(page)
        self.tap(page, "#login-to-setup")
        self.on_screen(page, "setup")

        def try_setup(code, password, again=None):
            page.fill("#setup-phone", sara.phone)
            page.fill("#setup-code", code)
            page.fill("#setup-password", password)
            page.fill("#setup-password2", password if again is None else again)
            self.tap(page, "#setup-submit")

        try_setup(sara.code, "password123")
        self.assert_text(page, "#setup-error", text("error.password_common"))
        sent = len(self.requests(page, "POST", "/api/setup-password"))
        try_setup(sara.code, "Blue-River-7310", "Blue-River-7311")
        self.assert_text(page, "#setup-error", text("check.passwords_differ"))
        self.assertEqual(len(self.requests(page, "POST", "/api/setup-password")), sent)
        try_setup("ABCD-EFGH-JKLM", "Blue-River-7310")
        self.assert_text(page, "#setup-error", text("error.setup_failed"))
        # No answer: the code may be used up, so the worker is told to try
        # logging in first.
        stop = self.intercept(page, "/api/setup-password", lambda route: route.abort())
        try_setup(sara.code, "Blue-River-7310")
        stop()
        self.assert_text(page, "#setup-error", text("setup.unsure"))
        expect(page.locator("#setup-to-login")).to_be_visible()
        try_setup(sara.code.lower(), "Blue-River-7310")
        self.on_screen(page, "home")
        self.assert_text(page, "#notice", text("setup.done"))
        self.assert_text(page, "#home-name", text("home.hello", name="Sara"))

    # ---------- balance and statement ----------

    def test_statement_shows_entries_as_text(self):
        name = "Mona <img src=x onerror=alert(1)>"
        note = "<img src=x onerror=alert(2)> rent"
        mona = self.worker(name, lyd=0)
        sam = self.worker("Sam", lyd=50)
        L.deposit(self.conn, mona.id, 100_000, admin_id=1, into="BANK",
                  idempotency_key=f"note-{mona.id}", note=note)
        txn = L.transfer(self.conn, sam.id, mona.id, 12_500, note="<b>lunch</b>")
        page = self.page
        self.open(page)
        self.log_in(page, mona.phone)
        self.assert_text(page, "#home-name", text("home.hello", name=name))
        self.assert_text(page, "#home-balance", lyd("112.500"))
        expect(page.locator("#statement-list > li.entry")).to_have_count(2)
        received = page.locator(f'#statement-list > li.entry[data-kind="transfer"]'
                                f'[data-dir="in"][data-txn-id="{txn}"]')
        self.assertEqual(received.locator(".entry-label").text_content(),
                         text("entry.received"))
        self.assertEqual(received.locator(".entry-amount").text_content(), lyd("+12.500"))
        self.assertEqual(received.locator(".entry-balance").text_content(),
                         text("entry.balance_after", amount=lyd("112.500")))
        self.assertEqual(received.locator(".entry-note").text_content(), "<b>lunch</b>")
        deposit = page.locator('#statement-list > li.entry[data-kind="deposit"][data-dir="in"]')
        self.assertEqual(deposit.locator(".entry-label").text_content(), text("entry.deposit"))
        self.assertEqual(deposit.locator(".entry-amount").text_content(), lyd("+100.000"))
        self.assertEqual(deposit.locator(".entry-balance").text_content(),
                         text("entry.balance_after", amount=lyd("100.000")))
        self.assertEqual(deposit.locator(".entry-note").text_content(), note)
        # Shown as text: no element was made from what the wallet holds.
        self.assertEqual(page.locator("img").count(), 0)
        self.assertEqual(page.locator("#screen-home b").count(), 0)
        expect(page.locator("#statement-empty")).to_be_hidden()
        expect(page.locator("#statement-more")).to_be_hidden()

    def test_workers_texts_keep_their_own_direction(self):
        # A name or note in Arabic reads right to left in the English page:
        # its "!" or "." ends it on the left, not on the right where the
        # Arabic starts. A Latin one stays left to right.
        ali, sami = self.worker("علي", lyd=100), self.worker("سامي")
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "send")
        self.to_confirm(page, sami.phone, "10", "شكرا!")

        def direction(selector):
            return page.evaluate("s => getComputedStyle(document.querySelector(s)).direction",
                                 selector)
        self.assertEqual(direction("#send-confirm-name"), "rtl")
        self.assertEqual(direction("#send-confirm-note"), "rtl")
        # It still lines up with the rest of the page, on the left.
        text_left, box_left = page.evaluate(
            "() => { const el = document.querySelector('#send-confirm-note'),"
            " range = document.createRange(); range.selectNodeContents(el);"
            " return [range.getBoundingClientRect().left, el.getBoundingClientRect().left]; }")
        self.assertAlmostEqual(text_left, box_left, delta=1)
        self.tap(page, "#send-confirm")
        self.tap(page, "#send-done-ok")
        self.on_screen(page, "home")
        self.assertEqual(direction("#statement-list .entry-note"), "rtl")
        self.go(page, "account")
        self.assertEqual(direction("#account-name"), "rtl")
        self.go(page, "send")
        self.to_confirm(page, sami.phone, "1", "rent (half).")
        self.assertEqual(direction("#send-confirm-note"), "ltr")

    def test_statement_shows_50_then_more(self):
        omar = self.worker("Omar")
        for n in range(55):
            L.deposit(self.conn, omar.id, 1000, admin_id=1, into="BANK",
                      idempotency_key=f"many-{omar.id}-{n}")
        page = self.page
        self.open(page)
        self.log_in(page, omar.phone)
        expect(page.locator("#statement-list > li.entry")).to_have_count(50)
        self.assert_text(page, "#home-balance", lyd("55.000"))
        self.tap(page, "#statement-more")
        expect(page.locator("#statement-list > li.entry")).to_have_count(55)
        expect(page.locator("#statement-more")).to_be_hidden()
        self.assertEqual(len(self.requests(page, "GET", "/api/statement")), 2)
        self.assertTrue(self.requests(page, "GET", "/api/statement")[-1][0]
                        .endswith("/api/statement?limit=200"))
        self.assert_fits(page)

    def test_refresh_and_show_more_keep_the_focus(self):
        # With a keyboard (or a screen reader): the button is disabled while
        # it loads, which takes the focus away; it then comes back to it, or,
        # after "Show more" (then hidden), goes to the first entry it added.
        self.prices()
        omar = self.worker("Omar")
        for n in range(55):
            L.deposit(self.conn, omar.id, 1000, admin_id=1, into="BANK",
                      idempotency_key=f"focus-{omar.id}-{n}")
        page = self.page
        self.open(page)
        self.log_in(page, omar.phone)

        def focused():
            return page.evaluate("document.activeElement.id")
        for screen, button in (("home", "home-refresh"), ("currency", "prices-refresh"),
                               ("orders", "orders-refresh")):
            with self.subTest(button):
                self.go(page, screen)
                page.focus(f"#{button}")
                page.keyboard.press("Enter")
                self.wait_idle(page)
                self.assertEqual(focused(), button)
        self.go(page, "home")
        page.focus("#statement-more")
        page.keyboard.press("Enter")
        self.wait_idle(page)
        expect(page.locator("#statement-list > li.entry")).to_have_count(55)
        self.assertTrue(page.evaluate("document.activeElement ==="
                                      " document.querySelectorAll('#statement-list > li')[50]"))

    def test_a_new_worker_has_no_activity(self):
        nour = self.worker("Nour")
        page = self.page
        self.open(page)
        self.log_in(page, nour.phone)
        self.assert_text(page, "#home-balance", lyd("0.000"))
        self.assert_text(page, "#statement-empty", text("home.empty"))
        expect(page.locator("#statement-list > li")).to_have_count(0)

    # ---------- send money ----------

    def test_send_money(self):
        ali, sam = self.worker("Ali", lyd=100), self.worker("Sam")
        nobody = f"092{next(PHONES):07d}"
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "send")
        self.assert_step(page, "send", "form")
        self.assert_text(page, "#send-balance", text("send.balance", amount=lyd("100.000")))

        page.fill("#send-phone", nobody)
        page.fill("#send-amount", "12.5")
        self.tap(page, "#send-next")
        self.assert_text(page, "#send-error", text("error.unknown_number"))
        page.fill("#send-phone", ali.phone)
        self.tap(page, "#send-next")
        self.assert_text(page, "#send-error", text("error.send_to_self"))
        lookups = len(self.requests(page, "GET", "/api/recipient"))
        page.fill("#send-phone", sam.phone)
        page.fill("#send-amount", "1.2345")
        self.tap(page, "#send-next")
        self.assert_text(page, "#send-error", text("check.amount_lyd"))
        self.assertEqual(len(self.requests(page, "GET", "/api/recipient")), lookups)
        self.assert_step(page, "send", "form")

        self.to_confirm(page, sam.phone, "12.5", "for lunch")
        self.assert_text(page, "#send-confirm-name", "Sam")
        self.assert_text(page, "#send-confirm-amount", lyd("12.500"))
        self.assert_text(page, "#send-confirm-note", "for lunch")
        self.assert_text(page, "#send-confirm", text("send.confirm", amount=lyd("12.500")))
        self.assert_fits(page)
        self.tap(page, "#send-confirm")
        self.assert_step(page, "send", "done")
        self.assert_text(page, "#send-done-text",
                         text("send.done_text", amount=lyd("12.500"), name="Sam"))
        self.assert_text(page, "#send-done-balance",
                         text("send.done_balance", amount=lyd("87.500")))
        self.assert_fits(page)
        self.tap(page, "#send-done-ok")
        self.on_screen(page, "home")
        self.assert_text(page, "#home-balance", lyd("87.500"))
        sent = page.locator('#statement-list > li.entry[data-kind="transfer"][data-dir="out"]')
        self.assertEqual(sent.locator(".entry-label").text_content(), text("entry.sent"))
        self.assertEqual(sent.locator(".entry-amount").text_content(), lyd("-12.500"))
        self.assertEqual(sent.locator(".entry-note").text_content(), "for lunch")
        # The home read the balance again after the send.
        methods = [(m, urlsplit(u).path) for m, u, _ in self.logs[page].requests]
        sent = methods.index(("POST", "/api/send"))
        self.assertIn(("GET", "/api/me"), methods[sent + 1:])
        self.assertEqual(L.balance(self.conn, sam.id), 12_500)
        self.assertEqual(self.transactions("transfer", sam.id), 1)
        body = json.loads(self.requests(page, "POST", "/api/send")[0][1])
        self.assertEqual((body["to_phone"], body["amount"], body["note"]),
                         ("+218" + sam.phone[1:], "12.5", "for lunch"))
        self.assertRegex(body["idempotency_key"], r"^web-[0-9a-f]{32}$")

    def test_typed_amounts_are_read_as_the_wallet_takes_them(self):
        # Arabic-Indic and Persian digits, the Arabic decimal mark, spaces and
        # leading zeros: the confirm step shows, and the page posts, the amount
        # the worker meant.
        self.prices()
        ali, sam = self.worker("Ali", lyd=2000), self.worker("Sam")
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "send")
        for typed, shown, posted in (("١٢٫٥", "12.500", "12.5"),
                                     ("۱۲۳۴", "1,234.000", "1234"),
                                     (" 0001234.5 ", "1,234.500", None),
                                     ("1000000", "1,000,000.000", None),
                                     ("٠٫٠٠٥", "0.005", None)):
            with self.subTest(typed=typed):
                self.to_confirm(page, sam.phone, typed)
                self.assert_text(page, "#send-confirm-amount", lyd(shown))
                self.assert_text(page, "#send-confirm", text("send.confirm", amount=lyd(shown)))
                if posted is None:
                    self.tap(page, "#send-edit")
                    continue
                self.tap(page, "#send-confirm")
                self.assert_step(page, "send", "done")
                body = json.loads(self.requests(page, "POST", "/api/send")[-1][1])
                self.assertEqual(body["amount"], posted)
                self.tap(page, "#send-again")
        self.assertEqual(L.balance(self.conn, sam.id), 1_246_500)
        lookups = len(self.requests(page, "GET", "/api/recipient"))
        for typed in ("1,000", "12.", ".5", "0.0001", "0", "١٫٢٣٤٥"):
            with self.subTest(refused=typed):
                page.fill("#send-phone", sam.phone)
                page.fill("#send-amount", typed)
                self.tap(page, "#send-next")
                self.assert_text(page, "#send-error", text("check.amount_lyd"))
                self.assert_step(page, "send", "form")
        self.assertEqual(len(self.requests(page, "GET", "/api/recipient")), lookups)

        self.go(page, "currency")
        self.to_quote(page, "١٠٠٫٥")
        self.assertEqual(json.loads(self.requests(page, "POST", "/api/quote")[-1][1])["amount"],
                         "100.5")
        self.assert_text(page, "#quote-summary",
                         text("quote.summary", amount="100.50", currency="USD",
                              cost=lyd("788.925")))
        self.tap(page, "#order-new-price")
        self.assert_step(page, "currency", "ask")
        page.fill("#quote-amount", "100.505")
        self.tap(page, "#quote-submit")
        self.assert_text(page, "#quote-error", text("check.amount_foreign"))
        self.assertEqual(len(self.requests(page, "POST", "/api/quote")), 1)

    def test_a_double_tap_sends_once(self):
        ali, sam = self.worker("Ali", lyd=100), self.worker("Sam")
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "send")
        self.to_confirm(page, sam.phone, "5")
        page.evaluate("() => { const b = document.getElementById('send-confirm');"
                      " b.click(); b.click(); }")
        self.wait_idle(page)
        self.assert_step(page, "send", "done")
        self.assertEqual(len(self.requests(page, "POST", "/api/send")), 1)
        self.assertEqual(self.transactions("transfer", sam.id), 1)
        self.assertEqual(L.balance(self.conn, sam.id), 5_000)

    def test_a_lost_answer_is_retried_with_the_same_key(self):
        ali, sam = self.worker("Ali", lyd=100), self.worker("Sam")
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "send")

        def lose_answer(route):
            route.fetch()           # the wallet does it...
            route.abort()           # ...and the phone never hears

        def cloudflare_timeout(route):
            route.fetch()
            route.fulfill(status=524, content_type="text/html",
                          body="<html><body><h1>A timeout occurred</h1></body></html>")

        for run, (handler, reload) in enumerate(((lose_answer, False), (lose_answer, True),
                                                 (cloudflare_timeout, False))):
            with self.subTest(run=run, reload=reload):
                posts = len(self.requests(page, "POST", "/api/send"))
                self.to_confirm(page, sam.phone, "2.5")
                stop = self.intercept(page, "/api/send", handler)
                self.tap(page, "#send-confirm")
                self.assert_text(page, "#send-confirm-error", text("send.unsure"))
                self.assert_text(page, "#send-confirm", text("common.retry"))
                expect(page.locator("#send-cancel")).to_be_visible()
                expect(page.locator("#send-edit")).to_be_hidden()
                stop()
                if reload:
                    page.reload()
                    self.on_screen(page, "send")
                    self.assert_step(page, "send", "confirm")
                    self.assert_text(page, "#send-confirm-error", text("send.unsure"))
                    self.assert_text(page, "#send-confirm", text("common.retry"))
                    expect(page.locator("#send-edit")).to_be_hidden()
                self.tap(page, "#send-confirm")
                self.assert_step(page, "send", "done")
                bodies = [json.loads(b) for _, b in self.requests(page, "POST", "/api/send")]
                self.assertEqual(len(bodies), posts + 2)
                self.assertEqual(bodies[-1]["idempotency_key"], bodies[-2]["idempotency_key"])
                self.assertEqual(self.transactions("transfer", sam.id), run + 1)
                self.assertEqual(L.balance(self.conn, sam.id), 2_500 * (run + 1))
                self.tap(page, "#send-again")
                self.assert_step(page, "send", "form")

        # Unsure, and the worker gives up: their activity tells whether it went.
        self.to_confirm(page, sam.phone, "2.5")
        stop = self.intercept(page, "/api/send", lose_answer)
        self.tap(page, "#send-confirm")
        self.assert_text(page, "#send-confirm-error", text("send.unsure"))
        stop()
        self.tap(page, "#send-cancel")
        self.on_screen(page, "home")
        self.assert_text(page, "#notice", text("send.cancelled_check"))
        self.assertEqual(self.transactions("transfer", sam.id), 4)
        transfers = page.locator('#statement-list > li.entry[data-kind="transfer"]')
        expect(transfers).to_have_count(4)
        page.reload()               # nothing is waiting any more
        self.on_screen(page, "home")
        self.assertEqual(len({json.loads(b)["idempotency_key"]
                              for _, b in self.requests(page, "POST", "/api/send")}), 4)

    def test_a_send_cut_off_by_a_logout_waits_for_its_worker(self):
        ali, sam, bob = self.worker("Ali", lyd=100), self.worker("Sam"), self.worker("Bob")
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "send")
        logged_out = wallet_error(401, "please log in", "NotLoggedIn")

        def cut_off(amount):
            """A send answered "logged out": nothing was sent."""
            self.to_confirm(page, sam.phone, amount)
            stop = self.intercept(page, "/api/send", logged_out)
            self.tap(page, "#send-confirm")
            stop()
            self.on_screen(page, "login")
            self.assert_text(page, "#notice", text("error.session_ended"))
            page.fill("#login-password", PASSWORD)

        # The same worker logs in again: the send waits for them, unsure.
        cut_off("3")
        page.fill("#login-phone", ali.phone)
        self.tap(page, "#login-submit")
        self.on_screen(page, "send")
        self.assert_step(page, "send", "confirm")
        self.assert_text(page, "#send-confirm-error", text("send.unsure"))
        self.assert_text(page, "#send-confirm-amount", lyd("3.000"))
        self.tap(page, "#send-confirm")
        self.assert_step(page, "send", "done")
        keys = [json.loads(b)["idempotency_key"]
                for _, b in self.requests(page, "POST", "/api/send")]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(self.transactions("transfer", sam.id), 1)

        # Someone else logs in on the same phone: it is dropped, never shown.
        self.tap(page, "#send-again")
        cut_off("4")
        page.fill("#login-phone", bob.phone)
        self.tap(page, "#login-submit")
        self.on_screen(page, "home")
        self.assertIsNone(page.evaluate("() => sessionStorage.getItem('wallet.pendingSend')"))
        self.go(page, "send")
        self.assert_step(page, "send", "form")
        self.assertEqual(page.input_value("#send-phone"), "")
        self.assertEqual(page.input_value("#send-amount"), "")
        self.assertEqual(self.transactions("transfer", sam.id), 1)

    def test_a_tab_left_open_never_pays_for_whoever_logged_in_since(self):
        ali, bob, sam = (self.worker("Ali", lyd=100), self.worker("Bob", lyd=100),
                         self.worker("Sam"))
        self.prices()
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)

        def bob_logs_in_in_another_tab():
            other = self.new_page(page.context)
            self.open(other)
            self.on_screen(other, "home")
            self.go(other, "account")
            self.tap(other, "#logout")
            self.log_in(other, bob.phone)
            other.close()

        # Ali left the send's confirm step, and the quote step, open.
        for screen in ("send", "currency"):
            with self.subTest(screen):
                self.go(page, screen)
                if screen == "send":
                    self.to_confirm(page, sam.phone, "10")
                else:
                    self.to_quote(page, "1")
                bob_logs_in_in_another_tab()
                if screen == "send":
                    self.tap(page, "#send-confirm")
                else:
                    self.place_order(page)
                self.on_screen(page, "login")
                self.assert_text(page, "#notice", text("error.session_ended"))
                self.assertNotIn(lyd("100.000"), page.content())
                page.fill("#login-phone", ali.phone)
                page.fill("#login-password", PASSWORD)
                self.tap(page, "#login-submit")
                if screen == "send":        # the send waits for Ali, unsure
                    self.on_screen(page, "send")
                    self.tap(page, "#send-cancel")
                else:
                    self.on_screen(page, "currency")
                    self.tap(page, "#order-cancel")
        self.assertEqual((L.balance(self.conn, ali.id), L.balance(self.conn, bob.id),
                          L.balance(self.conn, sam.id)), (100_000, 100_000, 0))
        self.assertEqual(self.orders(bob.id) + self.orders(ali.id), 0)

        # Reading the balance again shows the change too: never Bob's account.
        self.go(page, "home")
        bob_logs_in_in_another_tab()
        self.tap(page, "#home-refresh")
        self.on_screen(page, "login")
        self.assert_text(page, "#notice", text("error.session_ended"))
        self.assertEqual(page.locator("#home-name").text_content(), "")

    def test_a_balance_too_low_goes_back_to_the_form(self):
        ali, sam = self.worker("Ali", lyd=5), self.worker("Sam")
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "send")
        self.to_confirm(page, sam.phone, "10", "rent")
        self.tap(page, "#send-confirm")
        self.assert_step(page, "send", "form")
        self.assert_text(page, "#send-error", text("error.balance_too_low"))
        self.assertEqual(page.input_value("#send-amount"), "10")
        self.assertEqual(page.input_value("#send-note"), "rent")
        self.assertTrue(page.input_value("#send-phone"))
        self.assert_text(page, "#send-balance", text("send.balance", amount=lyd("5.000")))
        self.assertEqual(self.transactions("transfer", sam.id), 0)

    def test_a_late_home_load_never_hides_a_send(self):
        ali, sam = self.worker("Ali", lyd=100), self.worker("Sam")
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        # A Refresh whose statement answers only after a send: it read the
        # balance and the list from before it.
        held = []

        def hold_the_first(route):
            if held:
                route.continue_()
            else:
                held.append((route, route.fetch()))
        stop = self.intercept(page, "/api/statement", hold_the_first)
        page.tap("#home-refresh")
        expect(page.locator("#home-refresh")).to_be_disabled()
        # Waiting on each step, not on the page's being idle: it isn't.
        page.tap('#nav a[data-nav="send"]')
        page.locator('body[data-screen="send"]').wait_for(state="attached")
        page.fill("#send-phone", sam.phone)
        page.fill("#send-amount", "10")
        page.tap("#send-next")
        self.assert_step(page, "send", "confirm")
        page.tap("#send-confirm")
        self.assert_step(page, "send", "done")
        page.tap("#send-done-ok")
        self.assert_text(page, "#home-balance", lyd("90.000"))
        expect(page.locator('#statement-list > li.entry[data-dir="out"]')).to_have_count(1)
        self.assertEqual(len(held), 1)
        route, answer = held[0]
        route.fulfill(response=answer)
        stop()
        self.wait_idle(page)
        self.assert_text(page, "#home-balance", lyd("90.000"))
        expect(page.locator('#statement-list > li.entry[data-dir="out"]')).to_have_count(1)
        expect(page.locator("#home-refresh")).to_be_enabled()

    # ---------- prices, quote and order ----------

    def test_prices_make_no_quotes(self):
        self.prices()
        ali = self.worker("Ali", lyd=1000)
        page = self.page
        page.clock.install()
        self.open(page)
        self.log_in(page, ali.phone)
        # While no currency is offered, there is nothing to ask a price for.
        for code in ("USD", "EUR"):
            O.set_currency_active(self.conn, code, False, admin_id=1, reason="new prices")
        try:
            self.go(page, "currency")
            self.assert_text(page, "#prices-empty", text("currency.empty"))
            expect(page.locator("#quote-form")).to_be_hidden()
        finally:
            for code in ("USD", "EUR"):
                O.set_currency_active(self.conn, code, True, admin_id=1, reason="new prices")
        self.tap(page, "#prices-refresh")
        self.assert_step(page, "currency", "ask")
        expect(page.locator("#prices-empty")).to_be_hidden()
        expect(page.locator("#prices-list > li.price")).to_have_count(2)
        price = page.locator('#prices-list > li.price[data-currency="USD"]')
        self.assertEqual(price.locator(".price-rate").text_content(),
                         text("currency.price_line", currency="USD", rate="7.85"))
        # Typing, choosing, waiting and coming back to the tab ask for nothing.
        page.locator("#quote-amount").press_sequentially("25")
        page.select_option("#quote-currency", "EUR")
        page.select_option("#quote-currency", "USD")
        page.focus("#prices-refresh")
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(300)
        page.clock.fast_forward("05:00")
        page.wait_for_timeout(300)
        self.wait_idle(page)
        self.assertEqual(self.quotes(ali.id), 0)
        self.assertEqual(self.requests(page, "POST", "/api/quote"), [])
        self.assertEqual(self.server_saw("/api/quote"), [])
        self.tap(page, "#quote-submit")
        self.assert_step(page, "currency", "quote")
        self.assertEqual(self.quotes(ali.id), 1)
        self.assertEqual(len(self.requests(page, "POST", "/api/quote")), 1)
        self.assert_text(page, "#quote-summary",
                         text("quote.summary", amount="25.00", currency="USD",
                              cost=lyd("196.250")))

    def test_quote_order_and_receipt(self):
        self.prices()
        ali = self.worker("Ali", lyd=1000)
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "orders")
        self.assert_text(page, "#orders-empty", text("orders.empty"))
        self.go(page, "currency")
        self.to_quote(page, "10")
        self.assert_text(page, "#quote-summary",
                         text("quote.summary", amount="10.00", currency="USD",
                              cost=lyd("78.500")))
        self.assert_text(page, "#order-submit", text("order.submit", cost=lyd("78.500")))
        self.assert_fits(page)
        self.place_order(page)
        self.assert_step(page, "currency", "receipt")
        code, order_id = self.conn.execute(
            "SELECT receipt_code, id FROM orders WHERE customer_id = ?", (ali.id,)).fetchone()
        self.assertEqual(page.locator("#receipt-code").text_content(), code)
        expect(page.locator("#receipt-status")).to_contain_text(text("order.status.placed"))
        self.assert_text(page, "#receipt-balance",
                         text("receipt.balance", amount=lyd("921.500")))
        self.assert_fits(page)
        methods = [(m, urlsplit(u).path) for m, u, _ in self.logs[page].requests]
        placed = methods.index(("POST", "/api/orders"))
        self.assertIn(("GET", "/api/me"), methods[placed + 1:])

        self.tap(page, "#receipt-orders")
        self.on_screen(page, "orders")
        item = page.locator(f'#orders-list > li.order[data-receipt="{code}"]')
        expect(item).to_have_attribute("data-status", "placed")
        self.assertEqual(item.locator(".order-status").text_content(),
                         text("order.status.placed"))
        self.assertEqual(item.locator(".order-code").text_content(), code)
        # The office works on it: the list changes on Refresh only.
        supplier = O.add_supplier(self.conn, "Cairo agent", admin_id=1)
        O.set_supplier_price(self.conn, supplier, "USD", "7.80", admin_id=1)
        O.assign_supplier(self.conn, order_id, supplier, admin_id=1)
        expect(item).to_have_attribute("data-status", "placed")
        self.tap(page, "#orders-refresh")
        expect(item).to_have_attribute("data-status", "assigned")
        self.assertEqual(item.locator(".order-status").text_content(),
                         text("order.status.assigned"))
        O.mark_paid(self.conn, code, "CAIRO-0001", admin_id=1)
        self.tap(page, "#orders-refresh")
        expect(item).to_have_attribute("data-status", "paid")
        self.assertEqual(item.locator(".order-status").text_content(),
                         text("order.status.paid"))

        # A double tap on "Place order" orders and charges once.
        self.go(page, "currency")
        self.assert_step(page, "currency", "ask")
        self.to_quote(page, "10")
        page.fill("#order-name", "Ahmed Salem")
        page.fill("#order-id", "A1234567")
        posts = len(self.requests(page, "POST", "/api/orders"))
        page.evaluate("() => { const b = document.getElementById('order-submit');"
                      " b.click(); b.click(); }")
        self.wait_idle(page)
        self.assert_step(page, "currency", "receipt")
        self.assertEqual(len(self.requests(page, "POST", "/api/orders")), posts + 1)
        self.assertEqual(self.orders(ali.id), 2)
        self.assertEqual(self.transactions("order", ali.id), 2)
        self.assertEqual(L.balance(self.conn, ali.id), 1_000_000 - 2 * 78_500)

        # The office cancels the second one: the money comes back.
        second_code, second_id = self.conn.execute(
            "SELECT receipt_code, id FROM orders WHERE customer_id = ? AND id != ?",
            (ali.id, order_id)).fetchone()
        O.cancel_order(self.conn, second_id, admin_id=1, reason="the beneficiary moved")
        self.go(page, "orders")
        second = page.locator(f'#orders-list > li.order[data-receipt="{second_code}"]')
        expect(second).to_have_attribute("data-status", "cancelled")
        self.assertEqual(second.locator(".order-status").text_content(),
                         text("order.status.cancelled"))
        self.go(page, "home")
        self.assert_text(page, "#home-balance", lyd("921.500"))
        back = page.locator('#statement-list > li.entry[data-kind="reversal"][data-dir="in"]')
        self.assertEqual(back.locator(".entry-label").text_content(),
                         text("entry.order_cancelled"))
        self.assertEqual(back.locator(".entry-amount").text_content(), lyd("+78.500"))
        first = page.locator('#statement-list > li.entry[data-kind="order"]').last
        self.assertEqual(first.locator(".entry-label").text_content(),
                         text("entry.order", code=code))
        self.assertEqual(first.locator(".entry-amount").text_content(), lyd("-78.500"))

    def test_the_receipt_fits_a_small_phone_with_larger_text(self):
        # A 320 px phone with its text at 130%, and the same phone with the
        # page zoomed to 130% (246 px wide): the code stays inside its card.
        self.prices()
        ali = self.worker("Ali", lyd=1000)
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        for width, text_size in ((320, "130%"), (246, "100%")):
            with self.subTest(width=width, text_size=text_size):
                page.set_viewport_size({"width": width, "height": 740})
                page.evaluate("s => { document.documentElement.style.fontSize = s; }",
                              text_size)
                self.go(page, "currency")
                self.to_quote(page, "10")
                self.place_order(page)
                self.assert_step(page, "currency", "receipt")
                self.assert_fits(page)
                inside = page.evaluate(
                    "() => { const code = document.getElementById('receipt-code'),"
                    " card = code.parentElement.getBoundingClientRect();"
                    " return code.scrollWidth <= code.clientWidth && [...code.children]"
                    ".every(group => { const box = group.getBoundingClientRect();"
                    " return box.left >= card.left && box.right <= card.right; }); }")
                self.assertTrue(inside, "the receipt code runs out of its card")
                self.tap(page, "#receipt-new")

    def test_the_menu_never_splits_a_word(self):
        # A label too wide for its fifth of the bar is cut short, on one
        # line ("Curren…"), not split over two ("Curren" and "cy").
        page = self.page
        self.open(page)
        self.log_in(page, self.worker("Ali").phone)
        for width, text_size in ((277, "100%"), (246, "100%"), (320, "130%")):
            with self.subTest(width=width, text_size=text_size):
                page.set_viewport_size({"width": width, "height": 740})
                page.evaluate("s => { document.documentElement.style.fontSize = s; }",
                              text_size)
                for name in ("home", "currency", "account"):
                    self.go(page, name)
                    lines = page.evaluate(
                        "() => [...document.querySelectorAll('#nav a span')].map(label =>"
                        " [label.textContent, Math.round(label.getBoundingClientRect().height"
                        " / parseFloat(getComputedStyle(label).lineHeight))])")
                    self.assertEqual([n for _, n in lines], [1] * 5, lines)
                    self.assertEqual([label for label, _ in lines],
                                     [text(f"nav.{n}") for n in
                                      ("home", "send", "currency", "orders", "account")])

    def test_an_expired_quote_goes_back_to_asking(self):
        self.prices()
        ali = self.worker("Ali", lyd=1000)
        page = self.page
        page.clock.install()
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "currency")
        self.to_quote(page, "10")
        expect(page.locator("#quote-left")).not_to_be_empty()
        page.clock.fast_forward("10:00")
        self.assert_step(page, "currency", "ask")
        self.assert_text(page, "#quote-notice", text("error.quote_expired"))
        self.wait_idle(page)
        self.assertEqual(self.quotes(ali.id), 1)          # no new one asked for
        self.assertEqual(len(self.requests(page, "POST", "/api/quote")), 1)

        for error, kind, key in (
                ("price expired, get a new one", "QuoteExpired", "error.quote_expired"),
                ("this quote was already used for an order to another beneficiary; "
                 "get a new quote", "Conflict", "error.quote_used")):
            with self.subTest(key):
                self.to_quote(page, "10")
                stop = self.intercept(page, "/api/orders", wallet_error(409, error, kind))
                self.place_order(page)
                stop()
                self.assert_step(page, "currency", "ask")
                self.assert_text(page, "#quote-notice", text(key))
                # The beneficiary stays typed for the next price.
                self.assertEqual(page.input_value("#order-name"), "Ahmed Salem")
        self.assertEqual(self.orders(ali.id), 0)

    def test_an_order_answer_lost_near_expiry_is_never_ordered_twice(self):
        self.prices()
        ali = self.worker("Ali", lyd=1000)
        page = self.page
        page.clock.install()
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "currency")
        self.to_quote(page, "10")
        quotes = self.quotes(ali.id)

        def lose_answer(route):
            route.fetch()
            route.abort()
        stop = self.intercept(page, "/api/orders", lose_answer)
        self.place_order(page)
        self.assert_text(page, "#order-error", text("order.unsure"))
        self.assert_text(page, "#order-submit", text("common.retry"))
        expect(page.locator("#order-cancel")).to_be_visible()
        expect(page.locator("#order-new-price")).to_be_hidden()
        # Its time runs out while it is unsure: the step stays, with a line.
        page.clock.fast_forward("10:00")
        self.assert_text(page, "#quote-left", text("quote.expired_wait"))
        self.assert_step(page, "currency", "quote")
        page.reload()
        self.on_screen(page, "currency")
        self.assert_step(page, "currency", "quote")
        self.assert_text(page, "#order-error", text("order.unsure"))
        self.assert_text(page, "#order-submit", text("common.retry"))
        expect(page.locator("#order-new-price")).to_be_hidden()
        stop()
        self.tap(page, "#order-submit")
        self.assert_step(page, "currency", "receipt")
        code = self.conn.execute("SELECT receipt_code FROM orders WHERE customer_id = ?",
                                 (ali.id,)).fetchone()[0]
        self.assertEqual(page.locator("#receipt-code").text_content(), code)
        self.assertEqual(self.orders(ali.id), 1)
        self.assertEqual(self.transactions("order", ali.id), 1)
        self.assertEqual(self.quotes(ali.id), quotes)
        bodies = [json.loads(b) for _, b in self.requests(page, "POST", "/api/orders")]
        self.assertEqual(len({b["quote_id"] for b in bodies}), 1)

        # Unsure, and the worker gives up: their orders tell whether it went.
        self.go(page, "currency")
        self.to_quote(page, "20")
        stop = self.intercept(page, "/api/orders", lose_answer)
        self.place_order(page)
        self.assert_text(page, "#order-error", text("order.unsure"))
        stop()
        self.tap(page, "#order-cancel")
        self.on_screen(page, "orders")
        self.assert_text(page, "#notice", text("order.cancelled_check"))
        expect(page.locator("#orders-list > li.order")).to_have_count(2)
        page.reload()               # nothing is waiting any more
        self.on_screen(page, "orders")
        self.assertEqual(self.orders(ali.id), 2)

    def test_a_refused_order_keeps_its_line_when_the_price_runs_out(self):
        # 10 USD costs 78.500 LYD: more than Ali has, so every order is refused.
        self.prices()
        ali = self.worker("Ali", lyd=10)
        page = self.page
        page.clock.install()
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "currency")

        def lose_answer(route):
            route.fetch()
            route.abort()
        for unsure_first in (False, True):
            with self.subTest(unsure_first=unsure_first):
                self.to_quote(page, "10")
                if unsure_first:
                    # The answer is lost and the price runs out meanwhile;
                    # Try again is then refused.
                    stop = self.intercept(page, "/api/orders", lose_answer)
                    self.place_order(page)
                    self.assert_text(page, "#order-error", text("order.unsure"))
                    page.clock.fast_forward("10:00")
                    self.assert_text(page, "#quote-left", text("quote.expired_wait"))
                    stop()
                    self.tap(page, "#order-submit")
                else:
                    self.place_order(page)
                    self.assert_step(page, "currency", "quote")
                    self.assert_text(page, "#order-error", text("error.balance_too_low"))
                    page.clock.fast_forward("10:00")
                # The countdown goes back to ask with the refusal's line, not
                # with "This price has expired".
                page.clock.fast_forward("00:02")
                self.assert_step(page, "currency", "ask")
                self.assert_text(page, "#quote-notice", text("error.balance_too_low"))
                expect(page.locator("#order-error")).to_be_hidden()
                self.assertEqual(page.input_value("#order-name"), "Ahmed Salem")
        self.assertEqual(self.orders(ali.id), 0)
        self.assertEqual(L.balance(self.conn, ali.id), 10_000)

    # ---------- error lines and limits ----------

    def test_error_lines(self):
        ali = self.worker("Ali", lyd=100)
        page = self.page
        page.clock.install()
        self.open(page)
        self.log_in(page, ali.phone)

        stop = self.intercept(page, "/api/me", lambda route: route.abort())
        page.reload()
        self.on_screen(page, "offline")
        self.assert_text(page, "#offline-text", text("error.network"))
        stop()
        self.tap(page, "#offline-retry")
        self.on_screen(page, "home")
        self.assert_text(page, "#home-balance", lyd("100.000"))

        for status, handler, key in (
                (500, wallet_error(500, "internal error", "internal"), "error.server"),
                (502, answering(502, "<html><body><h1>502 Bad Gateway</h1></body></html>"),
                 "error.unavailable"),
                (418, wallet_error(418, "teapot", "Teapot"), "error.unknown")):
            with self.subTest(status=status):
                stop = self.intercept(page, "/api/statement", handler)
                self.tap(page, "#home-refresh")
                stop()
                self.assert_text(page, "#home-error", text(key))
                self.on_screen(page, "home")
        self.assertNotIn("teapot", page.content().lower())     # the wallet's text isn't shown

        held = []

        def hold(route):            # never answered
            held.append(route)
        stop = self.intercept(page, "/api/statement", hold)
        with page.expect_request(lambda r: urlsplit(r.url).path == "/api/statement"):
            page.tap("#home-refresh")
        page.clock.fast_forward("00:21")
        self.assert_text(page, "#home-error", text("error.timeout"))
        self.wait_idle(page)
        for route in held:          # the page gave up on it; let it go
            try:
                route.abort()
            except Exception:
                pass
        stop()

    def test_every_answer_shows_its_own_line(self):
        """Each answer the screens tell apart shows exactly its own line.
        Faked on the login form, which shows any line: the wallet's errors,
        answers that are not the wallet's, and no answer at all. The line is
        its key's text and nothing else: the wallet's own text never shows."""
        page = self.page
        self.open(page)
        cases = [(f"{status} {kind}: {error}", wallet_error(status, error, kind), key)
                 for status, kind, error, key in WALLET_ERRORS]
        cases += [(f"{status} {body!r}", answering(status, body), key)
                  for status, body, key in OTHER_ANSWERS]
        cases.append(("no answer", lambda route: route.abort(), "error.network"))
        for name, handler, key in cases:
            with self.subTest(name):
                self.on_screen(page, "login")
                page.fill("#login-phone", "0921234567")
                page.fill("#login-password", "Some-Password-1")
                stop = self.intercept(page, "/api/login", handler)
                self.tap(page, "#login-submit")
                stop()
                # Logged out: the login screen again, with the line above it.
                where = "#notice" if key in ("error.account_unusable",
                                             "error.session_ended") else "#login-error"
                self.assert_text(page, where, text(key))
                self.on_screen(page, "login")
        # Only start-up asked who is logged in: no answer passed for a login.
        self.assertEqual(len(self.requests(page, "GET", "/api/me")), 1)

    def test_limits(self):
        ali, sam = self.worker("Ali", lyd=100), self.worker("Sam")
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "send")
        limiter = self.app.worker_limiter
        self.app.worker_limiter = web.RateLimiter(1, 60,
                                                  message="too many requests; wait a minute")
        try:
            self.to_confirm(page, sam.phone, "1")
            self.tap(page, "#send-edit")
            self.assert_step(page, "send", "form")
            self.tap(page, "#send-next")
            self.assert_text(page, "#send-error", text("error.too_many_requests"))
            self.assert_step(page, "send", "form")
        finally:
            self.app.worker_limiter = limiter

        other = self.new_page()
        self.open(other)
        limiter = self.app.login_limiter
        self.app.login_limiter = web.RateLimiter(1, 600)
        try:
            for key in ("error.login_failed", "error.too_many_tries"):
                other.fill("#login-phone", sam.phone)
                other.fill("#login-password", "Not-The-Password-1")
                self.tap(other, "#login-submit")
                self.assert_text(other, "#login-error", text(key))
        finally:
            self.app.login_limiter = limiter

    # ---------- the account ----------

    def test_change_password_and_log_out(self):
        ali = self.worker("Ali")
        new = "Quiet-Harbor-5512"
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        self.go(page, "account")
        self.assert_text(page, "#account-name", "Ali")

        def change(old, password, again=None):
            page.fill("#password-old", old)
            page.fill("#password-new", password)
            page.fill("#password-new2", password if again is None else again)
            self.tap(page, "#password-submit")

        change("Not-The-Password-1", new)
        self.assert_text(page, "#password-error", text("error.wrong_password"))
        self.assertEqual(page.input_value("#password-old"), "")
        sent = len(self.requests(page, "POST", "/api/change-password"))
        change(ali.password, new, new + "x")
        self.assert_text(page, "#password-error", text("check.passwords_differ"))
        self.assertEqual(len(self.requests(page, "POST", "/api/change-password")), sent)
        change(ali.password, new)
        self.assert_text(page, "#password-done", text("password.done"))
        expect(page.locator("#password-error")).to_be_hidden()
        self.assertEqual(page.input_value("#password-new"), "")

        self.tap(page, "#logout")
        self.on_screen(page, "login")
        self.assert_text(page, "#notice", text("logout.done"))
        expect(page.locator("#nav")).to_be_hidden()
        page.fill("#login-phone", ali.phone)
        page.fill("#login-password", ali.password)
        self.tap(page, "#login-submit")
        self.assert_text(page, "#login-error", text("error.login_failed"))
        self.log_in(page, ali.phone, new)

    def test_log_out_everywhere(self):
        ali = self.worker("Ali", lyd=10)
        here, there = self.page, self.new_page()
        for page in (here, there):
            self.open(page)
            self.log_in(page, ali.phone)
        self.go(here, "account")
        expect(here.locator("#logout-all-confirm")).to_be_hidden()
        self.tap(here, "#logout-all")
        expect(here.locator("#logout-all-confirm")).to_be_visible()
        self.assert_above_the_bar(here, "#logout-all-confirm")    # its buttons too
        self.assert_fits(here)
        self.tap(here, "#logout-all-yes")
        self.on_screen(here, "login")
        self.assert_text(here, "#notice", text("logout.all_done"))
        self.tap(there, "#home-refresh")
        self.on_screen(there, "login")
        self.assert_text(there, "#notice", text("error.session_ended"))
        # Nothing of the worker's is left on the screens.
        self.assertEqual(there.locator("#statement-list > li").count(), 0)
        self.assertEqual(there.locator("#home-balance").text_content(), "")

    def test_a_blocked_worker(self):
        ali = self.worker("Ali", lyd=10)
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        L.block_user(self.conn, ali.id, admin_id=1, reason="checking a complaint")
        self.tap(page, "#home-refresh")
        self.on_screen(page, "login")
        self.assert_text(page, "#notice", text("error.session_ended"))
        page.fill("#login-phone", ali.phone)
        page.fill("#login-password", ali.password)
        self.tap(page, "#login-submit")
        self.assert_text(page, "#login-error", text("error.account_blocked"))
        self.on_screen(page, "login")

        # A session that outlived a block is rare (the block ends the sessions):
        # that answer is faked.
        other = self.new_page()
        self.intercept(other, "/api/me", wallet_error(
            401, "this account can't be used; please contact the office", "NotLoggedIn"))
        self.open(other)
        self.on_screen(other, "login")
        self.assert_text(other, "#notice", text("error.account_unusable"))

    def test_an_admin_is_told_the_screens_are_for_workers(self):
        page = self.page
        self.open(page)
        self.on_screen(page, "login")
        page.fill("#login-phone", "0910000000")
        page.fill("#login-password", "Office-Pass-2026")
        self.tap(page, "#login-submit")
        self.on_screen(page, "login")
        self.assert_text(page, "#login-error", text("error.workers_only"))
        expect(page.locator("#nav")).to_be_hidden()
        self.assertEqual(page.context.request.get(self.base + "/api/me").status, 401)

    def test_the_screens_are_in_english_whatever_the_language(self):
        for language in ("fr", "ar"):
            with self.subTest(language):
                worker = self.worker("Ali", language=language)
                page = self.new_page()
                self.open(page)
                self.log_in(page, worker.phone)
                self.assertEqual(page.get_attribute("html", "lang"), "en")
                self.assertEqual(page.get_attribute("html", "dir"), "ltr")
                self.assert_text(page, "#home-name", text("home.hello", name="Ali"))
                self.assert_text(page, "#home-balance", lyd("0.000"))
                self.assert_text(page, "#statement-empty", text("home.empty"))
                self.assertEqual(page.title(), text("app.title"))

    # ---------- another website, the page files kept, no polling ----------

    def test_another_website_cant_post_with_the_session(self):
        ali = self.worker("Ali", lyd=10)
        page = self.page
        self.open(page)
        self.log_in(page, ali.phone)
        open_sessions = "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND ended_at IS NULL"
        sessions = self.count(open_sessions, ali.id)
        other_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OtherSite)
        threading.Thread(target=other_server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(other_server.server_close)
        self.addCleanup(other_server.shutdown)
        port = other_server.server_port
        # Another site's page in the worker's browser: it shares the cookies.
        other = self.new_page(page.context, own=False)
        post = """async (url) => {
            const out = {};
            try {       // a form-like post: no preflight, the answer can't be read
                const r = await fetch(url, {method: "POST", mode: "no-cors",
                    credentials: "include", headers: {"Content-Type": "text/plain"},
                    body: "{}"});
                out.plain = r.type;
            } catch (e) { out.plain = "failed"; }
            try {       // JSON needs a preflight, which the wallet never grants
                await fetch(url, {method: "POST", credentials: "include",
                    headers: {"Content-Type": "application/json"}, body: "{}"});
                out.json = "answered";
            } catch (e) { out.json = "refused"; }
            return out;
        }"""
        # 127.0.0.1 on another port is the same site (SameSite lets the cookie
        # go); localhost is another site (no cookie).
        for host, with_cookie in (("127.0.0.1", True), ("localhost", False)):
            with self.subTest(host):
                self.seen_from = len(self.seen)
                origin = f"http://{host}:{port}"
                other.goto(origin + "/")
                result = other.evaluate(post, self.base + "/api/logout-everywhere")
                self.assertEqual(result, {"plain": "opaque", "json": "refused"})
                self.assertEqual(sorted(self.server_saw("/api/logout-everywhere")),
                                 [("OPTIONS", 403, False, origin),
                                  ("POST", 403, with_cookie, origin)])
        self.tap(page, "#home-refresh")
        self.on_screen(page, "home")
        self.assert_text(page, "#home-balance", lyd("10.000"))
        expect(page.locator("#home-error")).to_be_hidden()
        self.assertEqual(self.count(open_sessions, ali.id), sessions)     # none was ended

    def test_the_page_files_are_kept_and_checked_again(self):
        page = self.page
        first = page.goto(self.base + "/")
        self.on_screen(page, "login")
        self.assertEqual(first.headers["content-security-policy"], web.PAGE_CSP)
        self.assertEqual(first.headers["cache-control"], "no-cache")
        assets = [p for p in web.STATIC_FILES if p.endswith((".js", ".css"))]
        self.assertEqual([self.server_saw(p) for p in ["/"] + assets],
                         [[("GET", 200, False, None)]] * (1 + len(assets)))
        # Opened again, every file is asked for (a new version is seen at
        # once); the scripts and the styles kept from the first time are only
        # checked, and answered "not changed" without the file. (Chromium asks
        # for the page itself and the tab's icon in full each time.)
        self.seen_from = len(self.seen)
        again = page.goto(self.base + "/")
        self.on_screen(page, "login")
        self.assertEqual(again.headers["content-security-policy"], web.PAGE_CSP)
        self.assertEqual([method for method, *_ in self.server_saw("/")], ["GET"])
        self.assertEqual([self.server_saw(p) for p in assets],
                         [[("GET", 304, False, None)]] * len(assets))

    def test_no_screen_asks_anything_on_its_own(self):
        self.prices()
        ali = self.worker("Ali", lyd=10)
        page = self.page
        page.clock.install()
        self.open(page)
        self.log_in(page, ali.phone)
        for name in ("home", "currency", "orders", "send", "account"):
            with self.subTest(name):
                if name != "home":
                    self.go(page, name)
                asked, answered = len(self.logs[page].requests), len(self.seen)
                page.clock.fast_forward("30:00")
                page.wait_for_timeout(300)
                self.assertEqual(self.logs[page].requests[asked:], [])
                self.assertEqual(self.seen[answered:], [])
                self.assertEqual(page.get_attribute("body", "data-screen"), name)


if __name__ == "__main__":
    unittest.main()
