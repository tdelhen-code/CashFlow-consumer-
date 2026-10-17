import sqlite3
import unittest

from app import ledger as L


def lyd(x):
    return L.parse_lyd(x)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = L.connect(":memory:")
        self.addCleanup(self.db.close)
        self.admin = L.create_user(self.db, "admin", "Admin", "0910000000")
        self.ali = L.create_user(self.db, "customer", "Ali", "0911111111",
                                 admin_id=self.admin)
        self.sam = L.create_user(self.db, "customer", "Sam", "0922222222", "en",
                                 admin_id=self.admin)
        self.till = L.add_till(self.db, "main", admin_id=self.admin)

    def fund(self, user, amount, into="BANK"):
        return L.deposit(self.db, user, lyd(amount), admin_id=self.admin, into=into)

    def assertHealthy(self):
        self.assertEqual(L.verify_ledger(self.db), [])

    # --- money helpers ---
    def test_parse_and_format(self):
        self.assertEqual(L.parse_lyd("12.345"), 12345)
        self.assertEqual(L.parse_lyd("500"), 500000)
        self.assertEqual(L.format_lyd(12345), "12.345")
        self.assertEqual(L.format_lyd(1234567000), "1,234,567.000")
        self.assertEqual(L.format_lyd(-500), "-0.500")
        for bad in ("1.2345", "abc", "NaN", "inf", "", "1e30", "1e999999999"):
            with self.assertRaises(L.InvalidAmount):
                L.parse_lyd(bad)
        self.assertEqual(L.parse_lyd("1000000000"), L.MAX_DIRHAMS)

    def test_amounts_above_the_cap_are_refused(self):
        with self.assertRaises(L.InvalidAmount):
            L.deposit(self.db, self.ali, L.MAX_DIRHAMS + 1, admin_id=self.admin,
                      into="BANK")
        with self.assertRaises(L.InvalidAmount):
            L.commission(1000, 1.5)
        with self.assertRaises(L.LedgerError):
            L.create_user(self.db, "customer", "X", "0956666666", "de",
                          admin_id=self.admin)

    def test_commission_rounding_and_minimum(self):
        self.assertEqual(L.commission(lyd("500"), 100), lyd("5"))        # 1%
        self.assertEqual(L.commission(lyd("10"), 100, minimum=lyd("1")), lyd("1"))
        self.assertEqual(L.commission(1, 5000), 1)                       # 0.5 rounds up

    def test_rejects_bad_amounts(self):
        for bad in (0, -5, 1.5, True, "10"):
            with self.assertRaises(L.InvalidAmount):
                L.deposit(self.db, self.ali, bad, admin_id=self.admin, into="BANK")

    # --- who can do what ---
    def test_only_first_admin_can_be_created_without_an_admin(self):
        with self.assertRaises(L.PermissionDenied):
            L.create_user(self.db, "admin", "Second", "0933333333")
        with self.assertRaises(L.PermissionDenied):
            L.create_user(self.db, "customer", "Walk-in", "0944444444")

    def test_a_till_is_added_once(self):
        with self.assertRaises(L.Conflict):
            L.add_till(self.db, " Main", admin_id=self.admin)
        with self.assertRaises(L.LedgerError):
            L.add_till(self.db, 7, admin_id=self.admin)
        self.assertEqual(L.add_till(self.db, "airport", admin_id=self.admin), "TILL_AIRPORT")

    def test_workers_cannot_do_admin_actions(self):
        self.fund(self.ali, "100")
        with self.assertRaises(L.PermissionDenied):
            L.deposit(self.db, self.ali, lyd("1"), admin_id=self.ali, into="BANK")
        with self.assertRaises(L.PermissionDenied):
            L.withdraw_cash(self.db, self.ali, lyd("1"), 0, admin_id=self.ali,
                            from_till=self.till)
        with self.assertRaises(L.PermissionDenied):
            L.add_till(self.db, "second", admin_id=self.ali)

    # --- money in ---
    def test_bank_deposit_credits_worker_and_bank(self):
        self.fund(self.ali, "1000")
        self.assertEqual(L.balance(self.db, self.ali), lyd("1000"))
        self.assertEqual(L.holding_amount(self.db, "BANK"), lyd("1000"))
        self.assertHealthy()

    def test_cash_deposit_goes_to_the_till(self):
        self.fund(self.ali, "300", into=self.till)
        self.assertEqual(L.holding_amount(self.db, self.till), lyd("300"))
        self.assertEqual(L.holding_amount(self.db, "BANK"), 0)

    def test_unknown_till_is_rejected(self):
        with self.assertRaises(L.UnknownAccount):
            self.fund(self.ali, "10", into="TILL_NOPE")

    def test_payroll_batch_credits_everyone_in_one_transaction(self):
        L.payroll_credit(self.db, [(self.ali, lyd("800")), (self.sam, lyd("950.5"))],
                         admin_id=self.admin, batch_ref="OCT-2026")
        self.assertEqual(L.balance(self.db, self.ali), lyd("800"))
        self.assertEqual(L.balance(self.db, self.sam), lyd("950.5"))
        self.assertEqual(L.holding_amount(self.db, "BANK"), lyd("1750.5"))
        self.assertHealthy()

    def test_a_payroll_batch_reference_is_posted_once(self):
        boss = L.create_user(self.db, "admin", "Boss", "0930000000", admin_id=self.admin)
        first = L.payroll_credit(self.db, [(self.ali, lyd("800"))], admin_id=self.admin,
                                 batch_ref="OCT-2026", idempotency_key="p1")
        self.assertEqual(L.payroll_credit(self.db, [(self.ali, lyd("800"))],
                                          admin_id=self.admin, batch_ref="OCT-2026",
                                          idempotency_key="p1"), first)   # a retry
        for admin, ref, key in ((self.admin, "OCT-2026", None), (boss, " oct-2026 ", "p1")):
            with self.assertRaises(L.Conflict):
                L.payroll_credit(self.db, [(self.ali, lyd("800"))], admin_id=admin,
                                 batch_ref=ref, idempotency_key=key)
        self.assertEqual(L.balance(self.db, self.ali), lyd("800"))
        L.reverse(self.db, first, admin_id=self.admin, reason="wrong amount")
        L.payroll_credit(self.db, [(self.ali, lyd("850"))], admin_id=self.admin,
                         batch_ref="OCT-2026")
        self.assertEqual(L.balance(self.db, self.ali), lyd("850"))
        for ref in (" ", None):
            with self.assertRaises(L.LedgerError):
                L.payroll_credit(self.db, [(self.ali, lyd("1"))], admin_id=self.admin,
                                 batch_ref=ref)
        self.assertHealthy()

    def test_payroll_with_unknown_worker_credits_nobody(self):
        with self.assertRaises(L.UnknownAccount):
            L.payroll_credit(self.db, [(self.ali, lyd("800")), (9999, lyd("1"))],
                             admin_id=self.admin, batch_ref="X")
        self.assertEqual(L.balance(self.db, self.ali), 0)
        self.assertEqual(L.holding_amount(self.db, "BANK"), 0)

    def test_owner_capital_into_a_till(self):
        L.capital(self.db, self.till, lyd("2000"), admin_id=self.admin, direction="in")
        self.assertEqual(L.holding_amount(self.db, self.till), lyd("2000"))
        self.assertEqual(L.account_balance(self.db, "SYS_CAPITAL"), lyd("2000"))
        self.assertHealthy()

    # --- transfers between workers ---
    def test_transfer_moves_money_and_fee(self):
        self.fund(self.ali, "100")
        L.transfer(self.db, self.ali, self.sam, lyd("40"), fee=lyd("1"))
        self.assertEqual(L.balance(self.db, self.ali), lyd("59"))
        self.assertEqual(L.balance(self.db, self.sam), lyd("40"))
        self.assertEqual(L.account_balance(self.db, "SYS_FEES"), lyd("1"))
        self.assertHealthy()

    def test_transfer_without_enough_money_changes_nothing(self):
        self.fund(self.ali, "10")
        before = self.db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        with self.assertRaises(L.InsufficientFunds):
            L.transfer(self.db, self.ali, self.sam, lyd("10"), fee=lyd("1"))
        self.assertEqual(L.balance(self.db, self.ali), lyd("10"))
        self.assertEqual(L.balance(self.db, self.sam), 0)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], before)

    def test_cannot_transfer_to_self_or_to_admin(self):
        self.fund(self.ali, "10")
        with self.assertRaises(L.LedgerError):
            L.transfer(self.db, self.ali, self.ali, lyd("1"))
        with self.assertRaises(L.UnknownAccount):
            L.transfer(self.db, self.ali, self.admin, lyd("1"))

    def test_inactive_worker_cannot_receive(self):
        self.fund(self.ali, "10")
        self.db.execute("UPDATE users SET active = 0 WHERE id = ?", (self.sam,))
        with self.assertRaises(L.UnknownAccount):
            L.transfer(self.db, self.ali, self.sam, lyd("1"))

    def test_same_key_posts_once(self):
        a = L.deposit(self.db, self.ali, lyd("50"), admin_id=self.admin,
                      into="BANK", idempotency_key="dep-1")
        b = L.deposit(self.db, self.ali, lyd("50"), admin_id=self.admin,
                      into="BANK", idempotency_key="dep-1")
        self.assertEqual(a, b)
        self.assertEqual(L.balance(self.db, self.ali), lyd("50"))

    def test_same_key_from_two_workers_moves_both(self):
        self.fund(self.ali, "100")
        self.fund(self.sam, "100")
        bob = L.create_user(self.db, "customer", "Bob", "0955555555", admin_id=self.admin)
        a = L.transfer(self.db, self.ali, bob, lyd("10"), idempotency_key="tx-1")
        b = L.transfer(self.db, self.sam, bob, lyd("20"), idempotency_key="tx-1")
        self.assertNotEqual(a, b)
        self.assertEqual(L.balance(self.db, bob), lyd("30"))
        self.assertEqual(L.balance(self.db, self.sam), lyd("80"))

    def test_same_key_with_different_details_is_refused(self):
        L.deposit(self.db, self.ali, lyd("50"), admin_id=self.admin,
                  into="BANK", idempotency_key="dep-1")
        with self.assertRaises(L.LedgerError):
            L.deposit(self.db, self.ali, lyd("60"), admin_id=self.admin,
                      into="BANK", idempotency_key="dep-1")
        self.assertEqual(L.balance(self.db, self.ali), lyd("50"))

    def test_blank_or_odd_idempotency_keys_are_refused(self):
        for key in ("", "   ", False, 0, 5, ["k"], "k" * 201):
            with self.assertRaises(L.LedgerError, msg=repr(key)):
                L.deposit(self.db, self.ali, lyd("50"), admin_id=self.admin,
                          into="BANK", idempotency_key=key)
        self.assertEqual(L.balance(self.db, self.ali), 0)
        a = L.deposit(self.db, self.ali, lyd("50"), admin_id=self.admin,
                      into="BANK", idempotency_key=" dep-1 ")
        b = L.deposit(self.db, self.ali, lyd("50"), admin_id=self.admin,
                      into="BANK", idempotency_key="dep-1")
        self.assertEqual(a, b)
        L.deposit(self.db, self.ali, lyd("50"), admin_id=self.admin,
                  into="BANK", idempotency_key="k" * 200)
        self.assertEqual(L.balance(self.db, self.ali), lyd("100"))

    # --- cash out ---
    def test_withdraw_cash_with_commission(self):
        self.fund(self.ali, "1000", into=self.till)
        fee = L.commission(lyd("500"), 100)   # 1%
        L.withdraw_cash(self.db, self.ali, lyd("500"), fee, admin_id=self.admin,
                        from_till=self.till)
        self.assertEqual(L.balance(self.db, self.ali), lyd("500"))
        self.assertEqual(L.holding_amount(self.db, self.till), lyd("505"))  # paid out 495
        self.assertEqual(L.account_balance(self.db, "SYS_FEES"), lyd("5"))
        self.assertHealthy()

    def test_till_cannot_pay_out_cash_it_does_not_have(self):
        L.payroll_credit(self.db, [(self.ali, lyd("1000"))], admin_id=self.admin,
                         batch_ref="OCT")
        with self.assertRaises(L.NotEnoughCash):
            L.withdraw_cash(self.db, self.ali, lyd("500"), 0, admin_id=self.admin,
                            from_till=self.till)
        self.assertEqual(L.balance(self.db, self.ali), lyd("1000"))
        L.move_money(self.db, "BANK", self.till, lyd("600"), admin_id=self.admin)
        L.withdraw_cash(self.db, self.ali, lyd("500"), 0, admin_id=self.admin,
                        from_till=self.till)
        self.assertEqual(L.holding_amount(self.db, self.till), lyd("100"))
        self.assertEqual(L.holding_amount(self.db, "BANK"), lyd("400"))
        self.assertHealthy()

    def test_withdraw_more_than_balance_fails(self):
        self.fund(self.ali, "100", into=self.till)
        with self.assertRaises(L.InsufficientFunds):
            L.withdraw_cash(self.db, self.ali, lyd("101"), 0, admin_id=self.admin,
                            from_till=self.till)

    def test_fee_must_be_less_than_amount(self):
        self.fund(self.ali, "100", into=self.till)
        with self.assertRaises(L.InvalidAmount):
            L.withdraw_cash(self.db, self.ali, lyd("10"), lyd("10"),
                            admin_id=self.admin, from_till=self.till)

    # --- reversals ---
    def test_reversal_restores_and_works_once(self):
        dep = self.fund(self.ali, "100")
        rev = L.reverse(self.db, dep, admin_id=self.admin, reason="typed wrong amount")
        self.assertEqual(L.balance(self.db, self.ali), 0)
        self.assertEqual(L.holding_amount(self.db, "BANK"), 0)
        with self.assertRaises(L.LedgerError):
            L.reverse(self.db, dep, admin_id=self.admin, reason="again")
        with self.assertRaises(L.LedgerError):
            L.reverse(self.db, rev, admin_id=self.admin, reason="undo the undo")
        with self.assertRaises(L.NotFound):
            L.reverse(self.db, 999, admin_id=self.admin, reason="no such transaction")
        self.assertHealthy()

    def test_reversal_needs_a_reason(self):
        dep = self.fund(self.ali, "100")
        with self.assertRaises(L.LedgerError):
            L.reverse(self.db, dep, admin_id=self.admin, reason="  ")

    def test_cannot_reverse_a_deposit_that_was_already_spent(self):
        dep = self.fund(self.ali, "100")
        L.transfer(self.db, self.ali, self.sam, lyd("100"))
        with self.assertRaises(L.InsufficientFunds):
            L.reverse(self.db, dep, admin_id=self.admin, reason="mistake")
        self.assertEqual(L.balance(self.db, self.ali), 0)
        self.assertHealthy()

    # --- statement ---
    def test_statement_shows_running_balance_newest_first(self):
        self.fund(self.ali, "100")
        L.transfer(self.db, self.ali, self.sam, lyd("30"))
        rows = L.statement(self.db, self.ali)
        self.assertEqual([r["kind"] for r in rows], ["transfer", "deposit"])
        self.assertEqual([r["balance_after"] for r in rows], [lyd("70"), lyd("100")])

    # --- integrity ---
    def test_ledger_and_admin_log_cannot_be_edited_or_deleted(self):
        self.fund(self.ali, "100")
        for sql in ("UPDATE entries SET amount = 999999",
                    "DELETE FROM entries",
                    "UPDATE transactions SET note = 'x'",
                    "DELETE FROM transactions",
                    "UPDATE admin_log SET action = 'x'",
                    "DELETE FROM admin_log"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
        self.assertEqual(L.balance(self.db, self.ali), lyd("100"))

    def test_database_itself_refuses_overdraft(self):
        acc = self.db.execute("SELECT id FROM accounts WHERE user_id = ?",
                              (self.ali,)).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO transactions(kind) VALUES ('transfer')")
            self.db.execute("INSERT INTO entries(txn_id, account_id, amount) "
                            "VALUES (last_insert_rowid(), ?, -1)", (acc,))

    def test_verify_ledger_catches_tampering(self):
        self.fund(self.ali, "100")
        self.db.execute("DROP TRIGGER entries_no_update")
        self.db.execute("UPDATE entries SET amount = amount + 1 WHERE id = 1")
        self.assertTrue(L.verify_ledger(self.db))

    def test_verify_ledger_reads_one_snapshot(self):
        self.fund(self.ali, "100")
        statements = []
        self.db.set_trace_callback(statements.append)
        self.assertHealthy()
        self.db.set_trace_callback(None)
        self.assertEqual((statements[0], statements[-1]), ("BEGIN DEFERRED", "COMMIT"))
        self.assertFalse(self.db.in_transaction)
        with L._write(self.db):         # inside a write it reads that transaction
            self.assertHealthy()

    def test_admin_actions_are_logged(self):
        self.fund(self.ali, "100")
        row = self.db.execute(
            "SELECT admin_id, details FROM admin_log WHERE action = 'deposit'").fetchone()
        self.assertEqual(row["admin_id"], self.admin)
        self.assertIn('"amount": 100000', row["details"])

    # --- reconciliation ---
    def test_exact_reconciliation_per_bank_and_till(self):
        self.fund(self.ali, "600")
        self.fund(self.sam, "400", into=self.till)
        rows = L.reconcile(self.db, {"BANK": lyd("600"), self.till: lyd("400")})
        self.assertTrue(all(r["ok"] for r in rows))
        rows = L.reconcile(self.db, {"BANK": lyd("590"), self.till: lyd("400.5")})
        self.assertEqual([r["difference"] for r in rows], [-lyd("10"), lyd("0.5")])
        self.assertFalse(any(r["ok"] for r in rows))


if __name__ == "__main__":
    unittest.main()
