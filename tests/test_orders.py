import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import ledger as L
from app import orders as O

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def lyd(x):
    return L.parse_lyd(x)


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.db = O.connect(":memory:")
        self.addCleanup(self.db.close)
        self.admin = L.create_user(self.db, "admin", "Admin", "0910000000")
        self.ali = L.create_user(self.db, "customer", "Ali", "0911111111",
                                 admin_id=self.admin)
        self.sam = L.create_user(self.db, "customer", "Sam", "0922222222",
                                 admin_id=self.admin)
        L.payroll_credit(self.db, [(self.ali, lyd("1000"))], admin_id=self.admin,
                         batch_ref="OCT")
        O.add_currency(self.db, "USD", admin_id=self.admin)
        O.add_currency(self.db, "EGP", admin_id=self.admin)
        O.set_price(self.db, "USD", "7.85", admin_id=self.admin, now=T0)
        O.set_price(self.db, "EGP", "0.1534", admin_id=self.admin, now=T0)
        self.supplier = O.add_supplier(self.db, "Cairo agent", "EG", admin_id=self.admin)
        O.set_supplier_price(self.db, self.supplier, "USD", "7.80", admin_id=self.admin)
        O.set_supplier_price(self.db, self.supplier, "EGP", "0.15", admin_id=self.admin)

    def order(self, amount="100", currency="USD", who=None, at=T0):
        who = who or self.ali
        q = O.get_quote(self.db, who, currency, amount, now=at)
        return O.place_order(self.db, who, q["quote_id"], "Ahmed Saleh", "A1234567",
                             now=at + timedelta(minutes=2))

    def assertHealthy(self):
        self.assertEqual(O.verify(self.db), [])

    # --- quotes ---
    def test_quote_price_and_expiry(self):
        q = O.get_quote(self.db, self.ali, "USD", "100", now=T0)
        self.assertEqual(q["cost"], lyd("785"))
        self.assertEqual(q["expires_at"], (T0 + timedelta(minutes=10)).isoformat())
        self.assertEqual(O.get_quote(self.db, self.ali, "EGP", "1000", now=T0)["cost"],
                         lyd("153.4"))
        self.assertEqual(O.get_quote(self.db, self.ali, "USD", "0.01", now=T0)["cost"],
                         79)  # 0.0785 LYD = 78.5 dirhams, rounds half up

    def test_quote_keeps_its_price_after_a_price_change(self):
        q = O.get_quote(self.db, self.ali, "USD", "100", now=T0)
        O.set_price(self.db, "USD", "8.00", admin_id=self.admin, now=T0)
        o = O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1",
                          now=T0 + timedelta(minutes=5))
        self.assertEqual(o["cost"], lyd("785"))

    def test_bad_amounts_and_unknown_currency(self):
        for bad in ("0", "-5", "1.234", "abc"):
            with self.assertRaises(L.InvalidAmount):
                O.get_quote(self.db, self.ali, "USD", bad, now=T0)
        with self.assertRaises(L.LedgerError):
            O.get_quote(self.db, self.ali, "GBP", "10", now=T0)

    # --- placing orders ---
    def test_place_order_charges_and_issues_receipt(self):
        o = self.order()
        self.assertEqual(L.balance(self.db, self.ali), lyd("215"))
        self.assertEqual(o["status"], "placed")
        self.assertEqual(len(o["receipt_code"]), O.RECEIPT_LENGTH)
        self.assertTrue(set(o["receipt_code"]) <= set(O.RECEIPT_ALPHABET))
        self.assertHealthy()

    def test_expired_quote_is_refused(self):
        q = O.get_quote(self.db, self.ali, "USD", "100", now=T0)
        with self.assertRaises(O.QuoteExpired):
            O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1",
                          now=T0 + timedelta(minutes=10))
        self.assertEqual(L.balance(self.db, self.ali), lyd("1000"))

    def test_double_tap_charges_once(self):
        q = O.get_quote(self.db, self.ali, "USD", "100", now=T0)
        a = O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1", now=T0)
        b = O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1", now=T0)
        self.assertEqual(a["receipt_code"], b["receipt_code"])
        self.assertEqual(L.balance(self.db, self.ali), lyd("215"))

    def test_same_quote_for_another_beneficiary_is_refused(self):
        q = O.get_quote(self.db, self.ali, "USD", "100", now=T0)
        first = O.place_order(self.db, self.ali, q["quote_id"], "Ahmed Salh", "A12345",
                              now=T0)
        for name, id_number in (("Ahmed Saleh", "A12345"), ("Ahmed Salh", "A123456")):
            with self.assertRaises(L.Conflict):
                O.place_order(self.db, self.ali, q["quote_id"], name, id_number, now=T0)
        again = O.place_order(self.db, self.ali, q["quote_id"], " Ahmed Salh ", "A12345",
                              now=T0 + timedelta(minutes=30))     # a late retry is fine
        self.assertEqual(again["receipt_code"], first["receipt_code"])
        self.assertEqual(L.balance(self.db, self.ali), lyd("215"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)

    def test_amounts_and_rates_are_stored_in_one_plain_form(self):
        for typed, places, stored in (("1E+1", 2, "10.00"), ("10.0", 2, "10.00"),
                                      ("0.5", 2, "0.50"), ("1E+12", 2, "1000000000000.00"),
                                      ("785E-2", 6, "7.85"), ("7.850000", 6, "7.85"),
                                      ("0.1534", 6, "0.1534"), ("0.000001", 6, "0.000001")):
            self.assertEqual(str(O._decimal(typed, places, "x")), stored, typed)
        O.set_price(self.db, "USD", "785E-2", admin_id=self.admin, now=T0)
        q = O.get_quote(self.db, self.ali, "USD", "1e1", now=T0)
        self.assertEqual((q["amount"], q["rate"], q["cost"]), ("10.00", "7.85", lyd("78.5")))
        o = O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1", now=T0)
        O.assign_supplier(self.db, o["id"], self.supplier, admin_id=self.admin,
                          rate="0.78E1")
        row = O.find_by_receipt(self.db, o["receipt_code"])
        self.assertEqual((row["foreign_amount"], row["rate"], row["supplier_rate"]),
                         ("10.00", "7.85", "7.80"))
        O.set_supplier_price(self.db, self.supplier, "EGP", "0.1500", admin_id=self.admin)
        self.assertEqual(O.current_supplier_price(self.db, self.supplier, "EGP")["rate"],
                         "0.15")

    def test_cannot_use_someone_elses_quote(self):
        q = O.get_quote(self.db, self.ali, "USD", "10", now=T0)
        with self.assertRaises(L.LedgerError):
            O.place_order(self.db, self.sam, q["quote_id"], "X", "Y", now=T0)

    def test_not_enough_balance_creates_no_order(self):
        with self.assertRaises(L.InsufficientFunds):
            self.order(amount="200")   # 1,570 LYD
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(L.balance(self.db, self.ali), lyd("1000"))

    def test_beneficiary_is_required(self):
        q = O.get_quote(self.db, self.ali, "USD", "10", now=T0)
        with self.assertRaises(L.LedgerError):
            O.place_order(self.db, self.ali, q["quote_id"], " ", "A1", now=T0)

    # --- payout ---
    def test_full_flow_and_payout_only_once(self):
        o = self.order()
        O.assign_supplier(self.db, o["id"], self.supplier, admin_id=self.admin)
        O.mark_paid(self.db, o["receipt_code"].lower(), "EG-778812", admin_id=self.admin)
        self.assertEqual(O.find_by_receipt(self.db, o["receipt_code"])["status"], "paid")
        with self.assertRaises(L.LedgerError):
            O.mark_paid(self.db, o["receipt_code"], "EG-778812", admin_id=self.admin)
        with self.assertRaises(L.LedgerError):
            O.cancel_order(self.db, o["id"], admin_id=self.admin, reason="late")
        self.assertHealthy()

    def test_cannot_mark_paid_before_a_supplier_is_assigned(self):
        o = self.order()
        with self.assertRaises(L.LedgerError):
            O.mark_paid(self.db, o["receipt_code"], "REF", admin_id=self.admin)

    def test_unknown_receipt_code(self):
        with self.assertRaises(L.LedgerError):
            O.mark_paid(self.db, "NOTAREAL22", "REF", admin_id=self.admin)

    # --- cancellation ---
    def test_cancel_refunds_once(self):
        o = self.order()
        O.assign_supplier(self.db, o["id"], self.supplier, admin_id=self.admin)
        with self.assertRaises(L.LedgerError):     # still with the supplier
            O.cancel_order(self.db, o["id"], admin_id=self.admin, reason="offline")
        with self.assertRaises(L.LedgerError):     # a reason is required
            O.unassign_supplier(self.db, o["id"], admin_id=self.admin, reason=" ")
        O.unassign_supplier(self.db, o["id"], admin_id=self.admin,
                            reason="supplier confirmed not paid")
        unassigned = O.find_by_receipt(self.db, o["receipt_code"])
        self.assertEqual((unassigned["status"], unassigned["supplier_id"],
                          unassigned["supplier_cost"]), ("placed", None, None))
        O.cancel_order(self.db, o["id"], admin_id=self.admin, reason="supplier offline")
        self.assertEqual(L.balance(self.db, self.ali), lyd("1000"))
        self.assertEqual(O.find_by_receipt(self.db, o["receipt_code"])["status"],
                         "cancelled")
        with self.assertRaises(L.LedgerError):
            O.cancel_order(self.db, o["id"], admin_id=self.admin, reason="again")
        self.assertHealthy()

    def test_order_charge_cannot_be_reversed_directly(self):
        o = self.order()
        with self.assertRaises(L.LedgerError):
            L.reverse(self.db, o["charge_txn_id"], admin_id=self.admin, reason="x")

    # --- permissions and tampering ---
    def test_workers_cannot_run_the_admin_side(self):
        o = self.order()
        with self.assertRaises(L.PermissionDenied):
            O.set_price(self.db, "USD", "1", admin_id=self.ali)
        with self.assertRaises(L.PermissionDenied):
            O.assign_supplier(self.db, o["id"], self.supplier, admin_id=self.ali)
        with self.assertRaises(L.PermissionDenied):
            O.cancel_order(self.db, o["id"], admin_id=self.ali, reason="refund me")

    def test_order_terms_and_price_history_cannot_be_edited(self):
        o = self.order()
        for sql in (f"UPDATE orders SET cost = 1 WHERE id = {o['id']}",
                    f"UPDATE orders SET beneficiary_name = 'Someone' WHERE id = {o['id']}",
                    "DELETE FROM orders",
                    "UPDATE prices SET rate = '1'",
                    "UPDATE quotes SET cost = 1"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)

    def test_paid_order_is_frozen_in_the_database(self):
        o = self.order()
        O.assign_supplier(self.db, o["id"], self.supplier, admin_id=self.admin)
        O.mark_paid(self.db, o["receipt_code"], "REF-1", admin_id=self.admin)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(f"UPDATE orders SET status = 'placed' WHERE id = {o['id']}")

    def test_verify_catches_a_refund_that_skipped_the_order(self):
        o = self.order()
        L._reverse(self.db, o["charge_txn_id"], self.admin, "bypass")
        self.assertTrue(O.verify(self.db))

    def test_verify_reads_one_snapshot_while_others_write(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "wallet.db")
        db, other = O.connect(path), O.connect(path)     # two server threads
        self.addCleanup(db.close)
        self.addCleanup(other.close)
        admin = L.create_user(db, "admin", "Admin", "0910000000")
        ali = L.create_user(db, "customer", "Ali", "0911111111", admin_id=admin)
        L.deposit(db, ali, lyd("1000"), admin_id=admin, into="BANK")
        O.add_currency(db, "USD", admin_id=admin)
        O.set_price(db, "USD", "7.85", admin_id=admin, now=T0)
        q = O.get_quote(db, ali, "USD", "100", now=T0)
        real_balance = L.account_balance

        def order_placed_meanwhile(conn, code):
            # the other connection commits an order just after SYS_ORDERS is read
            result = real_balance(conn, code)
            if code == "SYS_ORDERS":
                O.place_order(other, ali, q["quote_id"], "Ahmed", "A1", now=T0)
            return result

        with mock.patch.object(L, "account_balance", order_placed_meanwhile):
            self.assertEqual(O.verify(db), [])
        self.assertEqual(L.account_balance(db, "SYS_ORDERS"), lyd("785"))
        self.assertEqual(O.verify(db), [])
        self.assertFalse(db.in_transaction)

    def test_huge_foreign_amounts_are_refused_cleanly(self):
        for amount in ("1e30", "1e999999", "2e15"):
            with self.assertRaises(L.InvalidAmount):
                O.get_quote(self.db, self.ali, "USD", amount, now=T0)

    def test_no_order_on_a_currency_switched_off_after_the_quote(self):
        q = O.get_quote(self.db, self.ali, "USD", "10", now=T0)
        O.set_currency_active(self.db, "usd", False, admin_id=self.admin,
                              reason="wrong price typed")
        with self.assertRaises(L.LedgerError):
            O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1", now=T0)
        with self.assertRaises(L.LedgerError):
            O.get_quote(self.db, self.ali, "USD", "10", now=T0)
        self.assertEqual(L.balance(self.db, self.ali), lyd("1000"))
        O.set_price(self.db, "USD", "7.90", admin_id=self.admin, now=T0)   # while off
        O.set_currency_active(self.db, "USD", True, admin_id=self.admin, reason="fixed")
        self.assertEqual(O.get_quote(self.db, self.ali, "USD", "10", now=T0)["rate"], "7.90")
        log = [(r["action"], r["target"], r["details"]) for r in self.db.execute(
            "SELECT action, target, details FROM admin_log "
            "WHERE action = 'set_currency_active' ORDER BY id")]
        self.assertEqual(log, [
            ("set_currency_active", "USD", '{"active": false, "reason": "wrong price typed"}'),
            ("set_currency_active", "USD", '{"active": true, "reason": "fixed"}')])

    def test_switching_a_currency_needs_an_admin_a_reason_and_a_change(self):
        with self.assertRaises(L.PermissionDenied):
            O.set_currency_active(self.db, "USD", False, admin_id=self.ali, reason="x")
        with self.assertRaises(L.LedgerError):
            O.set_currency_active(self.db, "USD", False, admin_id=self.admin, reason=" ")
        with self.assertRaises(L.LedgerError):
            O.set_currency_active(self.db, "USD", True, admin_id=self.admin, reason="x")
        with self.assertRaises(L.LedgerError):
            O.set_currency_active(self.db, "GBP", False, admin_id=self.admin, reason="x")
        with self.assertRaises(L.LedgerError):
            O.set_currency_active(self.db, "USD", 0, admin_id=self.admin, reason="x")

    def test_a_corrected_price_can_void_open_quotes(self):
        before = O.get_quote(self.db, self.ali, "USD", "100", now=T0)
        O.set_price(self.db, "USD", "0.785", admin_id=self.admin, now=T0)    # typo
        typo = O.get_quote(self.db, self.ali, "USD", "100", now=T0)
        O.set_price(self.db, "USD", "7.85", admin_id=self.admin, now=T0, void_quotes=True)
        O.set_price(self.db, "USD", "7.86", admin_id=self.admin, now=T0)
        for q in (before, typo):
            with self.assertRaises(O.QuoteExpired):
                O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1",
                              now=T0 + timedelta(minutes=1))
        self.assertEqual(L.balance(self.db, self.ali), lyd("1000"))
        o = self.order("100")
        self.assertEqual(o["cost"], lyd("786"))
        with self.assertRaises(L.LedgerError):
            O.set_price(self.db, "USD", "7.85", admin_id=self.admin, void_quotes="yes")
        self.assertHealthy()

    # --- setup checks ---
    def test_currency_codes_are_three_letters_and_added_once(self):
        self.assertEqual(O.add_currency(self.db, " gbp ", admin_id=self.admin), "GBP")
        for bad in ("EURO", "us", "U5D", "", None, 123, "us\u0131"):   # \u0131 upper() is I
            with self.assertRaises(L.LedgerError, msg=repr(bad)) as caught:
                O.add_currency(self.db, bad, admin_id=self.admin)
            self.assertNotIsInstance(caught.exception, L.Conflict)
        with self.assertRaises(L.Conflict):
            O.add_currency(self.db, "usd", admin_id=self.admin)

    def test_supplier_names_are_unique(self):
        with self.assertRaises(L.Conflict):
            O.add_supplier(self.db, " cairo AGENT", "EG", admin_id=self.admin)
        with self.assertRaises(L.LedgerError):
            O.add_supplier(self.db, "  ", admin_id=self.admin)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
