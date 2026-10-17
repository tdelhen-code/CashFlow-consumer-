import hashlib
import json
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from app import api
from app import auth as A
from app import ledger as L
from app import orders as O
from tests import STRONG_SCRYPT

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
        with self.assertRaises(A.LoginFailed) as caught:    # even the right password
            self.login_ali(at=last_try + timedelta(minutes=14))
        self.assertEqual(str(caught.exception), A.LOGIN_FAILED)
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
        for _ in range(A.MAX_FAILED_LOGINS):
            with self.assertRaises(A.LoginFailed):
                A.set_password_with_code(self.db, "0911111111", "GUESS-2345-6789",
                                         "pass-word-1", now=T0)
        with self.assertRaises(A.LoginFailed):
            self.login_ali()
        self.login_ali(at=T0 + timedelta(minutes=A.LOCK_MINUTES, seconds=1))

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

    # --- races: something changes while a password is being hashed ---
    def meanwhile(self, action):
        """Run `action` while the next password is being hashed, which happens
        outside the write lock."""
        real, done = A.hash_password, []

        def hash_password(secret):
            if not done:
                done.append(True)
                action()
            return real(secret)
        return unittest.mock.patch.object(A, "hash_password", hash_password)

    def ali_hash(self):
        return self.db.execute("SELECT password_hash FROM users WHERE id = ?",
                               (self.ali,)).fetchone()[0]

    def test_change_password_is_refused_if_reset_meanwhile(self):
        here = self.login_ali()
        reset = lambda: A.issue_setup_code(self.db, self.ali, admin_id=self.admin, now=T0)
        with self.meanwhile(reset), self.assertRaises(A.NotLoggedIn):
            A.change_password(self.db, here, ALI_PW, "thief-pass-1", now=T0)
        self.assertIsNone(self.ali_hash())
        with self.assertRaises(A.LoginFailed):
            self.login_ali("thief-pass-1")

    def test_change_password_is_refused_if_blocked_meanwhile(self):
        here = self.login_ali()
        block = lambda: L.block_user(self.db, self.ali, admin_id=self.admin, reason="doubt")
        with self.meanwhile(block), self.assertRaises(A.NotLoggedIn):
            A.change_password(self.db, here, ALI_PW, "thief-pass-1", now=T0)
        L.unblock_user(self.db, self.ali, admin_id=self.admin, reason="cleared")
        self.login_ali()

    def test_change_password_is_refused_if_changed_meanwhile(self):
        here = self.login_ali()
        other = lambda: A.change_password(self.db, here, ALI_PW, "owner-pass-1", now=T0)
        with self.meanwhile(other), self.assertRaises(L.Conflict):
            A.change_password(self.db, here, ALI_PW, "thief-pass-1", now=T0)
        self.login_ali("owner-pass-1")

    def test_setup_code_is_refused_if_blocked_meanwhile(self):
        code = A.issue_setup_code(self.db, self.ali, admin_id=self.admin, now=T0)
        block = lambda: L.block_user(self.db, self.ali, admin_id=self.admin, reason="doubt")
        with self.meanwhile(block), self.assertRaises(L.AccountBlocked):
            A.set_password_with_code(self.db, "0911111111", code, "brand-new-1", now=T0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND ended_at IS NULL",
            (self.ali,)).fetchone()[0], 0)

    def test_using_a_setup_code_ends_every_other_session(self):
        code = A.issue_setup_code(self.db, self.ali, admin_id=self.admin, now=T0)
        ali = self.db.execute("SELECT * FROM users WHERE id = ?", (self.ali,)).fetchone()
        with L._write(self.db):         # one opened after the code was issued
            stray = A._new_session(self.db, ali, T0)
        token = A.set_password_with_code(self.db, "0911111111", code, "brand-new-1", now=T0)
        A.authenticate(self.db, token, now=T0)
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, stray, now=T0)

    # --- guessing the current password ---
    def test_wrong_current_passwords_count_towards_the_lock(self):
        here = self.login_ali()
        for i in range(A.MAX_FAILED_LOGINS):
            with self.assertRaises(A.WrongPassword):
                A.change_password(self.db, here, f"guess-{i}-guess", "thief-pass-1", now=T0)
        with self.assertRaises(A.AccountLocked):
            A.change_password(self.db, here, ALI_PW, "thief-pass-1", now=T0)
        with self.assertRaises(A.LoginFailed):
            self.login_ali()
        A.authenticate(self.db, here, now=T0)       # still logged in
        self.login_ali(at=T0 + timedelta(minutes=A.LOCK_MINUTES, seconds=1))

    def test_change_password_checks_the_new_one_first(self):
        here = self.login_ali()
        answers = []
        for old in (ALI_PW, "wrong-guess-1"):
            with self.assertRaises(L.LedgerError) as caught:
                A.change_password(self.db, here, old, "x", now=T0)
            answers.append((type(caught.exception), str(caught.exception)))
        self.assertEqual(len(set(answers)), 1, answers)     # no hint the first was right

    # --- setup codes ---
    def test_setup_codes_are_long_and_stored_with_salted_scrypt(self):
        code = A.issue_setup_code(self.db, self.sam, admin_id=self.admin, now=T0)
        plain = code.replace("-", "")
        self.assertGreaterEqual(len(plain), 10)
        self.assertLessEqual(set(plain), set(A.SETUP_CODE_ALPHABET))
        stored = self.db.execute("SELECT code_hash FROM setup_codes WHERE user_id = ?",
                                 (self.sam,)).fetchone()[0]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertEqual(len(stored.split("$")), 6)         # with its own salt
        typed = " ".join(plain[i:i + 4] for i in range(0, len(plain), 4)).lower()
        A.set_password_with_code(self.db, "0922222222", typed, "sam-pass-99", now=T0)

    def test_a_right_setup_code_works_while_locked_and_lifts_the_lock(self):
        for _ in range(A.MAX_FAILED_LOGINS):
            with self.assertRaises(A.LoginFailed):
                self.login_ali("wrong-password")
        code = A.issue_setup_code(self.db, self.ali, admin_id=self.admin, now=T0)
        token = A.set_password_with_code(self.db, "0911111111", code, "brand-new-1", now=T0)
        A.authenticate(self.db, token, now=T0)
        self.login_ali("brand-new-1")

    # --- unknown phones look like registered ones ---
    def test_locked_unknown_and_wrong_get_the_same_answer(self):
        for _ in range(A.MAX_FAILED_LOGINS):
            for phone in ("0911111111", "0933333333"):
                with self.assertRaises(A.LoginFailed):
                    A.login(self.db, phone, "wrong-password", now=T0)
        answers = []
        for phone, password in (("0911111111", ALI_PW), ("0911111111", "wrong-password"),
                                ("0933333333", ALI_PW), ("0944444444", ALI_PW)):
            with self.assertRaises(A.LoginFailed) as caught:
                A.login(self.db, phone, password, now=T0)
            answers.append((type(caught.exception), str(caught.exception)))
        self.assertEqual(len(set(answers)), 1, answers)

    def test_unknown_phones_take_the_same_slow_check(self):
        A.issue_setup_code(self.db, self.sam, admin_id=self.admin, now=T0)
        tries = [lambda phone=phone: A.set_password_with_code(
                     self.db, phone, "WRONG-CODE-2345", "pass-word-1", now=T0)
                 for phone in ("0922222222", "0911111111", "0933333333")]
        tries += [lambda phone=phone: A.login(self.db, phone, "wrong-password", now=T0)
                  for phone in ("0911111111", "0922222222", "0933333333")]
        for attempt in tries:
            with unittest.mock.patch("hashlib.scrypt", wraps=hashlib.scrypt) as spy:
                with self.assertRaises(A.LoginFailed):
                    attempt()
            self.assertEqual(spy.call_count, 1)

    # --- admin unlock ---
    def test_admin_can_unlock_a_locked_account(self):
        for _ in range(A.MAX_FAILED_LOGINS):
            with self.assertRaises(A.LoginFailed):
                self.login_ali("wrong-password")
        with self.assertRaises(A.LoginFailed):
            self.login_ali()
        with self.assertRaises(L.PermissionDenied):
            A.unlock_login(self.db, self.ali, admin_id=self.sam, reason="please")
        with self.assertRaises(L.LedgerError):
            A.unlock_login(self.db, self.ali, admin_id=self.admin, reason=" ")
        A.unlock_login(self.db, self.ali, admin_id=self.admin,
                       reason="Ali called: someone keeps guessing", now=T0)
        self.login_ali()
        row = self.db.execute("SELECT admin_id, target, details FROM admin_log "
                              "WHERE action = 'unlock_login'").fetchone()
        self.assertEqual((row["admin_id"], row["target"], json.loads(row["details"])),
                         (self.admin, str(self.ali),
                          {"reason": "Ali called: someone keeps guessing"}))

    # --- admins and setup codes ---
    def test_setup_codes_are_logged_and_need_a_reason_for_admins(self):
        boss = L.create_user(self.db, "admin", "Boss", "0930000000", admin_id=self.admin)
        with self.assertRaises(L.LedgerError):
            A.issue_setup_code(self.db, boss, admin_id=self.admin, now=T0)
        code = A.issue_setup_code(self.db, boss, admin_id=self.admin,
                                  reason="new office admin", now=T0)
        A.set_password_with_code(self.db, "0930000000", code, "boss-pass-1", now=T0)
        rows = self.db.execute(
            "SELECT admin_id, action, details, at FROM admin_log WHERE target = ? "
            "AND action LIKE '%setup_code%' ORDER BY id", (str(boss),)).fetchall()
        self.assertEqual([(r["admin_id"], r["action"], json.loads(r["details"]).get("reason"))
                          for r in rows],
                         [(self.admin, "issue_setup_code", "new office admin"),
                          (self.admin, "setup_code_used", None)])
        self.assertTrue(all(r["at"] for r in rows))

    # --- password strength ---
    def test_common_passwords_and_phone_digits_are_refused(self):
        code = A.issue_setup_code(self.db, self.sam, admin_id=self.admin, now=T0)
        for bad in ("123456789", "1234567890", "9876543210", "qwertyuiop", "QWERTYUIOP",
                    "azertyuiop", "Password123", "iloveyou123", "sam+218922222222",
                    "my 092 222 2222"):
            with self.assertRaises(L.LedgerError, msg=bad):
                A.set_password_with_code(self.db, "0922222222", code, bad, now=T0)
        A.set_password_with_code(self.db, "0922222222", code, "fine-pass-1", now=T0)

    def test_scrypt_cost_is_strong_by_default(self):
        self.assertGreaterEqual(STRONG_SCRYPT["n"], 2**15)
        self.assertGreaterEqual(STRONG_SCRYPT["r"], 8)
        with unittest.mock.patch.object(A, "SCRYPT", STRONG_SCRYPT):
            stored = A.hash_password("strong-pass-1")
        self.assertEqual(stored.split("$")[1:4],
                         [str(STRONG_SCRYPT[k]) for k in ("n", "r", "p")])
        self.assertTrue(A.verify_password("strong-pass-1", stored))

    def test_an_older_hash_is_redone_at_login(self):
        before = self.ali_hash()
        stronger = dict(A.SCRYPT, n=A.SCRYPT["n"] * 2)
        with unittest.mock.patch.object(A, "SCRYPT", stronger):
            self.login_ali()
            after = self.ali_hash()
            self.assertEqual(after.split("$")[1], str(stronger["n"]))
            self.assertNotEqual(before, after)
            self.login_ali(at=T0 + timedelta(seconds=1))
            self.assertEqual(self.ali_hash(), after)       # done once

    def test_only_a_few_passwords_are_hashed_at_once(self):
        lock, running, most = threading.Lock(), [0], [0]
        real = hashlib.scrypt

        def scrypt(*args, **kwargs):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.02)
            with lock:
                running[0] -= 1
            return real(*args, **kwargs)

        with unittest.mock.patch("hashlib.scrypt", scrypt):
            threads = [threading.Thread(target=A.hash_password, args=(f"pass-word-{i}",))
                       for i in range(3 * A.MAX_PARALLEL_HASHES)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertLessEqual(most[0], A.MAX_PARALLEL_HASHES)

    # --- reading pages doesn't write ---
    def test_last_seen_is_written_at_most_once_a_minute(self):
        token = self.login_ali()
        statements = []
        self.db.set_trace_callback(statements.append)
        self.addCleanup(self.db.set_trace_callback, None)
        A.authenticate(self.db, token, now=T0 + timedelta(seconds=30))
        self.assertEqual([s for s in statements if not s.lstrip().startswith("SELECT")], [])
        A.authenticate(self.db, token, now=T0 + timedelta(seconds=61))
        self.assertTrue([s for s in statements if s.startswith("UPDATE sessions")])

    def test_admin_idle_limit_is_never_late(self):
        token = A.login(self.db, "0910000000", ADMIN_PW, now=T0)
        A.authenticate(self.db, token, now=T0 + timedelta(seconds=50))
        with self.assertRaises(A.NotLoggedIn):
            A.authenticate(self.db, token, now=T0 + timedelta(minutes=31))
        token = A.login(self.db, "0910000000", ADMIN_PW, now=T0)
        A.authenticate(self.db, token, now=T0 + timedelta(minutes=29))
        A.authenticate(self.db, token, now=T0 + timedelta(minutes=58))

    def test_blocked_admin_is_logged_out(self):
        boss = L.create_user(self.db, "admin", "Boss", "0930000000", admin_id=self.admin)
        code = A.issue_setup_code(self.db, boss, admin_id=self.admin, reason="new admin",
                                  now=T0)
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

    def test_statement_shows_no_admin_reasons(self):
        aid = api.admin_id(self.db, self.admin_t)
        O.add_currency(self.db, "USD", admin_id=aid)
        O.set_price(self.db, "USD", "7.85", admin_id=aid)
        supplier = O.add_supplier(self.db, "Cairo agent", admin_id=aid)
        O.set_supplier_price(self.db, supplier, "USD", "7.80", admin_id=aid)
        quote = api.get_quote(self.db, self.ali_t, "USD", "10")
        order = api.place_order(self.db, self.ali_t, quote["quote_id"], "Ahmed", "A1")
        O.assign_supplier(self.db, order["id"], supplier, admin_id=aid)
        O.unassign_supplier(self.db, order["id"], admin_id=aid,
                            reason="Cairo agent confirmed not paid")
        O.cancel_order(self.db, order["id"], admin_id=aid,
                       reason="Cairo agent could not pay at 7.80")
        sent = api.send_money(self.db, self.ali_t, "0922222222", "5", idempotency_key="s1")
        L.reverse(self.db, sent["txn_id"], admin_id=aid, reason="Cairo agent asked for it")
        rows = api.my_statement(self.db, self.ali_t)
        self.assertEqual([r["note"] for r in rows if r["kind"] == "reversal"],
                         ["Correction by admin", "Order cancelled"])
        self.assertFalse([r for r in rows if "Cairo" in repr(r)])
        admin_view = L.account_statement(self.db, f"CUS_{self.ali}")
        self.assertIn("Cairo agent could not pay at 7.80", [r["note"] for r in admin_view])

    def test_me_and_statement(self):
        self.assertEqual(api.me(self.db, self.ali_t)["balance"]["lyd"], "100.000")
        rows = api.my_statement(self.db, self.ali_t)
        self.assertEqual([(r["kind"], r["amount_lyd"]) for r in rows],
                         [("deposit", "100.000")])


if __name__ == "__main__":
    unittest.main()
