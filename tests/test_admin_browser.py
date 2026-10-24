"""The office's screens (/admin) in a real browser: Chromium, driven by
Playwright, on a computer-sized screen (and a phone's, to check every screen
fits), against the bundled server on a free port. Skipped without Playwright
or Chromium, like tests/test_browser.py, whose helpers these use."""
import itertools
import os
import tempfile
import threading
import unittest
import unittest.mock
from urllib.parse import urlsplit

from app import auth as A
from app import ledger as L
from app import orders as O
from app import web
from tests.test_browser import CHROMIUM, SKIP, WAIT_MS, _PageLog, _Recorder

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None

DESK = {"width": 1280, "height": 900}
PHONE = {"width": 360, "height": 740}
OFFICE_PHONE, OFFICE_PASSWORD = "0910000000", "Office-Pass-2026"
PASSWORD = "Garden-Lamp-4821"
PHONES = itertools.count(3_000_000)


@unittest.skipIf(SKIP is not None, SKIP or "")
class AdminBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.db = os.path.join(tmp.name, "wallet.db")
        cls.conn = A.connect(cls.db)
        cls.addClassCleanup(cls.conn.close)
        A.create_first_admin(cls.conn, "Office", OFFICE_PHONE, OFFICE_PASSWORD)
        L.add_till(cls.conn, "MAIN", admin_id=1)
        cls.app = web.App(cls.db, secure_cookies=False,
                          login_limiter=web.RateLimiter(1000, 600),
                          account_limiter=web.RateLimiter(1000, 600),
                          worker_limiter=web.RateLimiter(1000, 60))
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
        cls.server.server_close()
        for worker in cls.server.workers:
            worker.join(5)

    def setUp(self):
        self.logs = {}
        self.page = self.new_page()

    def tearDown(self):
        for page, log in self.logs.items():
            self.assertEqual([line for line in log.console
                              if "Content Security Policy" in line or "TrustedHTML" in line],
                             [])
            self.assertEqual(log.errors, [])
            self.assertEqual(log.dialogs, [])
            self.assertEqual([url for _, url, _ in log.requests
                              if not url.startswith(self.base + "/")], [])
            if not page.is_closed():
                self.assert_fits(page)

    # ---------- helpers ----------

    def new_page(self, viewport=DESK):
        context = self.browser.new_context(viewport=viewport)
        context.set_default_timeout(WAIT_MS)
        self.addCleanup(context.close)
        page = context.new_page()
        self.logs[page] = _PageLog(page, True)
        return page

    def idle(self, page=None):
        (page or self.page).locator('body[data-pending="0"]').wait_for(state="attached")

    def on_screen(self, name, page=None):
        page = page or self.page
        page.locator(f'body[data-screen="{name}"]').wait_for(state="attached")
        self.idle(page)

    def click(self, selector, page=None):
        page = page or self.page
        page.click(selector)
        self.idle(page)

    def assert_fits(self, page):
        width, inner = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
        self.assertLessEqual(width, inner, "the screen scrolls sideways")

    def assert_text(self, selector, expected, page=None):
        expect((page or self.page).locator(selector)).to_have_text(expected)

    def log_in(self, page=None, phone=OFFICE_PHONE, password=OFFICE_PASSWORD, screen="overview"):
        page = page or self.page
        page.goto(self.base + "/admin")
        self.on_screen("login", page)
        page.fill("#login-phone", phone)
        page.fill("#login-password", password)
        self.click("#login-submit", page)
        self.on_screen(screen, page)

    def open(self, hash_, screen, page=None):
        page = page or self.page
        page.goto(self.base + "/admin" + hash_)
        self.on_screen(screen, page)

    def answer(self, yes=True):
        """Presses the question box's button (or Cancel) and waits."""
        self.click("#ask-yes" if yes else "#ask-no")

    def worker(self, name="Ali", lyd=0, password=PASSWORD):
        phone = f"092{next(PHONES):07d}"
        uid = L.create_user(self.conn, "customer", name, phone, admin_id=1)
        code = A.issue_setup_code(self.conn, uid, admin_id=1)
        if password:
            A.set_password_with_code(self.conn, phone, code, password)
        if lyd:
            L.deposit(self.conn, uid, lyd * 1000, admin_id=1, into="BANK",
                      idempotency_key=f"start-{uid}")
        return uid, phone

    def count(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def posts(self, path, page=None):
        return [body for method, url, body in self.logs[page or self.page].requests
                if method == "POST" and urlsplit(url).path == path]

    def prices(self):
        if not self.count("SELECT COUNT(*) FROM currencies WHERE code = 'USD'"):
            O.add_currency(self.conn, "USD", admin_id=1)
            O.set_price(self.conn, "USD", "7.85", admin_id=1)

    def order(self, uid, amount="10"):
        quote = O.get_quote(self.conn, uid, "USD", amount)
        return O.place_order(self.conn, uid, quote["quote_id"], "Ahmed Saleh", "P-1234")

    # ---------- tests ----------

    def test_only_office_accounts_get_in(self):
        uid, phone = self.worker("Sara")
        open_sessions = ("SELECT COUNT(*) FROM sessions WHERE user_id = ? "
                         "AND ended_at IS NULL")
        before = self.count(open_sessions, uid)
        page = self.page
        page.goto(self.base + "/admin")
        self.on_screen("login")
        expect(page.locator("#nav")).to_be_hidden()
        page.fill("#login-phone", phone)
        page.fill("#login-password", PASSWORD)
        self.click("#login-submit")
        self.on_screen("login")
        expect(page.locator("#login-error")).to_contain_text("This is a worker's account")
        # Their session was ended at once, not left in the browser.
        self.assertEqual(self.count(open_sessions, uid), before)
        self.log_in(screen="overview")
        expect(page.locator("#nav")).to_be_visible()
        self.assert_text("#bar-who", "Office")
        self.assert_text("#overview-checks", "The books' checks are all fine.")
        expect(page.locator("#overview-holdings")).to_contain_text("TILL_MAIN")

    def test_a_new_worker_gets_a_setup_code_that_works(self):
        self.log_in()
        page = self.page
        self.open("#/workers", "workers")
        page.click("#workers-add-open")
        self.click("#worker-add-submit")
        self.assert_text("#worker-add-error", "Fill in the name and the phone number.")
        page.fill("#worker-add-name", "Mona Ali")
        page.fill("#worker-add-phone", "093 555 0101")
        page.fill("#worker-add-whatsapp", "+20 100 123 4567")
        self.click("#worker-add-submit")
        expect(page.locator("#code-box")).to_be_visible()
        self.assert_text("#code-title", "Mona Ali's account is ready")
        code = page.text_content("#code-value")
        self.assertRegex(code, r"^[A-Z0-9]{4}(-[A-Z0-9]{4})+$")
        row = page.locator("#workers-rows tr", has_text="Mona Ali")
        expect(row).to_contain_text("Waiting for a password")
        expect(row).to_contain_text("093 555 0101")
        # The code works once, for that phone.
        A.set_password_with_code(self.conn, "0935550101", code, PASSWORD)
        user = self.conn.execute("SELECT role, whatsapp FROM users WHERE phone = ?",
                                 ("+218935550101",)).fetchone()
        self.assertEqual(tuple(user), ("customer", "+201001234567"))
        # The same phone again is refused with the wallet's reason, nothing made.
        page.click("#code-done")
        page.click("#workers-add-open")
        page.fill("#worker-add-name", "Someone else")
        page.fill("#worker-add-phone", "0935550101")
        self.click("#worker-add-submit")
        expect(page.locator("#worker-add-error")).to_be_visible()
        self.assertEqual(self.count("SELECT COUNT(*) FROM users WHERE name = 'Someone else'"),
                         0)

    def test_a_deposit_is_made_once_even_when_its_answer_is_lost(self):
        uid, _ = self.worker("Omar")
        self.log_in()
        page = self.page
        self.open(f"#/worker/{uid}", "worker")
        self.assert_text("#worker-name", "Omar")
        self.assert_text("#worker-balance", "0.000 LYD")
        page.fill("#deposit-amount", "abc")
        self.click("#deposit-submit")
        self.assert_text("#deposit-error", "Type the amount in LYD, like 50 or 12.5.")
        page.fill("#deposit-amount", "25.5")
        page.fill("#deposit-note", "cash at the shop")
        self.click("#deposit-submit")
        expect(page.locator("#ask")).to_be_visible()
        self.assert_text("#ask-title", "Deposit 25.500 LYD?")
        # The first answer is lost on the way back: the deposit was made.
        lost = []

        def lose(route):
            if not lost:
                lost.append(route.fetch().status)
                route.abort()
            else:
                route.continue_()
        page.route(lambda url: urlsplit(url).path == "/api/admin/deposit", lose)
        self.answer()
        self.assertEqual(lost, [200])
        expect(page.locator("#ask-error")).to_contain_text("It may have gone through")
        self.answer()               # again: the same key, so not made twice
        expect(page.locator("#ask")).to_be_hidden()
        self.assert_text("#notice", "25.500 LYD deposited for Omar.")
        self.assert_text("#worker-balance", "25.500 LYD")
        [first, second] = self.posts("/api/admin/deposit")
        self.assertEqual(first, second)
        self.assertEqual(self.count("SELECT COUNT(*) FROM transactions t JOIN entries e "
                                    "ON e.txn_id = t.id JOIN accounts a ON a.id = e.account_id "
                                    "WHERE t.kind = 'deposit' AND a.user_id = ?", uid), 1)
        self.assertEqual(L.balance(self.conn, uid), 25_500)
        expect(page.locator("#worker-rows")).to_contain_text("cash at the shop")

    def test_a_cash_withdrawal_says_what_to_hand_over(self):
        uid, _ = self.worker("Khaled", lyd=100)
        L.move_money(self.conn, "BANK", "TILL_MAIN", 100_000, admin_id=1,
                     idempotency_key=f"till-{uid}")
        till = L.holding_amount(self.conn, "TILL_MAIN")
        self.log_in()
        page = self.page
        self.open(f"#/worker/{uid}", "worker")
        page.fill("#withdraw-amount", "50")
        page.fill("#withdraw-fee", "1.5")
        self.click("#withdraw-submit")
        expect(page.locator("#ask-facts")).to_contain_text("48.500 LYD")
        self.answer()
        self.assert_text("#notice", "Withdrawal done: give Khaled 48.500 LYD in cash.")
        self.assert_text("#worker-balance", "50.000 LYD")
        self.assertEqual(L.holding_amount(self.conn, "TILL_MAIN"), till - 48_500)
        self.assertEqual(L.balance(self.conn, uid), 50_000)

    def test_a_reversal_needs_a_reason(self):
        uid, _ = self.worker("Huda", lyd=30)
        self.log_in()
        page = self.page
        self.open(f"#/worker/{uid}", "worker")
        page.locator("#worker-rows").get_by_role("button", name="Reverse").click()
        self.answer()
        self.assert_text("#ask-error", "Fill in every field first.")
        page.fill("#ask-reason", "deposited to the wrong worker")
        self.answer()
        self.assert_text("#notice", "Reversed.")
        self.assert_text("#worker-balance", "0.000 LYD")
        self.assertEqual(L.balance(self.conn, uid), 0)

    def test_blocking_needs_a_reason_and_goes_in_the_log(self):
        uid, _ = self.worker("Yusuf")
        self.log_in()
        page = self.page
        self.open(f"#/worker/{uid}", "worker")
        page.locator("#worker-actions").get_by_role("button", name="Block").click()
        self.assert_text("#ask-title", "Block Yusuf?")
        page.fill("#ask-reason", "checking a complaint")
        self.answer()
        expect(page.locator("#worker-status")).to_contain_text("Blocked")
        expect(page.locator("#worker-status")).to_contain_text("checking a complaint")
        expect(page.locator("#worker-actions")).to_contain_text("Unblock")
        self.assertEqual(self.count("SELECT blocked FROM users WHERE id = ?", uid), 1)
        self.open("#/log", "log")
        first = page.locator("#log-rows tr").first
        expect(first).to_contain_text("Blocked")
        expect(first).to_contain_text("Yusuf")
        expect(first).to_contain_text("checking a complaint")

    def test_an_order_goes_to_a_supplier_and_is_paid_or_cancelled(self):
        self.prices()
        uid, _ = self.worker("Fatma", lyd=200)
        first, second = self.order(uid), self.order(uid, "5")
        supplier = O.add_supplier(self.conn, "Cairo agent", admin_id=1)
        O.set_supplier_price(self.conn, supplier, "USD", "7.80", admin_id=1)
        self.log_in()
        page = self.page
        self.open("#/orders", "orders")
        card = page.locator(f'#orders-list li[data-order="{first["id"]}"]')
        expect(card).to_contain_text("10.00 USD for 78.500 LYD")
        expect(card).to_contain_text("Ahmed Saleh")
        card.get_by_role("button", name="Give to a supplier").click()
        expect(page.locator("#ask-supplier")).to_contain_text("Cairo agent (7.80 LYD)")
        page.select_option("#ask-supplier", str(supplier))
        self.answer()
        self.assert_text("#notice", f"Order {first['receipt_code']} is with Cairo agent.")
        page.select_option("#orders-status", "assigned")
        self.idle()
        card = page.locator(f'#orders-list li[data-order="{first["id"]}"]')
        card.get_by_role("button", name="Mark paid").click()
        self.answer()
        self.assert_text("#ask-error", "Fill in every field first.")
        page.fill("#ask-ref", "CAIRO-0001")
        self.answer()
        self.assert_text("#notice", f"Order {first['receipt_code']} is paid.")
        self.assertEqual(self.count("SELECT status FROM orders WHERE id = ?", first["id"]),
                         "paid")
        # The other is cancelled with a reason: refunded.
        page.select_option("#orders-status", "placed")
        self.idle()
        card = page.locator(f'#orders-list li[data-order="{second["id"]}"]')
        card.get_by_role("button", name="Cancel and refund").click()
        page.fill("#ask-reason", "the beneficiary moved")
        self.answer()
        self.assertEqual(self.count("SELECT status FROM orders WHERE id = ?", second["id"]),
                         "cancelled")
        self.assertEqual(L.balance(self.conn, uid), 200_000 - 78_500)

    def test_currencies_and_prices(self):
        self.log_in()
        page = self.page
        self.open("#/prices", "prices")
        page.fill("#currency-code", "egp")
        self.click("#currency-submit")
        self.assert_text("#currency-done",
                         "EGP is added. Set its price so workers can order it.")
        row = page.locator("#prices-rows tr", has_text="EGP")
        expect(row).to_contain_text("No price yet")
        expect(page.locator("#price-currency")).to_have_value("EGP")
        page.fill("#price-rate", "0.16")
        self.click("#price-submit")
        self.assert_text("#price-done", "EGP is now 0.16 LYD.")
        expect(row).to_contain_text("0.16")
        self.assertEqual(O.current_price(self.conn, "EGP")["rate"], "0.16")
        row.get_by_role("button", name="Switch off").click()
        page.fill("#ask-reason", "no supplier this week")
        self.answer()
        self.assert_text("#notice", "EGP is switched off.")
        expect(row).to_contain_text("No, switched off")
        self.assertEqual(self.count("SELECT active FROM currencies WHERE code = 'EGP'"), 0)

    def test_a_supplier_is_added_given_a_rate_and_paid(self):
        self.prices()
        self.worker("Nadia", lyd=1000)          # the bank has the money to pay with
        self.log_in()
        page = self.page
        self.open("#/suppliers", "suppliers")
        page.fill("#supplier-add-name", "Tunis agent")
        page.fill("#supplier-add-country", "Tunisia")
        self.click("#supplier-add-submit")
        page.locator("#suppliers-rows a", has_text="Tunis agent").click()
        self.on_screen("supplier")
        self.assert_text("#supplier-name", "Tunis agent")
        page.select_option("#supplier-price-currency", "USD")
        page.fill("#supplier-price-rate", "7.70")
        self.click("#supplier-price-submit")
        self.assert_text("#supplier-rates", "USD 7.70")
        page.fill("#pay-amount", "1000")
        page.fill("#pay-reference", "TRANSFER-77")
        self.click("#pay-submit")
        self.assert_text("#ask-title", "Pay Tunis agent 1,000.000 LYD?")
        self.answer()
        self.assert_text("#supplier-balance", "1,000.000 LYD paid in advance")
        expect(page.locator("#supplier-rows")).to_contain_text("TRANSFER-77")

    def test_an_ended_session_goes_back_to_the_login(self):
        self.log_in()
        self.open("#/workers", "workers")
        self.conn.execute("UPDATE sessions SET ended_at = ? WHERE user_id = 1 "
                          "AND ended_at IS NULL", ("2026-01-01T00:00:00+00:00",))
        self.conn.commit()
        self.click("#nav a[data-nav='prices']")
        self.on_screen("login")
        self.assert_text("#notice", "Your session ended. Please log in again.")
        expect(self.page.locator("#nav")).to_be_hidden()

    def test_logging_out(self):
        self.log_in()
        self.open("#/account", "account")
        self.assert_text("#account-name", "Office")
        self.click("#logout")
        self.on_screen("login")
        self.assert_text("#notice", "You're logged out.")
        self.page.goto(self.base + "/admin#/overview")
        self.on_screen("login")

    def test_every_screen_fits_a_phone(self):
        self.prices()
        uid, _ = self.worker("A worker with a rather long name", lyd=20)
        self.order(uid, "1")
        supplier = O.add_supplier(self.conn, "Supplier with a long name", admin_id=1)
        page = self.new_page(PHONE)
        self.log_in(page)
        for hash_, screen in (("#/overview", "overview"), ("#/workers", "workers"),
                              (f"#/worker/{uid}", "worker"), ("#/orders", "orders"),
                              ("#/prices", "prices"), ("#/suppliers", "suppliers"),
                              (f"#/supplier/{supplier}", "supplier"), ("#/log", "log"),
                              ("#/account", "account")):
            with self.subTest(screen):
                self.open(hash_, screen, page)
                self.assert_fits(page)


if __name__ == "__main__":
    unittest.main()
