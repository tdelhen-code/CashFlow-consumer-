"""Changes to admins' accounts that a second admin approves (app/approvals.py),
through the module, the web routes and app.manage."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import approvals as P
from app import auth as A
from app import ledger as L
from app import manage as M
from app import web

from .test_web import Client

PASSWORD = "Garden-Lamp-4821"


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "wallet.db")
        self.conn = A.connect(self.db)
        self.addCleanup(self.conn.close)
        self.amal = A.create_first_admin(self.conn, "Amal", "0910000001", PASSWORD)

    def admin(self, name, phone):
        """An admin made straight in the books (as the owner could)."""
        uid = L.create_user(self.conn, "admin", name, phone, admin_id=self.amal)
        return uid

    def count(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def codes(self, user_id):
        return self.count("SELECT COUNT(*) FROM setup_codes WHERE user_id = ?", user_id)

    def test_the_only_admin_acts_alone(self):
        done = P.request(self.conn, "create_admin", admin_id=self.amal, details={
            "name": "Badr", "phone": "0910000002", "language": "ar", "reason": "new office"})
        badr = done["done"]["user_id"]
        self.assertRegex(done["done"]["setup_code"], r"^[A-Z0-9]{4}-")
        self.assertEqual(self.count("SELECT language FROM users WHERE id = ?", badr), "ar")
        self.assertEqual(self.count("SELECT COUNT(*) FROM approvals"), 0)

    def test_a_reset_of_an_admin_waits_for_another_admin_who_gets_the_code(self):
        badr, chadi = self.admin("Badr", "0910000002"), self.admin("Chadi", "0910000003")
        asked = P.request(self.conn, "admin_setup_code", admin_id=self.amal, target=badr,
                          details={"reason": "forgot the password"})
        approval = asked["approval_id"]
        self.assertEqual(self.codes(badr), 0)           # nothing done yet
        [item] = P.listing(self.conn, status="pending")
        self.assertEqual((item["action"], item["target"], item["approvers"]),
                         ("admin_setup_code", badr, [chadi]))
        # Not by the one who asked, nor by the account's own admin.
        for who in (self.amal, badr):
            with self.assertRaises(L.PermissionDenied):
                P.approve(self.conn, approval, admin_id=who)
        result = P.approve(self.conn, approval, admin_id=chadi)
        self.assertEqual(result["user_id"], badr)
        self.assertEqual(self.codes(badr), 1)
        issued_by = self.count("SELECT issued_by FROM setup_codes WHERE user_id = ?", badr)
        self.assertEqual(issued_by, chadi)              # the approver's code
        A.set_password_with_code(self.conn, "0910000002", result["setup_code"], PASSWORD)
        with self.assertRaises(L.Conflict):             # once
            P.approve(self.conn, approval, admin_id=chadi)
        log = L.admin_log(self.conn, kind="approval")
        self.assertEqual([e["action"] for e in log], ["approve", "request_approval"])
        self.assertEqual(log[0]["admin_id"], chadi)
        self.assertEqual(log[0]["details"]["requested_by"], self.amal)

    def test_with_two_admins_only_the_owner_can_approve_about_the_other(self):
        badr = self.admin("Badr", "0910000002")
        approval = P.request(self.conn, "admin_setup_code", admin_id=self.amal, target=badr,
                             details={"reason": "forgot the password"})["approval_id"]
        [item] = P.listing(self.conn, status="pending")
        self.assertEqual(item["approvers"], [])
        result = P.approve(self.conn, approval)         # admin_id=None: the owner
        self.assertIn("setup_code", result)
        [entry] = L.admin_log(self.conn, action="approve")
        self.assertEqual(entry["details"]["by"], "the owner, on the server")

    def test_a_new_admin_is_made_only_when_approved(self):
        badr = self.admin("Badr", "0910000002")
        asked = P.request(self.conn, "create_admin", admin_id=self.amal, details={
            "name": " Dina ", "phone": "091 000 0004", "language": "fr",
            "whatsapp": "+20 100 123 4567", "reason": "new office"})
        self.assertEqual(self.count("SELECT COUNT(*) FROM users WHERE name = 'Dina'"), 0)
        result = P.approve(self.conn, asked["approval_id"], admin_id=badr)
        row = self.conn.execute("SELECT role, name, phone, language, whatsapp FROM users "
                                "WHERE id = ?", (result["user_id"],)).fetchone()
        self.assertEqual(tuple(row), ("admin", "Dina", "+218910000004", "fr", "+201001234567"))
        self.assertEqual([i["target"] for i in P.listing(self.conn, status="approved")],
                         [result["user_id"]])

    def test_what_cant_be_done_is_refused_when_asked_and_when_approved(self):
        badr, chadi = self.admin("Badr", "0910000002"), self.admin("Chadi", "0910000003")
        worker = L.create_user(self.conn, "customer", "Ali", "0921111111", admin_id=self.amal)
        for action, target, details, error in (
                ("unblock_admin", badr, {"reason": "x"}, L.LedgerError),        # not blocked
                ("reactivate_admin", badr, {"reason": "x"}, L.LedgerError),     # active
                ("admin_setup_code", worker, {"reason": "x"}, L.LedgerError),  # not an admin
                ("admin_setup_code", badr, {"reason": " "}, L.LedgerError),    # no reason
                ("admin_setup_code", self.amal, {"reason": "x"}, L.LedgerError),   # own
                ("create_admin", None, {"name": "X", "phone": "0910000002",
                                        "language": "en", "reason": "x"}, L.Conflict),
                ("make_owner", badr, {"reason": "x"}, L.LedgerError)):
            with self.subTest(action=action, target=target):
                with self.assertRaises(error):
                    P.request(self.conn, action, admin_id=self.amal, target=target,
                              details=details)
        self.assertEqual(self.count("SELECT COUNT(*) FROM approvals"), 0)
        # Asked while it could be done, refused at approval when it no longer can.
        L.block_user(self.conn, badr, admin_id=self.amal, reason="checking")
        approval = P.request(self.conn, "unblock_admin", admin_id=self.amal, target=badr,
                             details={"reason": "checked"})["approval_id"]
        L.unblock_user(self.conn, badr, admin_id=chadi, reason="meanwhile")
        with self.assertRaises(L.LedgerError):
            P.approve(self.conn, approval, admin_id=chadi)
        self.assertEqual(P.listing(self.conn)[0]["status"], "pending")      # nothing changed

    def test_reject_cancel_and_expire(self):
        badr, chadi = self.admin("Badr", "0910000002"), self.admin("Chadi", "0910000003")
        now = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)

        def ask():
            return P.request(self.conn, "admin_setup_code", admin_id=self.amal, target=badr,
                             details={"reason": "forgot"}, now=now)["approval_id"]
        first, second, third = ask(), ask(), ask()
        with self.assertRaises(L.LedgerError):
            P.reject(self.conn, first, admin_id=self.amal, reason="no")     # cancel instead
        with self.assertRaises(L.LedgerError):
            P.reject(self.conn, first, admin_id=chadi, reason=" ")
        P.reject(self.conn, first, admin_id=chadi, reason="Badr is on leave", now=now)
        with self.assertRaises(L.PermissionDenied):
            P.cancel(self.conn, second, admin_id=chadi)                     # not theirs
        P.cancel(self.conn, second, admin_id=self.amal, now=now)
        later = now + timedelta(hours=P.EXPIRES_HOURS)
        self.assertEqual([i["status"] for i in P.listing(self.conn, now=later)],
                         ["expired", "cancelled", "rejected"])
        for approval in (first, second, third):
            with self.assertRaises(L.Conflict):
                P.approve(self.conn, approval, admin_id=chadi, now=later)
        self.assertEqual(self.codes(badr), 0)
        self.assertEqual(P.listing(self.conn, status="rejected", now=later)[0][
            "decision_reason"], "Badr is on leave")


class ApprovalWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "wallet.db")
        conn = A.connect(self.db)
        A.create_first_admin(conn, "Amal", "0910000001", PASSWORD)
        self.badr = L.create_user(conn, "admin", "Badr", "0910000002", admin_id=1)
        self.chadi = L.create_user(conn, "admin", "Chadi", "0910000003", admin_id=1)
        for uid, phone in ((self.badr, "0910000002"), (self.chadi, "0910000003")):
            A.set_password_with_code(conn, phone, A.issue_setup_code(
                conn, uid, admin_id=1, reason="new"), PASSWORD)
        conn.close()
        self.app = web.App(self.db)
        self.addCleanup(lambda: getattr(self.app.local, "conn", None) and
                        self.app.local.conn.close())
        self.amal, self.chadi_client = self.client("0910000001", 1), self.client(
            "0910000003", 3)

    def client(self, phone, n):
        client = Client(self.app, ip=f"10.0.7.{n}")
        status, body = client.post("/api/login", {"phone": phone, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return client

    def test_admin_account_changes_wait_and_are_approved_by_another_admin(self):
        amal, chadi = self.amal, self.chadi_client
        status, asked = amal.post(f"/api/admin/users/{self.badr}/setup-code",
                                  {"reason": "forgot the password"})
        self.assertEqual((status, asked["pending"]), (200, True))
        self.assertNotIn("setup_code", asked)
        status, made = amal.post("/api/admin/users", {
            "name": "Dina", "phone": "0910000004", "role": "admin", "reason": "new office"})
        self.assertEqual((status, made["pending"], "setup_code" in made), (200, True, False))
        status, blocked = amal.post(f"/api/admin/users/{self.badr}/block", {"reason": "x"})
        self.assertEqual((status, blocked), (200, {"ok": True}))     # immediate
        status, unblock = amal.post(f"/api/admin/users/{self.badr}/unblock", {"reason": "y"})
        self.assertEqual((status, unblock["pending"]), (200, True))
        status, listed = chadi.get("/api/admin/approvals?status=pending")
        self.assertEqual([a["action"] for a in listed["approvals"]],
                         ["unblock_admin", "create_admin", "admin_setup_code"])
        self.assertEqual(amal.post(f"/api/admin/approvals/{asked['approval_id']}/approve",
                                   {})[0], 403)                      # not your own
        status, done = chadi.post(f"/api/admin/approvals/{made['approval_id']}/approve", {})
        self.assertEqual(status, 200)
        self.assertRegex(done["setup_code"], r"^[A-Z0-9]{4}-")
        status, _ = chadi.post(f"/api/admin/approvals/{unblock['approval_id']}/reject",
                               {"reason": "still checking"})
        self.assertEqual(status, 200)
        self.assertEqual(amal.post(f"/api/admin/approvals/{asked['approval_id']}/cancel",
                                   {}), (200, {"ok": True}))
        self.assertEqual(chadi.post(f"/api/admin/approvals/{asked['approval_id']}/approve",
                                    {})[0], 409)
        # Workers' accounts are not affected: done at once, as before.
        status, worker = amal.post("/api/admin/users", {"name": "Ali", "phone": "0921111111"})
        self.assertEqual((status, "setup_code" in worker), (200, True))
        status, code = amal.post(f"/api/admin/users/{worker['id']}/setup-code", {})
        self.assertEqual((status, "setup_code" in code), (200, True))


class ApprovalCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "wallet.db")
        conn = A.connect(self.db)
        amal = A.create_first_admin(conn, "Amal", "0910000001", PASSWORD)
        self.badr = L.create_user(conn, "admin", "Badr", "0910000002", admin_id=amal)
        self.approval = P.request(conn, "admin_setup_code", admin_id=amal, target=self.badr,
                                  details={"reason": "forgot the password"})["approval_id"]
        conn.close()

    def run_cli(self, *argv, typed=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch("builtins.input", return_value=typed):
            status = M.main([str(a) for a in argv])
        return status, out.getvalue(), err.getvalue()

    def test_the_owner_lists_and_approves(self):
        status, out, _ = self.run_cli("approvals", "--db", self.db)
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), [
            f"#{self.approval} a new setup code for Badr (+218910000002); asked by Amal "
            f"(+218910000001) at {out.split(' at ')[1][:16]} UTC: forgot the password "
            "[only the owner can approve it]"])
        status, out, err = self.run_cli("approve", self.approval, "--db", self.db, typed="no")
        self.assertEqual((status, err), (1, "error: not approved\n"))
        status, out, _ = self.run_cli("approve", self.approval, "--db", self.db, typed="yes")
        self.assertEqual(status, 0)
        self.assertRegex(out, r"approved: request #\d+ is done\nsetup code: [A-Z0-9]{4}-")
        status, _, err = self.run_cli("approve", self.approval, "--db", self.db, "--yes")
        self.assertEqual((status, err), (1, f"error: request #{self.approval} is approved\n"))
        self.assertEqual(self.run_cli("approvals", "--db", self.db)[1],
                         "no request is waiting\n")
        status, _, err = self.run_cli("approve", 99, "--db", self.db, "--yes")
        self.assertEqual((status, err), (1, "error: no request #99\n"))


if __name__ == "__main__":
    unittest.main()
