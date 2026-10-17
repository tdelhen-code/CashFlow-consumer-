import json
import unittest

from app import ledger as L
from app import orders as O


def lyd(x):
    return L.parse_lyd(x)


class PhoneTests(unittest.TestCase):
    def test_libyan_mobiles_in_any_usual_form(self):
        for typed in ("0912345678", "091-2345678", "091 234 5678", "912345678",
                      "+218912345678", "+218 91 234 5678", "00218912345678",
                      "218912345678", "(091) 234-5678"):
            self.assertEqual(L.normalize_phone(typed), "+218912345678", typed)

    def test_rejects_anything_that_is_not_a_libyan_mobile(self):
        for typed in ("", "abc", "091234567", "09123456789", "0212345678",
                      "+201001234567", "0961234567", "+2180912345678x"):
            with self.assertRaises(L.LedgerError, msg=typed):
                L.normalize_phone(typed)

    def test_whatsapp_from_any_country(self):
        self.assertEqual(L.normalize_whatsapp("+20 100 123 4567"), "+201001234567")
        self.assertEqual(L.normalize_whatsapp("00216 20 123 456"), "+21620123456")
        self.assertEqual(L.normalize_whatsapp("092 123 4567"), "+218921234567")
        self.assertEqual(L.normalize_whatsapp("+218 92 123 4567"), "+218921234567")
        for typed in ("1001234567", "+0123456789", "+12345", "+20abc12345678"):
            with self.assertRaises(L.LedgerError, msg=typed):
                L.normalize_whatsapp(typed)

    def test_arabic_indic_digits_are_read_and_other_scripts_refused(self):
        # As typed on Arabic and Persian keyboards, alone or mixed with 0-9, and
        # as pasted from right-to-left text (direction marks, other dashes).
        for typed in ("٠٩١٢٣٤٥٦٧٨", "۰۹۱۲۳۴۵۶۷۸", "٠٩١ ٢٣٤ ٥٦٧٨", "+٢١٨ ٩١ ٢٣٤ ٥٦٧٨",
                      "091234567٨", "091\u2013234\u20135678", "091\u00a0234\u00a05678",
                      "\u202a+218 91 234 5678\u202c", "\u200e0912345678"):
            self.assertEqual(L.normalize_phone(typed), "+218912345678", ascii(typed))
        # Any other non-ASCII character, digits from other scripts included,
        # is refused rather than stored as it was typed.
        for typed in ("091234567²", "０９１２３４５６７８", "091234567२",
                      "0912345678\u200b", "٠٩١٢٣٤٥٦٧٨x"):
            with self.assertRaises(L.LedgerError, msg=ascii(typed)):
                L.normalize_phone(typed)
        self.assertEqual(L.normalize_whatsapp("+٢٠ ١٠٠ ١٢٣ ٤٥٦٧"), "+201001234567")
        self.assertEqual(L.normalize_whatsapp("+۲۱۸ ۹۲ ۱۲۳ ۴۵۶۷"), "+218921234567")
        self.assertEqual(L.normalize_whatsapp("٠٩٢ ١٢٣ ٤٥٦٧"), "+218921234567")
        for typed in ("+20100123456²", "+２０１００１２３４５６７"):
            with self.assertRaises(L.LedgerError, msg=ascii(typed)):
                L.normalize_whatsapp(typed)


class UserTests(unittest.TestCase):
    def setUp(self):
        self.db = O.connect(":memory:")
        self.addCleanup(self.db.close)
        self.admin = L.create_user(self.db, "admin", "Admin", "0910000000")
        self.ali = L.create_user(self.db, "customer", "Ali", "091 111 1111",
                                 admin_id=self.admin, whatsapp="+20 100 123 4567")
        self.sam = L.create_user(self.db, "customer", "Sam", "0922222222",
                                 admin_id=self.admin)
        self.till = L.add_till(self.db, "main", admin_id=self.admin)
        L.deposit(self.db, self.ali, lyd("100"), admin_id=self.admin, into=self.till)
        O.add_currency(self.db, "USD", admin_id=self.admin)
        O.set_price(self.db, "USD", "7.85", admin_id=self.admin)

    # --- phones ---
    def test_phone_is_stored_in_one_form_and_found_however_typed(self):
        user = L.find_user_by_phone(self.db, "+218 91 111 1111")
        self.assertEqual((user["id"], user["phone"], user["whatsapp"]),
                         (self.ali, "+218911111111", "+201001234567"))
        self.assertIsNone(L.find_user_by_phone(self.db, "0933333333"))

    def test_same_number_written_differently_is_a_duplicate(self):
        with self.assertRaises(L.Conflict):
            L.create_user(self.db, "customer", "Ali again", "00218911111111",
                          admin_id=self.admin)

    def test_same_number_in_arabic_indic_digits_is_the_same_number(self):
        for typed in ("091111111١", "٠٩١١١١١١١١"):
            with self.assertRaises(L.LedgerError, msg=ascii(typed)):
                L.create_user(self.db, "customer", "Ali again", typed, admin_id=self.admin)
            self.assertEqual(L.find_user_by_phone(self.db, typed)["id"], self.ali)
        omar = L.create_user(self.db, "customer", "Omar", "0931234567", admin_id=self.admin,
                             whatsapp="+٢١٨ ٩٣ ١٢٣ ٤٥٦٧")     # his own phone: not kept
        self.assertIsNone(L.find_user_by_phone(self.db, "0931234567")["whatsapp"])
        L.set_whatsapp(self.db, omar, "+٢٠ ١٠٠ ١٢٣ ٤٥٦٧", admin_id=self.admin)
        self.assertEqual(L.find_user_by_phone(self.db, "0931234567")["whatsapp"],
                         "+201001234567")

    def test_whatsapp_can_be_changed_and_is_dropped_when_same_as_phone(self):
        L.set_whatsapp(self.db, self.sam, "+216 20 123 456", admin_id=self.admin)
        self.assertEqual(L.find_user_by_phone(self.db, "0922222222")["whatsapp"],
                         "+21620123456")
        L.set_whatsapp(self.db, self.sam, "092 222 2222", admin_id=self.admin)
        self.assertIsNone(L.find_user_by_phone(self.db, "0922222222")["whatsapp"])
        with self.assertRaises(L.PermissionDenied):
            L.set_whatsapp(self.db, self.sam, None, admin_id=self.sam)

    # --- blocking ---
    def test_blocked_worker_cannot_move_money(self):
        L.block_user(self.db, self.ali, admin_id=self.admin, reason="under review")
        with self.assertRaises(L.AccountBlocked):
            L.transfer(self.db, self.ali, self.sam, lyd("1"))
        with self.assertRaises(L.AccountBlocked):
            L.transfer(self.db, self.sam, self.ali, lyd("1"))
        with self.assertRaises(L.AccountBlocked):
            L.withdraw_cash(self.db, self.ali, lyd("10"), 0, admin_id=self.admin,
                            from_till=self.till)
        with self.assertRaises(L.AccountBlocked):
            O.get_quote(self.db, self.ali, "USD", "1")
        self.assertEqual(L.balance(self.db, self.ali), lyd("100"))

    def test_quote_taken_before_the_block_cannot_be_used(self):
        q = O.get_quote(self.db, self.ali, "USD", "1")
        L.block_user(self.db, self.ali, admin_id=self.admin, reason="doubt")
        with self.assertRaises(L.AccountBlocked):
            O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1")

    def order(self, amount="1"):
        q = O.get_quote(self.db, self.ali, "USD", amount)
        return O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1")

    def test_a_blocked_workers_orders_are_held_but_can_be_cancelled(self):
        supplier = O.add_supplier(self.db, "Cairo agent", admin_id=self.admin)
        O.set_supplier_price(self.db, supplier, "USD", "7.80", admin_id=self.admin)
        placed, assigned = self.order(), self.order()
        O.assign_supplier(self.db, assigned["id"], supplier, admin_id=self.admin)
        L.block_user(self.db, self.ali, admin_id=self.admin, reason="suspected fraud")
        with self.assertRaises(L.AccountBlocked):
            O.assign_supplier(self.db, placed["id"], supplier, admin_id=self.admin)
        O.unassign_supplier(self.db, assigned["id"], admin_id=self.admin,
                            reason="worker blocked, supplier has not paid")
        for o in (placed, assigned):
            O.cancel_order(self.db, o["id"], admin_id=self.admin, reason="worker blocked")
        self.assertEqual(L.balance(self.db, self.ali), lyd("100"))   # refunded, frozen
        with self.assertRaises(L.AccountBlocked):
            L.transfer(self.db, self.ali, self.sam, lyd("1"))
        self.assertEqual(O.verify(self.db), [])

    def test_a_payout_already_made_is_recorded_while_the_worker_is_blocked(self):
        # The supplier paid before the block: refusing to record it would leave
        # the supplier unpaid on the books and push the admin to a cancel that
        # refunds money already paid out abroad.
        supplier = O.add_supplier(self.db, "Cairo agent", admin_id=self.admin)
        O.set_supplier_price(self.db, supplier, "USD", "7.80", admin_id=self.admin)
        o = self.order("10")
        O.assign_supplier(self.db, o["id"], supplier, admin_id=self.admin)
        L.block_user(self.db, self.ali, admin_id=self.admin, reason="doubt")
        before = L.balance(self.db, self.ali)
        O.mark_paid(self.db, o["receipt_code"], "EG-1", admin_id=self.admin)
        self.assertEqual(O.find_by_receipt(self.db, o["receipt_code"])["status"], "paid")
        self.assertEqual(L.balance(self.db, self.ali), before)      # still frozen, untouched
        self.assertEqual(O.supplier_balance(self.db, supplier), lyd("78"))
        logged = self.db.execute(
            "SELECT details FROM admin_log WHERE action = 'mark_paid'").fetchone()[0]
        self.assertIs(json.loads(logged)["customer_blocked"], True)
        with self.assertRaises(L.AccountBlocked):
            L.transfer(self.db, self.ali, self.sam, lyd("1"))
        self.assertEqual(O.verify(self.db), [])

    def test_unblocking_releases_held_orders(self):
        supplier = O.add_supplier(self.db, "Cairo agent", admin_id=self.admin)
        O.set_supplier_price(self.db, supplier, "USD", "7.80", admin_id=self.admin)
        o = self.order()
        L.block_user(self.db, self.ali, admin_id=self.admin, reason="doubt")
        with self.assertRaises(L.AccountBlocked):
            O.assign_supplier(self.db, o["id"], supplier, admin_id=self.admin)
        L.unblock_user(self.db, self.ali, admin_id=self.admin, reason="cleared")
        O.assign_supplier(self.db, o["id"], supplier, admin_id=self.admin)
        O.mark_paid(self.db, o["receipt_code"], "EG-1", admin_id=self.admin)
        self.assertEqual(O.find_by_receipt(self.db, o["receipt_code"])["status"], "paid")
        logged = self.db.execute(
            "SELECT details FROM admin_log WHERE action = 'mark_paid'").fetchone()[0]
        self.assertIs(json.loads(logged)["customer_blocked"], False)
        self.assertEqual(O.verify(self.db), [])

    def test_salary_and_deposits_still_reach_a_blocked_worker(self):
        L.block_user(self.db, self.ali, admin_id=self.admin, reason="under review")
        L.payroll_credit(self.db, [(self.ali, lyd("50"))], admin_id=self.admin,
                         batch_ref="NOV")
        L.deposit(self.db, self.ali, lyd("5"), admin_id=self.admin, into="BANK")
        self.assertEqual(L.balance(self.db, self.ali), lyd("155"))

    def test_unblock_restores_use_and_both_are_logged_with_reasons(self):
        L.block_user(self.db, self.ali, admin_id=self.admin, reason="doubt")
        user = L.find_user_by_phone(self.db, "0911111111")
        self.assertEqual((user["blocked"], user["blocked_reason"]), (1, "doubt"))
        with self.assertRaises(L.LedgerError):
            L.block_user(self.db, self.ali, admin_id=self.admin, reason="again")
        with self.assertRaises(L.LedgerError):
            L.unblock_user(self.db, self.ali, admin_id=self.admin, reason="")
        L.unblock_user(self.db, self.ali, admin_id=self.admin, reason="cleared")
        L.transfer(self.db, self.ali, self.sam, lyd("1"))
        actions = [(r["action"], r["details"]) for r in self.db.execute(
            "SELECT action, details FROM admin_log WHERE action LIKE '%block%' ORDER BY id")]
        self.assertEqual(actions, [("block_user", '{"reason": "doubt"}'),
                                   ("unblock_user", '{"reason": "cleared"}')])

    def test_blocking_needs_a_reason_and_an_admin(self):
        with self.assertRaises(L.LedgerError):
            L.block_user(self.db, self.ali, admin_id=self.admin, reason=" ")
        with self.assertRaises(L.PermissionDenied):
            L.block_user(self.db, self.ali, admin_id=self.sam, reason="x")

    def test_blocked_admin_loses_admin_rights_and_cannot_block_self(self):
        boss = L.create_user(self.db, "admin", "Boss", "0930000000", admin_id=self.admin)
        with self.assertRaises(L.LedgerError):
            L.block_user(self.db, boss, admin_id=boss, reason="x")
        L.block_user(self.db, boss, admin_id=self.admin, reason="suspicious payouts")
        with self.assertRaises(L.PermissionDenied):
            L.deposit(self.db, self.ali, lyd("1"), admin_id=boss, into="BANK")

    # --- deactivation ---
    def test_deactivate_only_at_zero_balance(self):
        with self.assertRaises(L.LedgerError):
            L.deactivate_user(self.db, self.ali, admin_id=self.admin, reason="left")
        L.withdraw_cash(self.db, self.ali, lyd("100"), 0, admin_id=self.admin,
                        from_till=self.till)
        L.deactivate_user(self.db, self.ali, admin_id=self.admin, reason="left")
        with self.assertRaises(L.UnknownAccount):
            L.transfer(self.db, self.sam, self.ali, lyd("1"))
        self.assertEqual(L.find_user_by_phone(self.db, "0911111111")["active"], 0)

    def test_deactivate_waits_for_unfinished_orders(self):
        q = O.get_quote(self.db, self.ali, "USD", "10")
        o = O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1")
        L.withdraw_cash(self.db, self.ali, L.balance(self.db, self.ali), 0,
                        admin_id=self.admin, from_till=self.till)
        with self.assertRaises(L.LedgerError):
            L.deactivate_user(self.db, self.ali, admin_id=self.admin, reason="left")
        O.cancel_order(self.db, o["id"], admin_id=self.admin, reason="customer left")
        with self.assertRaises(L.LedgerError):          # refund is back on the balance
            L.deactivate_user(self.db, self.ali, admin_id=self.admin, reason="left")

    def test_reversal_cannot_put_money_on_a_deactivated_account(self):
        out = L.withdraw_cash(self.db, self.ali, lyd("100"), 0, admin_id=self.admin,
                              from_till=self.till)
        L.deactivate_user(self.db, self.ali, admin_id=self.admin, reason="left")
        with self.assertRaises(L.LedgerError):
            L.reverse(self.db, out, admin_id=self.admin, reason="typed wrong")
        self.assertEqual(L.balance(self.db, self.ali), 0)
        self.assertEqual(O.verify(self.db), [])
        L.reactivate_user(self.db, self.ali, admin_id=self.admin, reason="pay back")
        L.reverse(self.db, out, admin_id=self.admin, reason="typed wrong")
        self.assertEqual(L.balance(self.db, self.ali), lyd("100"))

    def test_verify_flags_money_on_a_deactivated_account(self):
        self.db.execute("UPDATE users SET active = 0 WHERE id = ?", (self.ali,))
        self.assertEqual(O.verify(self.db),
                         [f"account CUS_{self.ali} of a deactivated user holds 100000"])

    def test_reactivate_and_no_self_deactivation(self):
        L.deactivate_user(self.db, self.sam, admin_id=self.admin, reason="left")
        L.reactivate_user(self.db, self.sam, admin_id=self.admin, reason="came back")
        L.transfer(self.db, self.ali, self.sam, lyd("1"))
        with self.assertRaises(L.LedgerError):
            L.deactivate_user(self.db, self.admin, admin_id=self.admin, reason="x")


if __name__ == "__main__":
    unittest.main()
