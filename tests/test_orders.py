import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

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

    def test_huge_foreign_amounts_are_refused_cleanly(self):
        for amount in ("1e30", "1e999999", "2e15"):
            with self.assertRaises(L.InvalidAmount):
                O.get_quote(self.db, self.ali, "USD", amount, now=T0)

    def test_no_order_on_a_currency_switched_off_after_the_quote(self):
        q = O.get_quote(self.db, self.ali, "USD", "10", now=T0)
        self.db.execute("UPDATE currencies SET active = 0 WHERE code = 'USD'")
        with self.assertRaises(L.LedgerError):
            O.place_order(self.db, self.ali, q["quote_id"], "Ahmed", "A1", now=T0)
        self.assertEqual(L.balance(self.db, self.ali), lyd("1000"))


if __name__ == "__main__":
    unittest.main()
