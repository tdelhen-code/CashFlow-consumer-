import unittest
from datetime import datetime, timedelta, timezone

from app import api
from app import auth as A
from app import ledger as L
from app import orders as O

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
ADMIN_PW = "Office-Pass-2026"
ALI_PW = "ali-wallet-77"


def lyd(x):
    return L.parse_lyd(x)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.db = A.connect(":memory:")
        self.addCleanup(self.db.close)
        self.admin = A.create_first_admin(self.db, "Admin", "0910000000", ADMIN_PW)
        self.ali = L.create_user(self.db, "customer", "Ali", "0911111111",
                                 admin_id=self.admin)
        self.sam = L.create_user(self.db, "customer", "Sam", "0922222222",
                                 admin_id=self.admin)
        code = A.issue_setup_code(self.db, self.ali, admin_id=self.admin, now=T0)
        A.set_password_with_code(self.db, "0911111111", code, ALI_PW, now=T0)

    def login_ali(self, password=ALI_PW, at=T0):
        return A.login(self.db, "091 111 1111", password, now=at)

    # --- logging in ---
    def test_login_with_phone_in_any_form(self):
        for phone in ("0910000000", "+218 91 000 0000", "00218910000000"):
            token = A.login(self.db, phone, ADMIN_PW, now=T0)
            self.assertEqual(A.authenticate(self.db, token, now=T0)["id"], self.admin)

    def test_unknown_phone_and_wrong_password_look_the_same(self):
        errors = []
        for phone, password in (("0911111111", "wrong-password"),
                                ("0933333333", ALI_PW), ("not a phone", ALI_PW)):
            with self.assertRaises(A.LoginFailed) as caught:
                A.login(self.db, phone, password, now=T0)
            errors.append(str(caught.exception))
        self.assertEqual(len(set(errors)), 1)

    def test_secrets_are_stored_only_as_hashes(self):
        token = self.login_ali()
        code = A.issue_setup_code(self.db, self.sam, admin_id=self.admin, now=T0)
        dump = "\n".join(self.db.iterdump())
        for secret in (token, code, ALI_PW, ADMIN_PW):
            self.assertNotIn(secret, dump)
        stored = self.db.execute("SELECT password_hash FROM users WHERE id = ?",
                                 (self.ali,)).fetchone()[0]
        self.assertTrue(stored.startswith("scrypt$"))

    # --- setup codes and passwords ---
    def test_setup_code_works_once_and_expires(self):
        code = A.issue_setup_code(self.db, self.sam, admin_id=self.admin, now=T0)
        with self.assertRaises(A.LoginFailed):      # not yet a password
            A.login(self.db, "0922222222", "", now=T0)
        late = T0 + timedelta(hours=A.SETUP_CODE_HOURS)
        with self.assertRaises(A.LoginFailed):
            A.set_password_with_code(self.db, "0922222222", code, "sam-pass-99", now=late)
        code = A.issue_setup_code(self.db, self.sam, admin_id=self.admin, now=T0)
        token = A.set_password_with_code(self.db, "0922222222", code.lower(),
                                         "sam-pass-99", now=T0)
        self.assertEqual(A.authenticate(self.db, token, now=T0)["id"], self.sam)
        with self.assertRaises(A.LoginFailed):
            A.set_password_with_code(self.db, "0922222222", code, "sam-pass-00", now=T0)

    def test_new_code_resets_password_and_ends_sessions(self):
        token = self.login_ali()
        old = A.issue_setup_code(self.db, self.ali, admin_id=self.admin, now=T0)
        new = A.issue_setup_code(self.db, self.ali, admin_id=self.admin, now=T0)
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0)
        with self.assertRaises(A.LoginFailed):
            self.login_ali()
        with self.assertRaises(A.LoginFailed):
            A.set_password_with_code(self.db, "0911111111", old, "brand-new-1", now=T0)
        A.set_password_with_code(self.db, "0911111111", new, "brand-new-1", now=T0)
        self.login_ali("brand-new-1")

    def test_only_admins_issue_codes(self):
        with self.assertRaises(L.PermissionDenied):
            A.issue_setup_code(self.db, self.sam, admin_id=self.ali, now=T0)

    def test_password_rules(self):
        code = A.issue_setup_code(self.db, self.sam, admin_id=self.admin, now=T0)
        for bad in ("short1", "aaaaaaaaaa", "1212121212", "0922222222", "922222222",
                    "x" * 200):
            with self.assertRaises(L.LedgerError, msg=bad):
                A.set_password_with_code(self.db, "0922222222", code, bad, now=T0)
        A.set_password_with_code(self.db, "0922222222", code, "fine-pass-1", now=T0)

    def test_change_password_keeps_this_session_and_ends_others(self):
        here = self.login_ali()
        there = self.login_ali()
        with self.assertRaises(A.LoginFailed):
            A.change_password(self.db, here, "wrong", "next-pass-22", now=T0)
        A.change_password(self.db, here, ALI_PW, "next-pass-22", now=T0)
        A.authenticate(self.db, here, now=T0)
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, there, now=T0)
        with self.assertRaises(A.LoginFailed):
            self.login_ali()
        self.login_ali("next-pass-22")

    # --- lockout ---
    def test_five_wrong_tries_lock_for_fifteen_minutes(self):
        for i in range(A.MAX_FAILED_LOGINS):
            with self.assertRaises(A.LoginFailed):
                self.login_ali("wrong-password", at=T0 + timedelta(seconds=i))
        last_try = T0 + timedelta(seconds=A.MAX_FAILED_LOGINS - 1)
        with self.assertRaises(A.AccountLocked):
            self.login_ali(at=last_try + timedelta(minutes=14))
        self.login_ali(at=last_try + timedelta(minutes=A.LOCK_MINUTES, seconds=1))

    def test_a_good_login_resets_the_count(self):
        for _ in range(A.MAX_FAILED_LOGINS - 1):
            with self.assertRaises(A.LoginFailed):
                self.login_ali("wrong-password")
        self.login_ali()
        for _ in range(A.MAX_FAILED_LOGINS - 1):
            with self.assertRaises(A.LoginFailed):
                self.login_ali("wrong-password")
        self.login_ali()

    def test_wrong_setup_codes_count_towards_the_lock(self):
        A.issue_setup_code(self.db, self.sam, admin_id=self.admin, now=T0)
        for _ in range(A.MAX_FAILED_LOGINS):
            with self.assertRaises(A.LoginFailed):
                A.set_password_with_code(self.db, "0922222222", "GUESS234", "pass-word-1",
                                         now=T0)
        with self.assertRaises(A.AccountLocked):
            A.set_password_with_code(self.db, "0922222222", "GUESS235", "pass-word-1",
                                     now=T0)

    # --- sessions ---
    def test_worker_session_lasts_a_week(self):
        token = self.login_ali()
        A.authenticate(self.db, token, now=T0 + timedelta(days=6))
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0 + timedelta(days=7))

    def test_admin_session_ends_after_idle_or_eight_hours(self):
        token = A.login(self.db, "0910000000", ADMIN_PW, now=T0)
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0 + timedelta(minutes=31))
        token = A.login(self.db, "0910000000", ADMIN_PW, now=T0)
        at = T0
        while at + timedelta(minutes=20) < T0 + timedelta(hours=8):
            at += timedelta(minutes=20)
            A.authenticate(self.db, token, now=at)
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0 + timedelta(hours=8))

    def test_logout_and_logout_everywhere(self):
        a, b, c = self.login_ali(), self.login_ali(), self.login_ali()
        A.logout(self.db, a, now=T0)
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, a, now=T0)
        A.logout_everywhere(self.db, b, now=T0)
        for token in (b, c):
            with self.assertRaises(A.NotLoggedIn):
                A.authenticate(self.db, token, now=T0)
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, "made-up-token", now=T0)

    # --- blocked and closed accounts ---
    def test_blocking_logs_out_and_stays_out_after_unblock(self):
        token = self.login_ali()
        L.block_user(self.db, self.ali, admin_id=self.admin, reason="under review")
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0)
        with self.assertRaises(L.AccountBlocked):
            self.login_ali()
        L.unblock_user(self.db, self.ali, admin_id=self.admin, reason="cleared")
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0)
        self.login_ali()

    def test_deactivated_user_cannot_log_in(self):
        token = self.login_ali()
        L.deactivate_user(self.db, self.ali, admin_id=self.admin, reason="left")
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0)
        with self.assertRaises(A.LoginFailed):
            self.login_ali()

    def test_blocked_admin_is_logged_out(self):
        boss = L.create_user(self.db, "admin", "Boss", "0930000000", admin_id=self.admin)
        code = A.issue_setup_code(self.db, boss, admin_id=self.admin, now=T0)
        token = A.set_password_with_code(self.db, "0930000000", code, "boss-pass-1", now=T0)
        L.block_user(self.db, boss, admin_id=self.admin, reason="suspicious payouts")
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0)


class ApiTests(unittest.TestCase):
    """The api layer acts only as the logged-in user (real clock)."""

    def setUp(self):
        self.db = A.connect(":memory:")
        self.addCleanup(self.db.close)
        self.admin = A.create_first_admin(self.db, "Admin", "0910000000", ADMIN_PW)
        self.ali = L.create_user(self.db, "customer", "Ali", "0911111111",
                                 admin_id=self.admin)
        self.sam = L.create_user(self.db, "customer", "Sam", "0922222222",
                                 admin_id=self.admin)
        for user, phone, pw in ((self.ali, "0911111111", ALI_PW),
                                (self.sam, "0922222222", "sam-wallet-88")):
            code = A.issue_setup_code(self.db, user, admin_id=self.admin)
            A.set_password_with_code(self.db, phone, code, pw)
        self.ali_t = A.login(self.db, "0911111111", ALI_PW)
        self.admin_t = A.login(self.db, "0910000000", ADMIN_PW)
        L.deposit(self.db, self.ali, lyd("100"), admin_id=api.admin_id(self.db, self.admin_t),
                  into="BANK")

    def test_send_money_by_phone_as_the_logged_in_worker(self):
        self.assertEqual(api.lookup_recipient(self.db, self.ali_t, "092 222 2222")["name"],
                         "Sam")
        result = api.send_money(self.db, self.ali_t, "0922222222", "12.5",
                                idempotency_key="confirm-1")
        again = api.send_money(self.db, self.ali_t, "0922222222", "12.5",
                               idempotency_key="confirm-1")
        self.assertEqual(result["txn_id"], again["txn_id"])
        self.assertEqual(result["balance"], {"dirhams": lyd("87.5"), "lyd": "87.500"})
        self.assertEqual(L.balance(self.db, self.sam), lyd("12.5"))

    def test_send_money_checks(self):
        with self.assertRaises(L.LedgerError):
            api.send_money(self.db, self.ali_t, "0922222222", "1", idempotency_key="")
        with self.assertRaises(L.UnknownAccount):
            api.send_money(self.db, self.ali_t, "0910000000", "1", idempotency_key="k1")
        with self.assertRaises(L.LedgerError):
            api.send_money(self.db, self.ali_t, "0911111111", "1", idempotency_key="k2")
        with self.assertRaises(L.InsufficientFunds):
            api.send_money(self.db, self.ali_t, "0922222222", "100.001",
                           idempotency_key="k3")
        with self.assertRaises(A.NotLoggedIn):
            api.send_money(self.db, "stolen?", "0922222222", "1", idempotency_key="k4")

    def test_roles_are_kept_apart(self):
        with self.assertRaises(L.PermissionDenied):
            api.admin_id(self.db, self.ali_t)
        with self.assertRaises(L.PermissionDenied):
            api.me(self.db, self.admin_t)

    def test_orders_show_the_worker_only_their_own_view(self):
        aid = api.admin_id(self.db, self.admin_t)
        O.add_currency(self.db, "USD", admin_id=aid)
        O.set_price(self.db, "USD", "7.85", admin_id=aid)
        supplier = O.add_supplier(self.db, "Cairo agent", admin_id=aid)
        O.set_supplier_price(self.db, supplier, "USD", "7.80", admin_id=aid)
        self.assertEqual([p["currency"] for p in api.prices(self.db, self.ali_t)], ["USD"])
        quote = api.get_quote(self.db, self.ali_t, "usd", "10")
        self.assertEqual(quote["cost_lyd"], "78.500")
        order = api.place_order(self.db, self.ali_t, quote["quote_id"], "Ahmed", "A1")
        O.assign_supplier(self.db, order["id"], supplier, admin_id=aid)
        mine = api.my_orders(self.db, self.ali_t)
        self.assertEqual([o["receipt_code"] for o in mine], [order["receipt_code"]])
        self.assertNotIn("supplier_cost", mine[0])
        self.assertNotIn("supplier_id", mine[0])
        sam_t = A.login(self.db, "0922222222", "sam-wallet-88")
        self.assertEqual(api.my_orders(self.db, sam_t), [])
        with self.assertRaises(L.LedgerError):     # Ali's quote, Sam's session
            api.place_order(self.db, sam_t, quote["quote_id"], "X", "1")

    def test_me_and_statement(self):
        self.assertEqual(api.me(self.db, self.ali_t)["balance"]["lyd"], "100.000")
        rows = api.my_statement(self.db, self.ali_t)
        self.assertEqual([(r["kind"], r["amount_lyd"]) for r in rows],
                         [("deposit", "100.000")])


if __name__ == "__main__":
    unittest.main()
