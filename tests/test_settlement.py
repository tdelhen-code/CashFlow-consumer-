import json
import os
import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import ledger as L
from app import orders as O

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def lyd(x):
    return L.parse_lyd(x)


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.db = O.connect(":memory:")
        self.addCleanup(self.db.close)
        self.admin = L.create_user(self.db, "admin", "Admin", "0910000000")
        self.ali = L.create_user(self.db, "customer", "Ali", "0911111111",
                                 admin_id=self.admin)
        L.payroll_credit(self.db, [(self.ali, lyd("5000"))], admin_id=self.admin,
                         batch_ref="OCT")
        self.till = L.add_till(self.db, "main", admin_id=self.admin)
        L.move_money(self.db, "BANK", self.till, lyd("100"), admin_id=self.admin)
        for code in ("USD", "EGP"):
            O.add_currency(self.db, code, admin_id=self.admin)
        O.set_price(self.db, "USD", "7.85", admin_id=self.admin, now=T0)
        O.set_price(self.db, "EGP", "0.1534", admin_id=self.admin, now=T0)
        self.cairo = O.add_supplier(self.db, "Cairo agent", "EG", admin_id=self.admin)
        self.tunis = O.add_supplier(self.db, "Tunis agent", "TN", admin_id=self.admin)
        O.set_supplier_price(self.db, self.cairo, "USD", "7.80", admin_id=self.admin)
        O.set_supplier_price(self.db, self.cairo, "EGP", "0.15", admin_id=self.admin)
        O.set_supplier_price(self.db, self.tunis, "USD", "7.82", admin_id=self.admin)

    def order(self, amount="100", currency="USD", at=T0):
        q = O.get_quote(self.db, self.ali, currency, amount, now=at)
        return O.place_order(self.db, self.ali, q["quote_id"], "Ahmed Saleh", "A1234567",
                             now=at)

    def pay_out(self, o, supplier=None, rate=None, at=T0):
        O.assign_supplier(self.db, o["id"], supplier or self.cairo, admin_id=self.admin,
                          rate=rate, now=at)
        return O.mark_paid(self.db, o["receipt_code"], "REF-" + o["receipt_code"],
                           admin_id=self.admin, now=at)

    def assertHealthy(self):
        self.assertEqual(O.verify(self.db), [])

    # --- payout splits the order money ---
    def test_paid_order_splits_into_supplier_cost_and_margin(self):
        o = self.order("100")
        self.assertEqual(L.account_balance(self.db, "SYS_ORDERS"), lyd("785"))
        self.pay_out(o)
        self.assertEqual(L.account_balance(self.db, "SYS_ORDERS"), 0)
        self.assertEqual(O.supplier_balance(self.db, self.cairo), lyd("780"))
        self.assertEqual(L.account_balance(self.db, "SYS_MARGIN"), lyd("5"))
        paid = O.find_by_receipt(self.db, o["receipt_code"])
        self.assertEqual((paid["supplier_rate"], paid["supplier_cost"]), ("7.80", lyd("780")))
        self.assertHealthy()

    def test_supplier_cost_rounds_half_up_to_the_dirham(self):
        o = self.order("1234.57", "EGP")           # 189.383038 LYD charged
        self.assertEqual(o["cost"], 189383)
        self.pay_out(o)                             # 185.1855 LYD supplier cost
        self.assertEqual(O.supplier_balance(self.db, self.cairo), 185186)
        self.assertEqual(L.account_balance(self.db, "SYS_MARGIN"), 189383 - 185186)
        self.assertHealthy()

    def test_rate_is_locked_when_the_supplier_is_assigned(self):
        o = self.order("100")
        O.assign_supplier(self.db, o["id"], self.cairo, admin_id=self.admin)
        O.set_supplier_price(self.db, self.cairo, "USD", "7.90", admin_id=self.admin)
        O.mark_paid(self.db, o["receipt_code"], "REF", admin_id=self.admin)
        self.assertEqual(O.supplier_balance(self.db, self.cairo), lyd("780"))
        self.assertHealthy()

    def test_one_off_rate_and_reassigning_after_unassign_recompute_the_cost(self):
        o = self.order("100")
        O.assign_supplier(self.db, o["id"], self.cairo, admin_id=self.admin, rate="7.81")
        self.assertEqual(O.find_by_receipt(self.db, o["receipt_code"])["supplier_cost"],
                         lyd("781"))
        O.unassign_supplier(self.db, o["id"], admin_id=self.admin,
                            reason="Cairo confirmed it was not paid")
        self.pay_out(o, supplier=self.tunis)
        self.assertEqual(O.supplier_balance(self.db, self.cairo), 0)
        self.assertEqual(O.supplier_balance(self.db, self.tunis), lyd("782"))
        self.assertEqual(L.account_balance(self.db, "SYS_MARGIN"), lyd("3"))
        self.assertHealthy()

    def test_an_assigned_order_moves_only_through_unassign(self):
        o = self.order("100")
        O.assign_supplier(self.db, o["id"], self.cairo, admin_id=self.admin)
        O.set_supplier_price(self.db, self.cairo, "USD", "7.95", admin_id=self.admin)
        for supplier in (self.tunis, self.cairo):     # another one, or the same again
            with self.assertRaises(L.Conflict):
                O.assign_supplier(self.db, o["id"], supplier, admin_id=self.admin)
        held = O.find_by_receipt(self.db, o["receipt_code"])
        self.assertEqual((held["supplier_id"], held["supplier_cost"]),
                         (self.cairo, lyd("780")))
        self.assertHealthy()

    def test_a_stale_confirmation_does_not_pay_the_wrong_supplier(self):
        o = self.order("100")
        O.assign_supplier(self.db, o["id"], self.cairo, admin_id=self.admin)
        O.unassign_supplier(self.db, o["id"], admin_id=self.admin, reason="Cairo is slow")
        O.assign_supplier(self.db, o["id"], self.tunis, admin_id=self.admin)
        with self.assertRaises(L.Conflict):        # Cairo's confirmation, typed in late
            O.mark_paid(self.db, o["receipt_code"], "CAIRO-77", admin_id=self.admin,
                        supplier_id=self.cairo)
        self.assertEqual(O.find_by_receipt(self.db, o["receipt_code"])["status"], "assigned")
        self.assertEqual(O.supplier_balance(self.db, self.tunis), 0)
        O.mark_paid(self.db, o["receipt_code"], "TUNIS-1", admin_id=self.admin,
                    supplier_id=self.tunis)
        self.assertEqual(O.supplier_balance(self.db, self.tunis), lyd("782"))
        logged = self.db.execute(
            "SELECT details FROM admin_log WHERE action = 'mark_paid'").fetchone()[0]
        self.assertEqual(json.loads(logged)["supplier_id"], self.tunis)
        self.assertHealthy()

    def test_a_placed_order_is_marked_paid_only_with_the_supplier_who_paid_it(self):
        # Unassigned on the supplier's word that it had not paid, then it sends
        # proof that it did: its rate is locked on in the same step.
        o = self.order("100")
        with self.assertRaises(L.LedgerError):
            O.mark_paid(self.db, o["receipt_code"], "TUNIS-1", admin_id=self.admin)
        with self.assertRaises(L.NotFound):
            O.mark_paid(self.db, o["receipt_code"], "TUNIS-1", admin_id=self.admin,
                        supplier_id=999)
        egp = self.order("1000", "EGP")
        with self.assertRaises(L.LedgerError):          # no agreed EGP rate with Tunis
            O.mark_paid(self.db, egp["receipt_code"], "TUNIS-2", admin_id=self.admin,
                        supplier_id=self.tunis)
        self.assertEqual(O.find_by_receipt(self.db, egp["receipt_code"])["status"], "placed")
        O.mark_paid(self.db, o["receipt_code"], "TUNIS-1", admin_id=self.admin,
                    supplier_id=self.tunis, rate="7.81")
        paid = O.find_by_receipt(self.db, o["receipt_code"])
        self.assertEqual((paid["status"], paid["supplier_id"], paid["supplier_cost"]),
                         ("paid", self.tunis, lyd("781")))
        self.assertEqual(O.supplier_balance(self.db, self.tunis), lyd("781"))
        later = self.order("10")
        O.assign_supplier(self.db, later["id"], self.cairo, admin_id=self.admin)
        with self.assertRaises(L.LedgerError):          # its rate was locked on assigning
            O.mark_paid(self.db, later["receipt_code"], "CAIRO-1", admin_id=self.admin,
                        rate="7.00")
        self.assertHealthy()

    def test_a_payout_recorded_after_an_unassign_keeps_the_rate_it_was_made_at(self):
        # Cairo held the order at 7.80 and paid it then; its price went up
        # before the proof arrived. Without a one-off rate, Cairo is owed what
        # it was assigned at; a supplier that never held the order, its rate now.
        o, never = self.order("100"), self.order("10")
        O.assign_supplier(self.db, o["id"], self.cairo, admin_id=self.admin)
        O.unassign_supplier(self.db, o["id"], admin_id=self.admin,
                            reason="Cairo says it has not paid")
        O.assign_supplier(self.db, o["id"], self.tunis, admin_id=self.admin)
        O.unassign_supplier(self.db, o["id"], admin_id=self.admin,
                            reason="Tunis cannot reach the bank")
        O.set_supplier_price(self.db, self.cairo, "USD", "7.95", admin_id=self.admin)
        O.mark_paid(self.db, o["receipt_code"], "CAIRO-PROOF", admin_id=self.admin,
                    supplier_id=self.cairo)
        paid = O.find_by_receipt(self.db, o["receipt_code"])
        self.assertEqual((paid["supplier_rate"], paid["supplier_cost"]), ("7.80", lyd("780")))
        self.assertEqual(O.supplier_balance(self.db, self.cairo), lyd("780"))
        O.mark_paid(self.db, never["receipt_code"], "CAIRO-2", admin_id=self.admin,
                    supplier_id=self.cairo)
        self.assertEqual(O.find_by_receipt(self.db, never["receipt_code"])["supplier_cost"],
                         lyd("79.5"))
        rates = [(d["rate"], d["rate_from"]) for d in (json.loads(r["details"]) for r in
                 self.db.execute("SELECT details FROM admin_log WHERE action = 'mark_paid' "
                                 "ORDER BY id"))]
        self.assertEqual(rates, [("7.80", "assignment"), ("7.95", "agreed")])
        self.assertHealthy()

    def test_a_supplier_switched_off_gets_no_new_orders_but_its_open_ones_settle(self):
        paid, taken_back, new = self.order("100"), self.order("10"), self.order("20")
        for o in (paid, taken_back):
            O.assign_supplier(self.db, o["id"], self.cairo, admin_id=self.admin)
        O.set_supplier_active(self.db, self.cairo, False, admin_id=self.admin,
                              reason="stopped answering")
        with self.assertRaises(L.LedgerError) as caught:
            O.assign_supplier(self.db, new["id"], self.cairo, admin_id=self.admin)
        self.assertNotIsInstance(caught.exception, L.NotFound)
        O.mark_paid(self.db, paid["receipt_code"], "CAIRO-1", admin_id=self.admin)
        O.unassign_supplier(self.db, taken_back["id"], admin_id=self.admin,
                            reason="Cairo has not paid it")
        O.pay_supplier(self.db, self.cairo, lyd("780"), admin_id=self.admin,
                       from_code="BANK", reference="W-1")
        self.assertEqual(O.supplier_balance(self.db, self.cairo), 0)
        O.assign_supplier(self.db, new["id"], self.tunis, admin_id=self.admin)
        O.set_supplier_active(self.db, self.cairo, True, admin_id=self.admin, reason="back")
        O.assign_supplier(self.db, taken_back["id"], self.cairo, admin_id=self.admin)
        log = [(r["target"], json.loads(r["details"])) for r in self.db.execute(
            "SELECT target, details FROM admin_log WHERE action = 'set_supplier_active' "
            "ORDER BY id")]
        self.assertEqual(log, [(str(self.cairo), {"active": False,
                                                  "reason": "stopped answering"}),
                               (str(self.cairo), {"active": True, "reason": "back"})])
        self.assertEqual(self.db.execute("SELECT active FROM suppliers WHERE id = ?",
                                         (self.cairo,)).fetchone()[0], 1)
        self.assertHealthy()

    def test_switching_a_supplier_needs_an_admin_a_reason_and_a_change(self):
        with self.assertRaises(L.PermissionDenied):
            O.set_supplier_active(self.db, self.cairo, False, admin_id=self.ali, reason="x")
        with self.assertRaises(L.LedgerError):
            O.set_supplier_active(self.db, self.cairo, False, admin_id=self.admin, reason=" ")
        with self.assertRaises(L.LedgerError):          # it is on already
            O.set_supplier_active(self.db, self.cairo, True, admin_id=self.admin, reason="x")
        with self.assertRaises(L.NotFound):
            O.set_supplier_active(self.db, 999, False, admin_id=self.admin, reason="x")
        with self.assertRaises(L.LedgerError):
            O.set_supplier_active(self.db, self.cairo, 0, admin_id=self.admin, reason="x")

    def test_assigning_needs_an_agreed_rate(self):
        o = self.order("1000", "EGP")
        with self.assertRaises(L.LedgerError):
            O.assign_supplier(self.db, o["id"], self.tunis, admin_id=self.admin)
        with self.assertRaises(L.InvalidAmount):
            O.assign_supplier(self.db, o["id"], self.tunis, admin_id=self.admin,
                              rate="-1")

    def test_zero_margin_posts_no_margin_entry(self):
        txn = self.pay_out(self.order("100"), rate="7.85")
        accounts = [r["code"] for r in self.db.execute(
            "SELECT a.code FROM entries e JOIN accounts a ON a.id = e.account_id "
            "WHERE e.txn_id = ? ORDER BY a.code", (txn,))]
        self.assertEqual(accounts, [f"SUP_{self.cairo}", "SYS_ORDERS"])
        self.assertHealthy()

    def test_times_in_other_zones_are_stored_in_utc(self):
        tripoli = timezone(timedelta(hours=2))
        self.pay_out(self.order("100"), at=T0.astimezone(tripoli))
        self.assertEqual(len(O.margin_report(self.db, since=T0,
                                             until=T0 + timedelta(seconds=1))), 1)

    def test_a_loss_shows_as_negative_margin(self):
        o = self.order("100")
        self.pay_out(o, rate="7.90")
        self.assertEqual(L.account_balance(self.db, "SYS_MARGIN"), -lyd("5"))
        self.assertHealthy()

    def test_paid_cannot_be_set_without_the_payout_posting(self):
        o = self.order("100")
        O.assign_supplier(self.db, o["id"], self.cairo, admin_id=self.admin)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(f"UPDATE orders SET status = 'paid' WHERE id = {o['id']}")

    def test_payout_cannot_be_reversed(self):
        txn = self.pay_out(self.order("100"))
        with self.assertRaises(L.LedgerError):
            L.reverse(self.db, txn, admin_id=self.admin, reason="oops")

    # --- paying suppliers ---
    def test_paying_a_supplier_reduces_what_we_owe_and_the_bank(self):
        self.pay_out(self.order("100"))
        bank = L.holding_amount(self.db, "BANK")
        O.pay_supplier(self.db, self.cairo, lyd("500"), admin_id=self.admin,
                       from_code="BANK", reference="WIRE-1")
        self.assertEqual(O.supplier_balance(self.db, self.cairo), lyd("280"))
        self.assertEqual(L.holding_amount(self.db, "BANK"), bank - lyd("500"))
        self.assertEqual([r["amount"] for r in O.supplier_statement(self.db, self.cairo)],
                         [-lyd("500"), lyd("780")])
        self.assertHealthy()

    def test_overpaying_leaves_a_prepayment(self):
        O.pay_supplier(self.db, self.tunis, lyd("50"), admin_id=self.admin,
                       from_code=self.till, reference="CASH-1")
        self.assertEqual(O.supplier_balance(self.db, self.tunis), -lyd("50"))
        self.pay_out(self.order("10"), supplier=self.tunis)
        self.assertEqual(O.supplier_balance(self.db, self.tunis), lyd("28.2"))
        self.assertHealthy()

    def test_cannot_pay_more_cash_than_the_till_holds(self):
        with self.assertRaises(L.NotEnoughCash):
            O.pay_supplier(self.db, self.cairo, lyd("101"), admin_id=self.admin,
                           from_code=self.till, reference="CASH-2")

    def test_supplier_payment_is_idempotent_reversible_and_admin_only(self):
        a = O.pay_supplier(self.db, self.cairo, lyd("10"), admin_id=self.admin,
                           from_code="BANK", reference="W", idempotency_key="pay-1")
        b = O.pay_supplier(self.db, self.cairo, lyd("10"), admin_id=self.admin,
                           from_code="BANK", reference="W", idempotency_key="pay-1")
        self.assertEqual(a, b)
        self.assertEqual(O.supplier_balance(self.db, self.cairo), -lyd("10"))
        L.reverse(self.db, a, admin_id=self.admin, reason="wrong supplier")
        self.assertEqual(O.supplier_balance(self.db, self.cairo), 0)
        with self.assertRaises(L.PermissionDenied):
            O.pay_supplier(self.db, self.cairo, lyd("1"), admin_id=self.ali,
                           from_code="BANK", reference="W")
        with self.assertRaises(L.LedgerError):
            O.pay_supplier(self.db, self.cairo, lyd("1"), admin_id=self.admin,
                           from_code="BANK", reference=" ")
        self.assertHealthy()

    def test_supplier_payment_key_reused_for_another_reference_is_refused(self):
        a = O.pay_supplier(self.db, self.cairo, lyd("10"), admin_id=self.admin,
                           from_code="BANK", reference="WIRE-1", idempotency_key="pay-1")
        with self.assertRaises(L.LedgerError):
            O.pay_supplier(self.db, self.cairo, lyd("10"), admin_id=self.admin,
                           from_code="BANK", reference="WIRE-2", idempotency_key="pay-1")
        self.assertEqual(O.pay_supplier(self.db, self.cairo, lyd("10"), admin_id=self.admin,
                                        from_code="BANK", reference=" WIRE-1 ",
                                        idempotency_key="pay-1"), a)
        self.assertEqual(O.supplier_balance(self.db, self.cairo), -lyd("10"))

    # --- reporting and checks ---
    def test_margin_report_groups_and_filters_by_paid_time(self):
        self.pay_out(self.order("100"), at=T0)
        self.pay_out(self.order("50"), at=T0 + timedelta(hours=1))
        self.pay_out(self.order("100"), supplier=self.tunis, at=T0 + timedelta(hours=2))
        self.pay_out(self.order("1000", "EGP"), at=T0 + timedelta(days=1))
        cancelled = self.order("100")
        O.cancel_order(self.db, cancelled["id"], admin_id=self.admin, reason="test")

        rows = O.margin_report(self.db, since=T0, until=T0 + timedelta(days=1))
        self.assertEqual(
            [(r["currency"], r["supplier"], r["orders"], r["foreign_amount"],
              r["charged"], r["supplier_cost"], r["margin"]) for r in rows],
            [("USD", "Cairo agent", 2, "150.00", lyd("1177.5"), lyd("1170"), lyd("7.5")),
             ("USD", "Tunis agent", 1, "100.00", lyd("785"), lyd("782"), lyd("3"))])
        everything = O.margin_report(self.db)
        self.assertEqual(sum(r["margin"] for r in everything),
                         L.account_balance(self.db, "SYS_MARGIN"))
        self.assertHealthy()

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_margin_report_times_without_a_zone_are_utc(self):
        sept_30_late = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
        self.pay_out(self.order("100", at=sept_30_late), at=sept_30_late)
        try:
            with mock.patch.dict(os.environ, {"TZ": "LYT-2"}):   # server on UTC+2
                time.tzset()
                september = O.margin_report(self.db, since=datetime(2026, 9, 1),
                                            until=datetime(2026, 10, 1))
                october = O.margin_report(self.db, since=datetime(2026, 10, 1),
                                          until=datetime(2026, 11, 1))
        finally:
            time.tzset()                                      # back to the real zone
        self.assertEqual(([r["orders"] for r in september], october), ([1], []))

    def test_verify_catches_margin_posted_outside_an_order(self):
        L._post(self.db, "payout", [(L._system(self.db, "SYS_ORDERS"), -1),
                                    (L._system(self.db, "SYS_MARGIN"), 1)], self.admin)
        problems = O.verify(self.db)
        self.assertTrue(any("margin" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
