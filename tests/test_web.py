import http.client
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from app import auth as A
from app import ledger as L
from app import orders as O
from app import web


class Client:
    """Calls the WSGI app in-process, keeping the session cookie like a browser."""

    def __init__(self, app, ip="10.0.0.1"):
        self.app, self.ip, self.cookie = app, ip, None

    def request(self, method, path, body=None, *, content_type="application/json",
                raw=None, headers=None):
        path, _, query = path.partition("?")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None
                                            else b"")
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
                   "REMOTE_ADDR": self.ip, "CONTENT_TYPE": content_type,
                   "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data)}
        if self.cookie:
            environ["HTTP_COOKIE"] = f"session={self.cookie}"
        environ.update(headers or {})
        setup_testing_defaults(environ)
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = response_headers

        payload = json.loads(b"".join(self.app(environ, start_response)))
        for name, value in captured["headers"]:
            if name == "Set-Cookie":
                self.cookie = value.split(";")[0].split("=", 1)[1] or None
        self.headers = dict(captured["headers"])
        return captured["status"], payload

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body or {}, **kw)


class WebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "wallet.db")
        conn = A.connect(self.db_path)
        A.create_first_admin(conn, "Admin", "0910000000", "Office-Pass-2026")
        conn.close()
        self.app = web.App(self.db_path)
        self.addCleanup(self.close_connections)
        self.admin = Client(self.app)
        status, _ = self.admin.post("/api/login", {"phone": "0910000000",
                                                   "password": "Office-Pass-2026"})
        self.assertEqual(status, 200)

    def close_connections(self):
        conn = getattr(self.app.local, "conn", None)
        if conn:
            conn.close()

    def new_worker(self, name, phone, password):
        status, made = self.admin.post("/api/admin/users", {"name": name, "phone": phone})
        self.assertEqual(status, 200, made)
        client = Client(self.app, ip=f"10.0.1.{made['id']}")
        status, body = client.post("/api/setup-password", {
            "phone": phone, "code": made["setup_code"], "password": password})
        self.assertEqual(status, 200, body)
        return made["id"], client

    def call(self, environ):
        """The app called with a hand-made environ: (status, JSON answer)."""
        environ = dict({"REQUEST_METHOD": "POST", "PATH_INFO": "/api/login",
                        "REMOTE_ADDR": "10.0.5.5", "CONTENT_TYPE": "application/json"},
                       **environ)
        setup_testing_defaults(environ)
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])
        payload = json.loads(b"".join(self.app(environ, start_response)))
        return captured["status"], payload

    def setup_orders(self, worker_id):
        for path, body in (
                ("/api/admin/deposit", {"user_id": worker_id, "amount": "1000",
                                        "into": "BANK", "idempotency_key": "setup"}),
                ("/api/admin/currencies", {"code": "USD"}),
                ("/api/admin/prices", {"currency": "USD", "rate": "7.85"}),
                ("/api/admin/suppliers", {"name": "Cairo agent"}),
                ("/api/admin/suppliers/1/price", {"currency": "USD", "rate": "7.80"})):
            self.assertEqual(self.admin.post(path, body)[0], 200, path)

    # --- the whole worker journey ---
    def test_worker_journey(self):
        ali_id, ali = self.new_worker("Ali", "091 111 1111", "ali-wallet-77")
        sam_id, sam = self.new_worker("Sam", "0922222222", "sam-wallet-88")
        status, _ = self.admin.post("/api/admin/deposit", {
            "user_id": ali_id, "amount": "100", "into": "BANK", "idempotency_key": "d1"})
        self.assertEqual(status, 200)

        status, me = ali.get("/api/me")
        self.assertEqual((status, me["balance"]["lyd"]), (200, "100.000"))
        status, who = ali.get("/api/recipient?phone=%2B218922222222")
        self.assertEqual((status, who["name"]), (200, "Sam"))
        send = {"to_phone": "0922222222", "amount": "12.5", "idempotency_key": "c-1"}
        first = ali.post("/api/send", send)
        again = ali.post("/api/send", send)
        self.assertEqual(first[1]["txn_id"], again[1]["txn_id"])
        self.assertEqual(sam.get("/api/me")[1]["balance"]["lyd"], "12.500")

        for path, body in (("/api/admin/currencies", {"code": "usd"}),
                           ("/api/admin/prices", {"currency": "USD", "rate": "7.85"}),
                           ("/api/admin/suppliers", {"name": "Cairo agent"})):
            self.assertEqual(self.admin.post(path, body)[0], 200)
        self.admin.post("/api/admin/suppliers/1/price", {"currency": "USD", "rate": "7.80"})
        status, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
        self.assertEqual((status, quote["cost_lyd"]), (200, "78.500"))
        status, order = ali.post("/api/orders", {
            "quote_id": quote["quote_id"], "beneficiary_name": "Ahmed",
            "beneficiary_id": "A1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.admin.post(f"/api/admin/orders/{order['id']}/assign",
                                         {"supplier_id": 1})[0], 200)
        status, _ = self.admin.post("/api/admin/orders/paid", {
            "receipt_code": order["receipt_code"], "payout_ref": "EG-1"})
        self.assertEqual(status, 200)
        status, mine = ali.get("/api/orders")
        self.assertEqual(mine["orders"][0]["status"], "paid")
        self.assertNotIn("supplier_cost", mine["orders"][0])

        status, health = self.admin.get("/api/admin/health")
        self.assertEqual((status, health["problems"]), (200, []))
        self.assertEqual(health["margin"]["lyd"], "0.500")
        status, statement = ali.get("/api/statement?limit=10")
        self.assertEqual([e["kind"] for e in statement["entries"]],
                         ["order", "transfer", "deposit"])

    # --- sessions over HTTP ---
    def test_cookie_settings_and_logout(self):
        client = Client(self.app)
        client.post("/api/login", {"phone": "0910000000", "password": "Office-Pass-2026"})
        cookie = client.headers["Set-Cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(part, cookie)
        self.assertEqual(client.get("/api/admin/users")[0], 200)
        client.post("/api/logout")
        self.assertIsNone(client.cookie)
        self.assertEqual(client.get("/api/admin/users")[0], 401)

    def test_bearer_token_for_phone_apps(self):
        _, body = Client(self.app).post("/api/login", {"phone": "0910000000",
                                                       "password": "Office-Pass-2026"})
        bare = Client(self.app)
        status, _ = bare.get("/api/admin/users",
                             headers={"HTTP_AUTHORIZATION": f"Bearer {body['token']}"})
        self.assertEqual(status, 200)

    def test_status_codes(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.new_worker("Sam", "0922222222", "sam-wallet-88")
        anon = Client(self.app)
        cases = [
            (anon.get("/api/me"), 401),
            (anon.post("/api/login", {"phone": "0911111111", "password": "nope-nope"}), 401),
            (ali.get("/api/admin/users"), 403),
            (self.admin.get("/api/me"), 403),
            (ali.post("/api/send", {"to_phone": "0933333333", "amount": "1",
                                    "idempotency_key": "x"}), 404),
            (ali.post("/api/send", {"to_phone": "0922222222", "amount": "1",
                                    "idempotency_key": "x"}), 409),
            (ali.post("/api/send", {"to_phone": "0910000000", "amount": "1",
                                    "idempotency_key": "x"}), 404),
            (ali.post("/api/send", {"to_phone": "0922222222", "amount": "abc",
                                    "idempotency_key": "x"}), 400),
            (ali.post("/api/send", {"amount": "1"}), 400),
            (anon.get("/api/nothing-here"), 404),
            (anon.get("/api/login"), 405),
            (self.admin.post("/api/admin/deposit", {"user_id": "abc", "amount": "1",
                                                    "into": "BANK"}), 400),
            (self.admin.post("/api/admin/withdraw", {"user_id": ali_id, "amount": "1",
                                                     "from_till": "TILL_NOPE",
                                                     "idempotency_key": "w1"}), 404),
        ]
        for (status, body), expected in cases:
            self.assertEqual(status, expected, body)
            self.assertIn("error", body)

    def test_insufficient_funds_is_a_conflict(self):
        _, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.new_worker("Sam", "0922222222", "sam-wallet-88")
        status, body = ali.post("/api/send", {"to_phone": "0922222222", "amount": "1",
                                              "idempotency_key": "k"})
        self.assertEqual((status, body["type"]), (409, "InsufficientFunds"))

    # --- admin money: idempotency keys and payroll batches ---
    def test_admin_money_routes_need_a_real_idempotency_key(self):
        ali_id, _ = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.assertEqual(self.admin.post("/api/admin/tills", {"name": "main"})[0], 200)
        self.assertEqual(self.admin.post("/api/admin/suppliers", {"name": "Cairo"})[0], 200)
        routes = [
            ("/api/admin/deposit", {"user_id": ali_id, "amount": "100", "into": "TILL_MAIN"}),
            ("/api/admin/payroll", {"items": [{"user_id": ali_id, "amount": "500"}],
                                    "batch_ref": "OCT-2026"}),
            ("/api/admin/withdraw", {"user_id": ali_id, "amount": "50",
                                     "from_till": "TILL_MAIN"}),
            ("/api/admin/move", {"from": "BANK", "to": "TILL_MAIN", "amount": "20"}),
            ("/api/admin/suppliers/1/pay", {"amount": "10", "from": "BANK",
                                            "reference": "W-1"}),
        ]
        txns = {}
        for path, body in routes:
            for key in (None, "", "  ", 5, False):
                sent = body if key is None else dict(body, idempotency_key=key)
                status, answer = self.admin.post(path, sent)
                self.assertEqual(status, 400, (path, key, answer))
            first = self.admin.post(path, dict(body, idempotency_key=f"k-{path}"))
            again = self.admin.post(path, dict(body, idempotency_key=f"k-{path}"))
            self.assertEqual(first[0], 200, first)
            self.assertEqual(first, again)
            txns[path] = first[1]["txn_id"]
        status, statement = self.admin.get(f"/api/admin/users/{ali_id}/statement")
        self.assertEqual(statement["balance"]["lyd"], "550.000")
        self.assertEqual(len(set(txns.values())), 5)

        move = {"txn_id": txns["/api/admin/move"], "reason": "not moved"}
        self.assertEqual(self.admin.post("/api/admin/reverse",
                                         dict(move, idempotency_key=" "))[0], 400)
        self.assertEqual(self.admin.post("/api/admin/reverse", move)[0], 200)
        self.assertEqual(self.admin.get("/api/admin/health")[1]["problems"], [])

    def test_a_payroll_batch_is_posted_once_whoever_sends_it(self):
        ali_id, _ = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        status, made = self.admin.post("/api/admin/users", {
            "name": "Boss", "phone": "0930000000", "role": "admin"})
        boss = Client(self.app, ip="10.0.2.1")
        status, _ = boss.post("/api/setup-password", {
            "phone": "0930000000", "code": made["setup_code"], "password": "Boss-Pass-2026"})
        self.assertEqual(status, 200)
        batch = {"items": [{"user_id": ali_id, "amount": "950"}], "batch_ref": "OCT-2026"}
        self.assertEqual(self.admin.post("/api/admin/payroll",
                                         dict(batch, idempotency_key="p1"))[0], 200)
        for client, key in ((boss, "p1"), (self.admin, "p2")):
            status, body = client.post("/api/admin/payroll", dict(batch, idempotency_key=key))
            self.assertEqual((status, body["type"]), (409, "Conflict"))
        status, statement = self.admin.get(f"/api/admin/users/{ali_id}/statement")
        self.assertEqual(statement["balance"]["lyd"], "950.000")

    # --- admin setup: currencies, tills, suppliers ---
    def test_duplicate_or_bad_setup_is_refused_clearly(self):
        cases = [
            (("/api/admin/currencies", {"code": "usd"}), 200),
            (("/api/admin/currencies", {"code": "USD"}), 409),
            (("/api/admin/currencies", {"code": "EURO"}), 400),
            (("/api/admin/currencies", {"code": 123}), 400),
            (("/api/admin/tills", {"name": "MAIN"}), 200),
            (("/api/admin/tills", {"name": "main"}), 409),
            (("/api/admin/suppliers", {"name": "Cairo agent"}), 200),
            (("/api/admin/suppliers", {"name": "cairo agent "}), 409),
            (("/api/admin/prices", {"currency": "usd", "rate": "7.85"}), 200),   # any case
            (("/api/admin/suppliers/1/price", {"currency": "Usd", "rate": "7.80"}), 200),
            (("/api/admin/prices", {"currency": "EURO", "rate": "1"}), 400),
        ]
        for (path, body), expected in cases:
            status, answer = self.admin.post(path, body)
            self.assertEqual(status, expected, (path, body, answer))

    def test_admin_can_void_quotes_and_switch_a_currency_off(self):
        _, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.admin.post("/api/admin/currencies", {"code": "USD"})
        self.admin.post("/api/admin/prices", {"currency": "USD", "rate": "0.785"})
        status, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
        self.assertEqual(status, 200)
        self.assertEqual(self.admin.post("/api/admin/prices", {
            "currency": "USD", "rate": "7.85", "void_quotes": "yes"})[0], 400)
        self.assertEqual(self.admin.post("/api/admin/prices", {
            "currency": "USD", "rate": "7.85", "void_quotes": True})[0], 200)
        status, body = ali.post("/api/orders", {"quote_id": quote["quote_id"],
                                                "beneficiary_name": "Ahmed",
                                                "beneficiary_id": "A1"})
        self.assertEqual((status, body["type"]), (409, "QuoteExpired"))

        switch = {"code": "usd", "active": False, "reason": "checking the rate"}
        for bad in ({"active": "no"}, {"reason": ""}, {"code": "GBP"}):
            self.assertEqual(self.admin.post("/api/admin/currencies/active",
                                             dict(switch, **bad))[0], 400)
        self.assertEqual(ali.post("/api/admin/currencies/active", switch)[0], 403)
        self.assertEqual(self.admin.post("/api/admin/currencies/active", switch)[0], 200)
        self.assertEqual(ali.get("/api/prices")[1], {"prices": []})
        self.assertEqual(ali.post("/api/quote", {"currency": "USD", "amount": "10"})[0], 400)
        self.assertEqual(self.admin.post("/api/admin/currencies/active",
                                         dict(switch, active=True))[0], 200)
        self.assertEqual(ali.post("/api/quote", {"currency": "USD", "amount": "10"})[0], 200)

    # --- orders: replays, stale screens, blocked workers ---
    def test_order_conflicts_and_blocked_workers_over_http(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        for path, body in (
                ("/api/admin/deposit", {"user_id": ali_id, "amount": "1000", "into": "BANK",
                                        "idempotency_key": "d1"}),
                ("/api/admin/currencies", {"code": "USD"}),
                ("/api/admin/prices", {"currency": "USD", "rate": "7.85"}),
                ("/api/admin/suppliers", {"name": "Cairo agent"}),
                ("/api/admin/suppliers", {"name": "Tunis agent"}),
                ("/api/admin/suppliers/1/price", {"currency": "USD", "rate": "7.80"}),
                ("/api/admin/suppliers/2/price", {"currency": "USD", "rate": "7.82"})):
            self.assertEqual(self.admin.post(path, body)[0], 200, path)

        def place(**details):
            _, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
            return dict({"quote_id": quote["quote_id"], "beneficiary_name": "Ahmed",
                         "beneficiary_id": "A1"}, **details)

        body = place()
        status, first = ali.post("/api/orders", body)
        self.assertEqual(ali.post("/api/orders", body)[1]["receipt_code"],
                         first["receipt_code"])
        status, answer = ali.post("/api/orders", dict(body, beneficiary_id="A2"))
        self.assertEqual((status, answer["type"]), (409, "Conflict"))

        assign = f"/api/admin/orders/{first['id']}/assign"
        self.assertEqual(self.admin.post(assign, {"supplier_id": 1})[0], 200)
        self.assertEqual(self.admin.post(assign, {"supplier_id": 2})[0], 409)
        self.assertEqual(self.admin.post(f"/api/admin/orders/{first['id']}/unassign",
                                         {"reason": "Cairo has not paid"})[0], 200)
        self.assertEqual(self.admin.post(assign, {"supplier_id": 2})[0], 200)
        paid = {"receipt_code": first["receipt_code"], "payout_ref": "CAIRO-77"}
        status, answer = self.admin.post("/api/admin/orders/paid", dict(paid, supplier_id=1))
        self.assertEqual((status, answer["type"]), (409, "Conflict"))
        self.assertEqual(self.admin.post("/api/admin/orders/paid",
                                         dict(paid, supplier_id="x"))[0], 400)
        self.assertEqual(self.admin.post("/api/admin/orders/paid", dict(
            paid, payout_ref="TUNIS-1", supplier_id=2))[0], 200)

        status, held = ali.post("/api/orders", place())
        self.assertEqual(self.admin.post(f"/api/admin/users/{ali_id}/block",
                                         {"reason": "suspected fraud"})[0], 200)
        status, queue = self.admin.get("/api/admin/orders?status=placed")
        self.assertEqual([(o["id"], o["customer_blocked"]) for o in queue["orders"]],
                         [(held["id"], 1)])
        status, answer = self.admin.post(f"/api/admin/orders/{held['id']}/assign",
                                         {"supplier_id": 1})
        self.assertEqual((status, answer["type"]), (403, "AccountBlocked"))
        self.assertEqual(self.admin.post(f"/api/admin/orders/{held['id']}/cancel",
                                         {"reason": "worker blocked"})[0], 200)
        self.assertEqual(self.admin.get("/api/admin/health")[1]["problems"], [])

    def test_margin_dates_in_the_usual_forms(self):
        for query in ("since=2026-10-01T00:00:00+00:00",       # '+' not URL-encoded
                      "since=2026-10-01T00:00:00%2B02:00", "since=2026-10-01T00:00:00Z",
                      "since=2026-10-01&until=2026-11-01", "until=2026-10-01T00:00-01:00"):
            status, body = self.admin.get(f"/api/admin/margin?{query}")
            self.assertEqual((status, body), (200, {"rows": []}), query)
        status, body = self.admin.get("/api/admin/margin?since=October")
        self.assertEqual(status, 400)
        self.assertIn("2026-10-01T00:00:00Z", body["error"])

    # --- request hygiene ---
    def test_posts_must_be_json(self):
        status, _ = self.admin.post("/api/admin/currencies", content_type="text/plain",
                                    raw=b'{"code": "USD"}')
        self.assertEqual(status, 415)
        status, _ = self.admin.post(
            "/api/admin/currencies", content_type="application/x-www-form-urlencoded",
            raw=b"code=USD")
        self.assertEqual(status, 415)
        for raw in (b"{not json", b"[1, 2]"):
            self.assertEqual(self.admin.post("/api/admin/currencies", raw=raw)[0], 400)
        big = json.dumps({"code": "x" * (web.MAX_BODY_BYTES + 1)}).encode()
        self.assertEqual(self.admin.post("/api/admin/currencies", raw=big)[0], 413)

    def test_security_headers(self):
        self.admin.get("/api/admin/users")
        for name in ("Cache-Control", "X-Content-Type-Options", "X-Frame-Options",
                     "Content-Security-Policy"):
            self.assertIn(name, self.admin.headers)

    def test_unexpected_errors_reveal_nothing(self):
        def boom(app, conn, req):
            raise RuntimeError("secret detail")
        web.ROUTES.insert(0, ("GET", __import__("re").compile("^/api/boom$"), boom))
        self.addCleanup(web.ROUTES.pop, 0)
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            status, body = self.admin.get("/api/boom")
        self.assertEqual((status, body), (500, {"error": "internal error",
                                                "type": "internal"}))
        self.assertIn("secret detail", stderr.getvalue())

    # --- login limits per address ---
    def test_login_tries_are_limited_per_address(self):
        attacker = Client(self.app, ip="203.0.113.9")
        for i in range(web.LOGIN_TRIES_PER_IP):
            status, _ = attacker.post("/api/login", {"phone": f"09{i % 5 + 1}1234567",
                                                     "password": "guess-guess"})
            self.assertEqual(status, 401)
        status, _ = attacker.post("/api/login", {"phone": "0910000000",
                                                 "password": "Office-Pass-2026"})
        self.assertEqual(status, 429)
        status, _ = Client(self.app, ip="198.51.100.7").post(
            "/api/login", {"phone": "0910000000", "password": "Office-Pass-2026"})
        self.assertEqual(status, 200)

    def test_registered_and_unknown_phones_answer_alike(self):
        self.new_worker("Ali", "0911111111", "ali-wallet-77")
        answers = {}
        for phone, ip in (("0911111111", "203.0.113.1"), ("0933333333", "203.0.113.2")):
            client = Client(self.app, ip=ip)
            answers[phone] = [client.post("/api/login", {"phone": phone,
                                                         "password": "guess-guess-1"})
                              for _ in range(A.MAX_FAILED_LOGINS + 1)]
        self.assertEqual(answers["0911111111"], answers["0933333333"])
        self.assertEqual({status for status, _ in answers["0911111111"]}, {401})
        locked = Client(self.app, ip="198.51.100.7").post(
            "/api/login", {"phone": "0911111111", "password": "ali-wallet-77"})
        self.assertEqual(locked, answers["0933333333"][-1])

    def test_admin_unlocks_a_locked_worker(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        attacker = Client(self.app, ip="203.0.113.1")
        for _ in range(A.MAX_FAILED_LOGINS):
            attacker.post("/api/login", {"phone": "0911111111", "password": "guess-guess-1"})
        login = {"phone": "0911111111", "password": "ali-wallet-77"}
        self.assertEqual(Client(self.app).post("/api/login", login)[0], 401)
        path = f"/api/admin/users/{ali_id}/unlock"
        self.assertEqual(ali.post(path, {"reason": "it's me"})[0], 403)
        self.assertEqual(self.admin.post(path, {})[0], 400)
        self.assertEqual(self.admin.post(path, {"reason": "someone kept guessing"})[0], 200)
        self.assertEqual(Client(self.app).post("/api/login", login)[0], 200)

    def test_change_password_tries_are_limited(self):
        _, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        wrong = {"old_password": "guess-guess-1", "new_password": "thief-pass-1"}
        status, body = ali.post("/api/change-password", wrong)
        self.assertEqual((status, body["type"]), (403, "WrongPassword"))
        self.assertEqual(ali.get("/api/me")[0], 200)        # a typo doesn't log out
        for _ in range(A.MAX_FAILED_LOGINS - 1):
            ali.post("/api/change-password", wrong)
        status, body = ali.post("/api/change-password", {"old_password": "ali-wallet-77",
                                                         "new_password": "thief-pass-1"})
        self.assertEqual((status, body["type"]), (429, "AccountLocked"))
        for _ in range(web.LOGIN_TRIES_PER_IP):         # and counted per address
            status, body = ali.post("/api/change-password", wrong)
        self.assertEqual((status, body["type"]), (429, "http"))

    def test_setup_code_for_an_admin_needs_a_reason(self):
        status, made = self.admin.post("/api/admin/users", {
            "name": "Boss", "phone": "0930000000", "role": "admin"})
        self.assertEqual(status, 200)
        self.assertRegex(made["setup_code"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        path = f"/api/admin/users/{made['id']}/setup-code"
        self.assertEqual(self.admin.post(path, {})[0], 400)
        status, body = self.admin.post(path, {"reason": "Boss forgot the password"})
        self.assertEqual(status, 200, body)

    def test_rate_limiter_window(self):
        now = [0.0]
        limiter = web.RateLimiter(2, 60, clock=lambda: now[0])
        limiter.check("ip")
        limiter.check("ip")
        with self.assertRaises(web.HttpError):
            limiter.check("ip")
        now[0] = 61
        limiter.check("ip")

    def test_forwarded_address_used_only_when_trusted(self):
        headers = {"HTTP_X_FORWARDED_FOR": "1.2.3.4, 203.0.113.50"}
        environ = {"REQUEST_METHOD": "GET", "REMOTE_ADDR": "127.0.0.1", **headers}
        self.assertEqual(web.Request(environ, trust_proxy=False).ip, "127.0.0.1")
        self.assertEqual(web.Request(environ, trust_proxy=True).ip, "203.0.113.50")

    # --- admin user management ---
    def test_admin_blocks_and_worker_is_logged_out(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        status, _ = self.admin.post(f"/api/admin/users/{ali_id}/block", {"reason": "doubt"})
        self.assertEqual(status, 200)
        self.assertEqual(ali.get("/api/me")[0], 401)
        status, body = Client(self.app).post("/api/login", {"phone": "0911111111",
                                                            "password": "ali-wallet-77"})
        self.assertEqual((status, body["type"]), (403, "AccountBlocked"))
        status, users = self.admin.get("/api/admin/users")
        ali_row = next(u for u in users["users"] if u["id"] == ali_id)
        self.assertEqual((ali_row["blocked"], ali_row["blocked_reason"]), (1, "doubt"))
        self.assertEqual(self.admin.post(f"/api/admin/users/{ali_id}/block", {})[0], 400)

    def test_blocked_or_closed_colleague_looks_like_an_unknown_number(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        sam_id, _ = self.new_worker("Sam", "0922222222", "sam-wallet-88")
        omar_id, _ = self.new_worker("Omar", "0933333333", "omar-wallet-99")
        self.admin.post("/api/admin/deposit", {"user_id": ali_id, "amount": "10",
                                               "into": "BANK", "idempotency_key": "d1"})
        self.assertEqual(self.admin.post(f"/api/admin/users/{sam_id}/block",
                                         {"reason": "suspected fraud"})[0], 200)
        self.assertEqual(self.admin.post(f"/api/admin/users/{omar_id}/deactivate",
                                         {"reason": "left"})[0], 200)
        unknown = ali.get("/api/recipient?phone=0944444444")
        self.assertEqual(unknown, (404, {"error": "no worker with that phone number",
                                         "type": "UnknownAccount"}))
        for phone in ("0922222222", "0933333333"):
            self.assertEqual(ali.get(f"/api/recipient?phone={phone}"), unknown, phone)
            self.assertEqual(ali.post("/api/send", {"to_phone": phone, "amount": "1",
                                                    "idempotency_key": phone}), unknown)

        # Blocked between the lookup and the transfer: still the same answer,
        # not "user 3 is blocked".
        real = L.find_user_by_phone
        looked_up = []

        def stale(conn, phone):
            row = real(conn, phone)
            if not looked_up:
                row = dict(row, blocked=0)
            looked_up.append(phone)
            return row
        with unittest.mock.patch.object(L, "find_user_by_phone", stale):
            self.assertEqual(ali.post("/api/send", {"to_phone": "0922222222", "amount": "1",
                                                    "idempotency_key": "race"}), unknown)
        self.assertEqual(ali.get("/api/me")[1]["balance"]["lyd"], "10.000")

    # --- wrong types, odd input and limits never crash the server ---
    def test_wrong_json_types_are_refused_not_crashed(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.new_worker("Sam", "0922222222", "sam-wallet-88")
        self.setup_orders(ali_id)
        _, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
        good_order = {"quote_id": quote["quote_id"], "beneficiary_name": "Ahmed",
                      "beneficiary_id": "A1"}
        _, order = ali.post("/api/orders", good_order)
        send = {"to_phone": "0922222222", "amount": "1", "idempotency_key": "s1"}
        payroll = {"batch_ref": "OCT", "idempotency_key": "p1"}
        anon = Client(self.app, ip="10.0.9.9")
        admin = self.admin
        cases = [
            (ali, "/api/send", dict(send, note={"a": 1}), 400),
            (ali, "/api/send", dict(send, amount=1.5), 400),     # a fraction may not be exact
            (ali, "/api/send", dict(send, to_phone=922222222), 400),
            (ali, "/api/quote", {"currency": 840, "amount": "10"}, 400),
            (ali, "/api/quote", {"currency": "USD", "amount": [10]}, 400),
            (ali, "/api/orders", dict(good_order, beneficiary_id=119900123456), 400),
            (ali, "/api/orders", dict(good_order, beneficiary_name=["Ahmed"]), 400),
            (ali, "/api/orders", dict(good_order, beneficiary_name="\u200f"), 400),
            (ali, "/api/orders", dict(good_order, quote_id=[quote["quote_id"]]), 400),
            (ali, "/api/orders", dict(good_order, quote_id={"id": 1}), 400),
            (anon, "/api/login", {"phone": 911111111, "password": "ali-wallet-77"}, 400),
            (anon, "/api/setup-password", {"phone": "0911111111", "code": 23456789,
                                           "password": "new-wallet-99"}, 400),
            (admin, f"/api/admin/users/{ali_id}/block", {"reason": 5}, 400),
            (admin, "/api/admin/users", {"name": 5, "phone": "0944444444"}, 400),
            (admin, "/api/admin/users", {"name": "Omar", "phone": "0944444444",
                                         "whatsapp": 201001234567}, 400),
            (admin, "/api/admin/orders/paid", {"receipt_code": order["receipt_code"],
                                               "payout_ref": 778812}, 400),
            (admin, "/api/admin/suppliers/1/pay", {"amount": "1", "from": "BANK",
                                                   "reference": 1234,
                                                   "idempotency_key": "k1"}, 400),
            (admin, "/api/admin/move", {"from": ["BANK"], "to": "TILL_X", "amount": "1",
                                        "idempotency_key": "m1"}, 400),
            (admin, "/api/admin/prices", {"currency": ["USD"], "rate": "7.9"}, 400),
            (admin, "/api/admin/suppliers", {"name": "Tunis", "country": ["TN"]}, 400),
            (admin, "/api/admin/deposit", {"user_id": 2**64, "amount": "1", "into": "BANK",
                                           "idempotency_key": "d2"}, 400),
            (admin, "/api/admin/payroll", dict(payroll, items=[{"user_id": ali_id + 0.9,
                                                                "amount": "1"}]), 400),
            (admin, "/api/admin/payroll", dict(payroll, items=[{"user_id": True,
                                                                "amount": "1"}]), 400),
            (admin, "/api/admin/payroll", dict(payroll, items=["x"]), 400),
            (admin, "/api/admin/payroll", dict(payroll, items=[{"user_id": ali_id,
                                                                "amount": 0.1}]), 400),
            (admin, "/api/admin/reconcile", {"counts": {"BANK": [1]}}, 400),
            (admin, "/api/admin/orders/99999999999999999999/assign", {"supplier_id": 1}, 404),
        ]
        for client, path, body, expected in cases:
            status, answer = client.post(path, body)
            self.assertEqual(status, expected, (path, body, answer))
            self.assertNotEqual(answer["type"], "internal")
        status, answer = admin.get("/api/admin/margin?until=9999-12-31T23:59:59-01:00")
        self.assertEqual(status, 400, answer)

        # Text that is not valid Unicode (a lone surrogate) and JSON nested
        # too deep for the parser are refused like any other bad body.
        _, code = admin.post(f"/api/admin/users/{ali_id}/setup-code", {})
        for raw in (b'{"phone": "0911111111", "code": "%s", "password": "\\ud800-wallet-77"}'
                    % code["setup_code"].encode(),
                    b'{"phone": "0911111111", "code": "\\udfff", "password": "ali-wallet-78"}',
                    b'{"a": ' + b"[" * 30000 + b"]" * 30000 + b"}"):
            status, answer = anon.post("/api/setup-password", raw=raw)
            self.assertEqual(status, 400, answer)
        status, statement = admin.get(f"/api/admin/users/{ali_id}/statement")
        self.assertEqual(statement["balance"]["lyd"], "921.500")     # nothing credited

    def test_content_length_must_be_a_plain_size(self):
        body = json.dumps({"phone": "0910000000", "password": "Office-Pass-2026",
                           "padding": "x" * web.MAX_BODY_BYTES}).encode()
        for length in ("-1", "-100", "1e3", "+5", "0x10", "١٢"):
            stream = io.BytesIO(body)
            status, answer = self.call({"CONTENT_LENGTH": length, "wsgi.input": stream})
            self.assertEqual((status, answer["error"]), (400, "bad Content-Length"), length)
            self.assertEqual(stream.tell(), 0, length)          # nothing was read
        stream = io.BytesIO(body)                               # no length: no body
        status, answer = self.call({"wsgi.input": stream})
        self.assertEqual((status, answer["error"]), (400, "'phone' is required"))
        self.assertEqual(stream.tell(), 0)
        status, _ = self.call({"CONTENT_LENGTH": "9" * 5000, "wsgi.input": io.BytesIO()})
        self.assertEqual(status, 413)

    def test_text_fields_have_a_length_limit(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.new_worker("Sam", "0922222222", "sam-wallet-88")
        self.setup_orders(ali_id)
        send = {"to_phone": "0922222222", "amount": "0.001"}
        status, answer = ali.post("/api/send", dict(send, idempotency_key="n1",
                                                    note="x" * 201))
        self.assertEqual(status, 400)
        self.assertIn("'note' is too long", answer["error"])
        self.assertEqual(ali.post("/api/send", dict(send, idempotency_key="n2",
                                                    note="x" * 200))[0], 200)
        self.assertEqual(ali.post("/api/send", dict(send, idempotency_key="k" * 201))[0], 400)
        _, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
        order = {"quote_id": quote["quote_id"], "beneficiary_name": "A" * 101,
                 "beneficiary_id": "A1"}
        self.assertEqual(ali.post("/api/orders", order)[0], 400)
        self.assertEqual(ali.post("/api/orders", dict(order, beneficiary_name="Ahmed",
                                                      beneficiary_id="1" * 101))[0], 400)
        status, placed = ali.post("/api/orders", dict(order, beneficiary_name="A" * 100))
        self.assertEqual(status, 200, placed)
        for path, body in (
                ("/api/admin/users", {"name": "N" * 101, "phone": "0944444444"}),
                (f"/api/admin/users/{ali_id}/block", {"reason": "r" * 501}),
                ("/api/admin/suppliers", {"name": "S" * 101}),
                ("/api/admin/suppliers", {"name": "Tunis", "country": "C" * 101}),
                ("/api/admin/payroll", {"items": [{"user_id": ali_id, "amount": "1"}],
                                        "batch_ref": "B" * 101, "idempotency_key": "p"}),
                ("/api/admin/orders/paid", {"receipt_code": placed["receipt_code"],
                                            "payout_ref": "P" * 101})):
            status, answer = self.admin.post(path, body)
            self.assertEqual(status, 400, (path, answer))
            self.assertIn("is too long", answer["error"])
        self.assertEqual(self.admin.post(f"/api/admin/users/{ali_id}/block",
                                         {"reason": "r" * 500})[0], 200)

    def test_worker_money_and_order_requests_are_limited_per_worker(self):
        _, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        _, sam = self.new_worker("Sam", "0922222222", "sam-wallet-88")
        nothing = {"currency": "XYZ", "amount": "1"}
        for _ in range(web.WORKER_REQUESTS_PER_MINUTE):
            self.assertEqual(ali.post("/api/quote", nothing)[0], 400)
        for status, answer in (
                ali.post("/api/quote", nothing),
                ali.post("/api/send", {"to_phone": "0922222222", "amount": "1",
                                       "idempotency_key": "s"}),
                ali.post("/api/orders", {"quote_id": "q", "beneficiary_name": "A",
                                         "beneficiary_id": "1"}),
                ali.get("/api/recipient?phone=0922222222")):
            self.assertEqual((status, answer["type"]), (429, "http"))
        self.assertEqual(ali.get("/api/me")[0], 200)                # reading isn't limited
        self.assertEqual(sam.post("/api/quote", nothing)[0], 400)   # nor other workers
        later = time.monotonic() + 61
        with unittest.mock.patch.object(self.app.worker_limiter, "clock", lambda: later):
            self.assertEqual(ali.post("/api/quote", nothing)[0], 400)

    def test_blank_optional_fields_count_as_left_out(self):
        status, made = self.admin.post("/api/admin/users", {
            "name": "Ali", "phone": "0911111111", "whatsapp": "", "role": "",
            "language": " ", "reason": ""})
        self.assertEqual(status, 200, made)
        ali_id = made["id"]
        ali = Client(self.app, ip="10.0.1.50")
        self.assertEqual(ali.post("/api/setup-password", {
            "phone": "0911111111", "code": made["setup_code"],
            "password": "ali-wallet-77"})[0], 200)

        def whatsapp():
            users = self.admin.get("/api/admin/users")[1]["users"]
            return next(u["whatsapp"] for u in users if u["id"] == ali_id)
        self.assertIsNone(whatsapp())
        path = f"/api/admin/users/{ali_id}/whatsapp"
        self.assertEqual(self.admin.post(path, {"whatsapp": "+20 100 123 4567"})[0], 200)
        self.assertEqual(self.admin.post(path, {})[0], 400)         # left out: unchanged
        self.assertEqual(whatsapp(), "+201001234567")
        for blank in ("", None):                                    # blank or null: removed
            self.assertEqual(self.admin.post(path, {"whatsapp": blank})[0], 200)
            self.assertIsNone(whatsapp())
            self.admin.post(path, {"whatsapp": "+20 100 123 4567"})

        self.setup_orders(ali_id)
        self.assertEqual(self.admin.post("/api/admin/suppliers",
                                         {"name": "Tunis agent", "country": ""})[0], 200)
        suppliers = self.admin.get("/api/admin/suppliers")[1]["suppliers"]
        self.assertIsNone(suppliers[1]["country"])
        _, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
        _, order = ali.post("/api/orders", {"quote_id": quote["quote_id"],
                                            "beneficiary_name": "Ahmed",
                                            "beneficiary_id": "A1"})
        status, answer = self.admin.post(f"/api/admin/orders/{order['id']}/assign",
                                         {"supplier_id": 1, "rate": ""})
        self.assertEqual(status, 200, answer)
        orders = self.admin.get("/api/admin/orders")[1]["orders"]
        self.assertEqual(orders[0]["supplier_rate"], "7.80")       # the agreed rate
        self.assertEqual(self.admin.post("/api/admin/orders/paid", {
            "receipt_code": order["receipt_code"], "payout_ref": "EG-1",
            "supplier_id": ""})[0], 200)
        self.assertEqual(self.admin.post("/api/admin/tills", {"name": "main"})[0], 200)
        self.admin.post("/api/admin/move", {"from": "BANK", "to": "TILL_MAIN", "amount": "50",
                                            "idempotency_key": "m"})
        status, answer = self.admin.post("/api/admin/withdraw", {
            "user_id": ali_id, "amount": "10", "fee": "", "from_till": "TILL_MAIN",
            "note": " ", "idempotency_key": "w"})
        self.assertEqual(status, 200, answer)
        self.assertEqual(ali.get("/api/me")[1]["balance"]["lyd"], "911.500")

    # --- login limits: only failures count, memory stays small ---
    def test_only_failed_logins_count_against_an_address(self):
        office = Client(self.app, ip="198.51.100.20")
        login = {"phone": "0910000000", "password": "Office-Pass-2026"}
        for _ in range(web.LOGIN_TRIES_PER_IP + 5):         # a whole office logging in
            self.assertEqual(office.post("/api/login", login)[0], 200)
        for _ in range(web.LOGIN_TRIES_PER_IP + 5):         # refused before any check
            self.assertEqual(office.post("/api/login", {})[0], 400)
        self.assertEqual(office.post("/api/login", login)[0], 200)
        for i in range(web.LOGIN_TRIES_PER_IP):
            self.assertEqual(office.post("/api/login", {"phone": f"09{i % 5 + 1}1234567",
                                                        "password": "guess-guess"})[0], 401)
        self.assertEqual(office.post("/api/login", login)[0], 429)

    def test_ipv6_addresses_count_per_64_block(self):
        login = {"phone": "0910000000", "password": "Office-Pass-2026"}
        for i in range(web.LOGIN_TRIES_PER_IP):
            status, _ = Client(self.app, ip=f"2001:db8:5:6::{i + 1:x}").post(
                "/api/login", {"phone": f"09{i % 5 + 1}1234567", "password": "guess-guess"})
            self.assertEqual(status, 401)
        self.assertEqual(Client(self.app, ip="2001:db8:5:6:abcd::1").post(
            "/api/login", login)[0], 429)
        self.assertEqual(Client(self.app, ip="2001:db8:5:7::1").post(
            "/api/login", login)[0], 200)
        self.assertEqual(web.client_key("::ffff:203.0.113.9"), "203.0.113.9")
        self.assertEqual(web.client_key("203.0.113.9"), "203.0.113.9")
        self.assertEqual(web.client_key("not an address"), "not an address")

    def test_rate_limiter_forgets_old_keys_and_stays_small(self):
        now = [0.0]
        limiter = web.RateLimiter(2, 60, clock=lambda: now[0], max_keys=100)
        for i in range(100):
            limiter.check(f"10.0.0.{i}")
        limiter.check("one more")                  # the least recently seen makes room
        self.assertEqual(len(limiter.hits), 100)
        self.assertNotIn("10.0.0.0", limiter.hits)
        now[0] = 61
        limiter.check("10.9.9.9")                  # everything older than the window goes
        self.assertEqual(list(limiter.hits), ["10.9.9.9"])
        limiter.forgive("10.9.9.9", 61)            # a try that went fine doesn't count
        self.assertEqual(list(limiter.hits), [])

    def test_rate_limit_docs_match_how_it_works(self):
        self.assertIn('make_app("wallet.db", trust_proxy=True)', web.__doc__)
        with open(os.path.join(os.path.dirname(__file__), "..", "README.md"),
                  encoding="utf-8") as f:
            readme = f.read()
        self.assertTrue("per server process" in readme, "README: limits are per process")

    # --- cookies and the bundled server ---
    def test_session_cookie_is_found_among_other_cookies(self):
        _, body = Client(self.app).post("/api/login", {"phone": "0910000000",
                                                       "password": "Office-Pass-2026"})
        token = body["token"]
        bare = Client(self.app)
        for header in (f'prefs={{"lang":"ar"}}; theme=dark mode; session={token}',
                       f"version=1; session={token}", f"path=/x; session={token}",
                       f"x; session={token}", f"x=café; session={token}",
                       f'x="unterminated; session={token}', f'session="{token}"',
                       f"session=; session={token}"):
            status, answer = bare.get("/api/admin/users", headers={"HTTP_COOKIE": header})
            self.assertEqual(status, 200, (header, answer))
        for header in (f"xsession={token}", "session=wrong", ""):
            status, _ = bare.get("/api/admin/users", headers={"HTTP_COOKIE": header})
            self.assertEqual(status, 401, header)

    def test_bundled_server_keeps_database_connections(self):
        server = web.dev_server(self.app, "127.0.0.1", 0)
        serving = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        serving.start()

        def stop():
            server.shutdown()
            server.server_close()
            for worker in server.workers:
                worker.join(5)
        self.addCleanup(stop)
        with unittest.mock.patch.object(web.A, "connect", wraps=A.connect) as opened, \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            for _ in range(3 * web.DEV_SERVER_THREADS):
                http_conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                                       timeout=10)
                http_conn.request("GET", "/api/me")
                response = http_conn.getresponse()
                response.read()
                http_conn.close()
                self.assertEqual(response.status, 401)
        self.assertLessEqual(opened.call_count, web.DEV_SERVER_THREADS)

    def test_login_with_arabic_indic_digits(self):
        status, _ = Client(self.app).post("/api/login", {"phone": "٠٩١٠٠٠٠٠٠٠",
                                                         "password": "Office-Pass-2026"})
        self.assertEqual(status, 200)

    # --- helpers for the admin, owner-rule and race tests below ---
    def new_admin(self, name, phone, password):
        status, made = self.admin.post("/api/admin/users", {
            "name": name, "phone": phone, "role": "admin", "reason": "new admin"})
        self.assertEqual(status, 200, made)
        client = Client(self.app, ip=f"10.0.2.{made['id']}")
        status, body = client.post("/api/setup-password", {
            "phone": phone, "code": made["setup_code"], "password": password})
        self.assertEqual(status, 200, body)
        return made["id"], client

    def books(self):
        """Every table an admin action could change (not sessions or login
        tries), read on a connection of its own."""
        conn = A.connect(self.db_path)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT IN ('sessions', 'login_attempts') ORDER BY name")]
            return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")]
                    for t in tables}
        finally:
            conn.close()

    def lyd(self, client=None, user_id=None):
        """A worker's balance in LYD text: their own view, or the admin's."""
        if client is not None:
            return client.get("/api/me")[1]["balance"]["lyd"]
        return self.admin.get(f"/api/admin/users/{user_id}/statement")[1]["balance"]["lyd"]

    def place(self, worker, beneficiary_id="A1", amount="10"):
        status, quote = worker.post("/api/quote", {"currency": "USD", "amount": amount})
        self.assertEqual(status, 200, quote)
        status, order = worker.post("/api/orders", {
            "quote_id": quote["quote_id"], "beneficiary_name": "Ahmed",
            "beneficiary_id": beneficiary_id})
        self.assertEqual(status, 200, order)
        return order

    def admin_fixture(self):
        """A worker with money and a placed order, a till and a supplier with a
        price: enough for every request of admin_requests to make sense."""
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.setup_orders(ali_id)
        self.assertEqual(self.admin.post("/api/admin/tills", {"name": "main"})[0], 200)
        return ali_id, ali, self.place(ali)

    def admin_requests(self, ali_id, order):
        """One request for every admin route, keyed by the route as web.ROUTES
        spells it, so a new route can't be left out. Every body claims to come
        from admin 1, which must never count."""
        deposit = next(e["txn_id"] for e in self.admin.get(
            f"/api/admin/users/{ali_id}/statement")[1]["entries"] if e["kind"] == "deposit")
        user, sup, o = (f"/api/admin/users/{ali_id}", "/api/admin/suppliers/1",
                        f"/api/admin/orders/{order['id']}")
        key = {"idempotency_key": "only-admins-1"}
        reason = {"reason": "checking"}
        requests = {
            "GET /api/admin/users": "/api/admin/users",
            "POST /api/admin/users": ("/api/admin/users", {
                "name": "Omar", "phone": "0944444444", "role": "admin", **reason}),
            "POST /api/admin/users/<id>/setup-code": (f"{user}/setup-code", reason),
            "POST /api/admin/users/<id>/whatsapp": (f"{user}/whatsapp",
                                                   {"whatsapp": "+201001234567"}),
            "POST /api/admin/users/<id>/block": (f"{user}/block", reason),
            "POST /api/admin/users/<id>/unblock": (f"{user}/unblock", reason),
            "POST /api/admin/users/<id>/deactivate": (f"{user}/deactivate", reason),
            "POST /api/admin/users/<id>/reactivate": (f"{user}/reactivate", reason),
            "POST /api/admin/users/<id>/unlock": (f"{user}/unlock", reason),
            "GET /api/admin/users/<id>/statement": f"{user}/statement",
            "POST /api/admin/deposit": ("/api/admin/deposit", {
                "user_id": ali_id, "amount": "5", "into": "BANK", **key}),
            "POST /api/admin/payroll": ("/api/admin/payroll", {
                "items": [{"user_id": ali_id, "amount": "5"}], "batch_ref": "NOV", **key}),
            "POST /api/admin/withdraw": ("/api/admin/withdraw", {
                "user_id": ali_id, "amount": "5", "from_till": "TILL_MAIN", **key}),
            "POST /api/admin/move": ("/api/admin/move", {
                "from": "BANK", "to": "TILL_MAIN", "amount": "5", **key}),
            "POST /api/admin/reverse": ("/api/admin/reverse",
                                        {"txn_id": deposit, **reason}),
            "POST /api/admin/tills": ("/api/admin/tills", {"name": "back"}),
            "GET /api/admin/health": "/api/admin/health",
            "POST /api/admin/reconcile": ("/api/admin/reconcile", {"counts": {"BANK": "0"}}),
            "POST /api/admin/currencies": ("/api/admin/currencies", {"code": "EUR"}),
            "POST /api/admin/currencies/active": ("/api/admin/currencies/active", {
                "code": "USD", "active": False, **reason}),
            "POST /api/admin/prices": ("/api/admin/prices", {"currency": "USD", "rate": "9"}),
            "GET /api/admin/suppliers": "/api/admin/suppliers",
            "POST /api/admin/suppliers": ("/api/admin/suppliers", {"name": "Tunis agent"}),
            "POST /api/admin/suppliers/<id>/price": (f"{sup}/price",
                                                    {"currency": "USD", "rate": "1"}),
            "POST /api/admin/suppliers/<id>/pay": (f"{sup}/pay", {
                "amount": "5", "from": "BANK", "reference": "W-9", **key}),
            "GET /api/admin/suppliers/<id>/statement": f"{sup}/statement",
            "GET /api/admin/orders": "/api/admin/orders",
            "POST /api/admin/orders/<id>/assign": (f"{o}/assign", {"supplier_id": 1}),
            "POST /api/admin/orders/<id>/unassign": (f"{o}/unassign", reason),
            "POST /api/admin/orders/<id>/cancel": (f"{o}/cancel", reason),
            "POST /api/admin/orders/paid": ("/api/admin/orders/paid", {
                "receipt_code": order["receipt_code"], "payout_ref": "P-1"}),
            "GET /api/admin/margin": "/api/admin/margin?since=2026-01-01",
        }
        for method, regex, _ in web.ROUTES:
            route = regex.pattern[1:-1].replace("(?P<id>[0-9]+)", "<id>")
            if route.startswith("/api/admin/"):
                self.assertIn(f"{method} {route}", requests, "a new admin route: add it here")
        out = {}
        for name, request in requests.items():
            method = name.split()[0]
            path, body = (request, None) if method == "GET" else request
            out[name] = (method, path, None if body is None else dict(body, admin_id=1))
        return out

    # --- admin routes: only an active admin gets through ---
    def test_every_admin_route_refuses_everyone_else(self):
        ali_id, ali, order = self.admin_fixture()
        boss_id, boss = self.new_admin("Boss", "0930000000", "Boss-Pass-2026")
        zed_id, zed = self.new_admin("Zed", "0940000000", "Zed-Pass-2026x")
        self.assertEqual(self.admin.post(f"/api/admin/users/{boss_id}/block",
                                         {"reason": "suspicious payouts"})[0], 200)
        self.assertEqual(self.admin.post(f"/api/admin/users/{zed_id}/deactivate",
                                         {"reason": "left the company"})[0], 200)
        requests = self.admin_requests(ali_id, order)
        callers = [(Client(self.app, ip="10.0.9.1"), (401, "NotLoggedIn")),    # no token
                   (ali, (403, "PermissionDenied")),                           # a worker
                   (boss, (401, "NotLoggedIn")),         # a blocked admin's old session
                   (zed, (401, "NotLoggedIn"))]          # a deactivated admin's one
        before = self.books()
        for caller, expected in callers:
            for name, (method, path, body) in requests.items():
                status, answer = caller.request(method, path, body)
                self.assertEqual((status, answer.get("type")), expected, (name, answer))
        self.assertEqual(self.books(), before)
        status, answer = Client(self.app).post("/api/login", {"phone": "0930000000",
                                                              "password": "Boss-Pass-2026"})
        self.assertEqual((status, answer["type"]), (403, "AccountBlocked"))

    def test_an_admin_blocked_or_closed_mid_request_is_refused_by_the_books(self):
        """The session is checked first (api.admin_id), then every admin action
        checks again inside its write lock: an admin blocked or deactivated in
        between gets a 403 and nothing is written."""
        ali_id, _, order = self.admin_fixture()
        boss_id, boss = self.new_admin("Boss", "0930000000", "Boss-Pass-2026")
        zed_id, zed = self.new_admin("Zed", "0940000000", "Zed-Pass-2026x")
        requests = self.admin_requests(ali_id, order)
        self.admin.post(f"/api/admin/users/{boss_id}/block", {"reason": "suspicious payouts"})
        self.admin.post(f"/api/admin/users/{zed_id}/deactivate", {"reason": "left"})
        before = self.books()
        for admin_id, client in ((boss_id, boss), (zed_id, zed)):
            stale = unittest.mock.patch.object(web.api, "admin_id",
                                               lambda conn, token, admin_id=admin_id: admin_id)
            with stale:
                for name, (method, path, body) in requests.items():
                    if method == "GET" or name == "POST /api/admin/reconcile":
                        continue                    # they only read
                    status, answer = client.request(method, path, body)
                    self.assertEqual((status, answer.get("type")), (403, "PermissionDenied"),
                                     (name, answer))
        self.assertEqual(self.books(), before)

    def test_admin_money_routes_move_the_right_amounts(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        sam_id, sam = self.new_worker("Sam", "0922222222", "sam-wallet-88")
        for path, body in (("/api/admin/tills", {"name": "main"}),
                           ("/api/admin/suppliers", {"name": "Cairo agent"})):
            self.assertEqual(self.admin.post(path, body)[0], 200)

        def post(path, **body):
            status, answer = self.admin.post(path, dict(body, idempotency_key=f"k{post.n}"))
            post.n += 1
            self.assertEqual(status, 200, (path, answer))
            return answer["txn_id"]
        post.n = 0

        def holdings():
            health = self.admin.get("/api/admin/health")[1]
            self.assertEqual(health["problems"], [])
            return {code: m["lyd"] for code, m in health["holdings"].items()}, \
                health["fees"]["lyd"], health["owed_to_workers"]["lyd"]

        post("/api/admin/deposit", user_id=ali_id, amount="100", into="BANK")
        post("/api/admin/payroll", items=[{"user_id": ali_id, "amount": "950.5"},
                                          {"user_id": sam_id, "amount": 1200}],
             batch_ref="OCT-2026")
        self.assertEqual((self.lyd(ali), self.lyd(sam)), ("1,050.500", "1,200.000"))
        post("/api/admin/move", **{"from": "BANK", "to": "TILL_MAIN", "amount": "300"})
        cash = post("/api/admin/withdraw", user_id=ali_id, amount="100", fee="2",
                    from_till="TILL_MAIN", note="Ali at the shop")
        self.assertEqual(self.lyd(ali), "950.500")
        post("/api/admin/suppliers/1/pay", amount="50", **{"from": "BANK"},
             reference="WIRE-1")
        self.assertEqual(holdings(), ({"BANK": "1,900.500", "TILL_MAIN": "202.000"},
                                      "2.000", "2,150.500"))
        supplier = self.admin.get("/api/admin/suppliers/1/statement")[1]
        self.assertEqual(supplier["balance"]["lyd"], "-50.000")         # a prepayment

        undo = {"txn_id": cash, "reason": "paid out by mistake", "idempotency_key": "u1"}
        first = self.admin.post("/api/admin/reverse", undo)
        self.assertEqual(first[0], 200, first)
        self.assertEqual(self.admin.post("/api/admin/reverse", undo), first)    # a retry
        self.assertEqual(self.admin.post("/api/admin/reverse",
                                         dict(undo, idempotency_key="u2"))[0], 400)
        self.assertEqual(self.lyd(ali), "1,050.500")
        self.assertEqual(holdings(), ({"BANK": "1,900.500", "TILL_MAIN": "300.000"},
                                      "0.000", "2,250.500"))
        status, answer = self.admin.post("/api/admin/reconcile", {
            "counts": {"BANK": "1900.5", "TILL_MAIN": "299"}})
        self.assertEqual([(r["account"], r["difference"], r["ok"]) for r in answer["rows"]],
                         [("BANK", 0, True), ("TILL_MAIN", -1000, False)])
        # More than the till or the bank holds, by the books: nothing moves.
        for path, body in (("/api/admin/withdraw", {"user_id": ali_id, "amount": "400",
                                                    "from_till": "TILL_MAIN"}),
                           ("/api/admin/move", {"from": "BANK", "to": "TILL_MAIN",
                                                "amount": "5000"})):
            status, answer = self.admin.post(path, dict(body, idempotency_key="too-much"))
            self.assertEqual((status, answer["type"]), (409, "NotEnoughCash"), path)
        self.assertEqual(holdings(), ({"BANK": "1,900.500", "TILL_MAIN": "300.000"},
                                      "0.000", "2,250.500"))
        kinds = [e["kind"] for e in ali.get("/api/statement")[1]["entries"]]
        self.assertEqual(kinds, ["reversal", "withdrawal", "payroll", "deposit"])

    # --- not found is a 404, wherever the id comes from ---
    def test_unknown_things_are_not_found(self):
        ali_id, ali, order = self.admin_fixture()
        cases = [
            (self.admin, "/api/admin/reverse", {"txn_id": 999, "reason": "typo"}),
            (self.admin, "/api/admin/orders/999/assign", {"supplier_id": 1}),
            (self.admin, "/api/admin/orders/999/unassign", {"reason": "x"}),
            (self.admin, "/api/admin/orders/999/cancel", {"reason": "x"}),
            (self.admin, f"/api/admin/orders/{order['id']}/assign", {"supplier_id": 99}),
            (self.admin, "/api/admin/orders/paid", {"receipt_code": "NOTAREAL22",
                                                    "payout_ref": "P-1"}),
            (self.admin, "/api/admin/suppliers/99/pay", {"amount": "1", "from": "BANK",
                                                         "reference": "W",
                                                         "idempotency_key": "k"}),
            (self.admin, "/api/admin/deposit", {"user_id": 999, "amount": "1",
                                                "into": "BANK", "idempotency_key": "k"}),
            (self.admin, "/api/admin/users/999/block", {"reason": "x"}),
            (ali, "/api/orders", {"quote_id": "no-such-quote", "beneficiary_name": "Ahmed",
                                  "beneficiary_id": "A1"}),
        ]
        before = self.books()
        for client, path, body in cases:
            status, answer = client.post(path, body)
            self.assertEqual(status, 404, (path, answer))
        for path in ("/api/admin/suppliers/99/statement", "/api/admin/users/999/statement"):
            self.assertEqual(self.admin.get(path)[0], 404, path)
        self.assertEqual(self.books(), before)

    # --- a worker can act only on their own money ---
    def test_a_worker_cannot_touch_another_workers_money(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        sam_id, sam = self.new_worker("Sam", "0922222222", "sam-wallet-88")
        self.setup_orders(sam_id)                       # Sam has 1000, Ali nothing
        omar_id, _ = self.new_worker("Omar", "0933333333", "omar-wallet-99")
        _, sams_quote = sam.post("/api/quote", {"currency": "USD", "amount": "10"})
        before = self.books()

        # Ali names Sam as the payer, in every way a body could.
        claims = {"from_phone": "0922222222", "from_user": sam_id, "user_id": sam_id,
                  "customer_id": sam_id}
        status, answer = ali.post("/api/send", dict(claims, to_phone="0933333333",
                                                    amount="10", idempotency_key="k1"))
        self.assertEqual((status, answer["type"]), (409, "InsufficientFunds"))
        status, answer = ali.post("/api/orders", dict(claims, quote_id=sams_quote["quote_id"],
                                                      beneficiary_name="Ahmed",
                                                      beneficiary_id="A1"))
        self.assertEqual(status, 404, answer)           # Sam's quote is not Ali's to use
        for path, body in (("/api/admin/withdraw", {"user_id": sam_id, "amount": "10",
                                                    "from_till": "BANK",
                                                    "idempotency_key": "k2"}),
                           ("/api/admin/reverse", {"txn_id": 1, "reason": "mine"})):
            self.assertEqual(ali.post(path, body)[0], 403, path)
        self.assertEqual(ali.get(f"/api/admin/users/{sam_id}/statement")[0], 403)
        self.assertEqual(self.books(), before)
        self.assertEqual((self.lyd(ali), self.lyd(sam)), ("0.000", "1,000.000"))

        # The same idempotency key from two workers is two transfers.
        self.admin.post("/api/admin/deposit", {"user_id": ali_id, "amount": "5",
                                               "into": "BANK", "idempotency_key": "a"})
        send = {"to_phone": "0933333333", "amount": "5", "idempotency_key": "same"}
        sent = [client.post("/api/send", send) for client in (sam, ali, sam)]
        self.assertEqual([s for s, _ in sent], [200, 200, 200])
        self.assertNotEqual(sent[0][1]["txn_id"], sent[1][1]["txn_id"])
        self.assertEqual(sent[0][1]["txn_id"], sent[2][1]["txn_id"])
        self.assertEqual(self.lyd(user_id=omar_id), "10.000")
        self.assertEqual([e["kind"] for e in ali.get("/api/statement")[1]["entries"]],
                         ["transfer", "deposit"])
        self.assertEqual(ali.get("/api/orders")[1], {"orders": []})

    def test_workers_never_see_supplier_cost_or_margin(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.setup_orders(ali_id)                       # supplier "Cairo agent"
        seen = []

        def worker(answer):
            seen.append(answer)
            return answer
        status, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
        worker(quote)
        order = worker(ali.post("/api/orders", {"quote_id": quote["quote_id"],
                                                "beneficiary_name": "Ahmed",
                                                "beneficiary_id": "A1"})[1])
        paid, cancelled = order, self.place(ali, "A2")
        self.admin.post(f"/api/admin/orders/{paid['id']}/assign", {"supplier_id": 1})
        self.admin.post("/api/admin/orders/paid", {"receipt_code": paid["receipt_code"],
                                                   "payout_ref": "EG-1"})
        self.admin.post(f"/api/admin/orders/{cancelled['id']}/assign", {"supplier_id": 1})
        self.admin.post(f"/api/admin/orders/{cancelled['id']}/unassign",
                        {"reason": "Cairo agent charges 7.80, too slow"})
        self.admin.post(f"/api/admin/orders/{cancelled['id']}/cancel",
                        {"reason": "Cairo agent margin too thin"})
        admin_view = self.admin.get("/api/admin/orders")[1]["orders"]
        self.assertEqual({o["supplier_cost"] for o in admin_view}, {78000, None})
        orders = worker(ali.get("/api/orders")[1])["orders"]
        self.assertEqual([o["status"] for o in orders], ["cancelled", "paid"])
        statement = worker(ali.get("/api/statement")[1])["entries"]
        self.assertEqual(statement[0]["note"], "Order cancelled")
        worker(ali.get("/api/me")[1])
        worker(ali.get("/api/prices")[1])

        def keys_and_values(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys_and_values(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys_and_values(item)
            else:
                yield value
        for answer in seen:
            self.assertNotIn("Cairo", json.dumps(answer))      # name, or in a reason
            for item in keys_and_values(answer):
                self.assertNotIn(item, (78000, "7.80"), answer)     # supplier cost, rate
                if isinstance(item, str):
                    self.assertNotIn("supplier", item.lower(), answer)
                    self.assertNotIn("margin", item.lower(), answer)

    def test_margin_report_counts_orders_paid_in_its_window(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.setup_orders(ali_id)                       # sold at 7.85, Cairo charges 7.80
        order = self.place(ali)
        self.place(ali, "A2")                           # not paid: not in the report
        self.assertEqual(self.admin.post(f"/api/admin/orders/{order['id']}/assign",
                                         {"supplier_id": 1})[0], 200)
        self.assertEqual(self.admin.post("/api/admin/orders/paid", {
            "receipt_code": order["receipt_code"], "payout_ref": "EG-1"})[0], 200)
        paid_at = self.admin.get("/api/admin/orders?status=paid")[1]["orders"][0]["paid_at"]
        self.assertTrue(paid_at.endswith("+00:00"), paid_at)
        row = {"currency": "USD", "supplier_id": 1, "supplier": "Cairo agent", "orders": 1,
               "foreign_amount": "10.00", "charged": 78500, "supplier_cost": 78000,
               "margin": 500}

        def report(**window):
            status, answer = self.admin.get("/api/admin/margin?" + urlencode(window))
            self.assertEqual(status, 200, (window, answer))
            return answer["rows"]
        self.assertEqual(report(), [row])
        self.assertEqual(report(since=paid_at), [row])          # from `since` on
        self.assertEqual(report(until=paid_at), [])             # up to, not including, `until`
        self.assertEqual(report(since=paid_at[:10]), [row])     # a date is midnight UTC
        self.assertEqual(report(until=paid_at[:10]), [])
        self.assertEqual(report(until=paid_at[:-6]), [])        # no zone = UTC
        self.assertEqual(report(until=paid_at.replace("+00:00", "-01:00")), [row])
        self.assertEqual(report(since=paid_at.replace("+00:00", "-01:00")), [])

    # --- the owner's rules, over HTTP ---
    def test_rule_a_an_order_is_cancelled_only_while_placed(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.setup_orders(ali_id)
        order = self.place(ali)
        path = f"/api/admin/orders/{order['id']}"
        self.assertEqual(self.lyd(ali), "921.500")
        self.assertEqual(self.admin.post(f"{path}/assign", {"supplier_id": 1})[0], 200)
        status, answer = self.admin.post(f"{path}/cancel", {"reason": "worker asked"})
        self.assertEqual((status, answer["type"]), (409, "Conflict"))      # unassign first
        self.assertEqual(ali.post(f"{path}/unassign", {"reason": "mine"})[0], 403)
        for blank in ({}, {"reason": " "}):
            self.assertEqual(self.admin.post(f"{path}/unassign", blank)[0], 400)
        self.assertEqual(self.lyd(ali), "921.500")
        self.assertEqual(self.admin.post(f"{path}/unassign",
                                         {"reason": "Cairo confirmed not paid"})[0], 200)
        self.assertEqual(ali.post(f"{path}/cancel", {"reason": "refund me"})[0], 403)
        self.assertEqual(self.admin.post(f"{path}/cancel", {})[0], 400)    # a reason
        self.assertEqual(self.admin.post(f"{path}/cancel", {"reason": "worker asked"})[0],
                         200)
        self.assertEqual(self.lyd(ali), "1,000.000")
        self.assertEqual(self.admin.post(f"{path}/cancel", {"reason": "again"})[0], 400)
        self.assertEqual(self.lyd(ali), "1,000.000")

        paid = self.place(ali)                         # a paid order stays paid
        self.admin.post(f"/api/admin/orders/{paid['id']}/assign", {"supplier_id": 1})
        self.admin.post("/api/admin/orders/paid", {"receipt_code": paid["receipt_code"],
                                                   "payout_ref": "EG-1"})
        for action in ("unassign", "cancel"):
            self.assertEqual(self.admin.post(f"/api/admin/orders/{paid['id']}/{action}",
                                             {"reason": "late"})[0], 400, action)
        self.assertEqual(self.lyd(ali), "921.500")
        self.assertEqual(self.admin.get("/api/admin/health")[1]["problems"], [])

    def test_rule_b_a_libyan_mobile_and_an_optional_foreign_whatsapp(self):
        for phone in ("0961234567", "0811234567", "091123456", "09112345678",
                      "+201001234567", "+218 21 123 4567", "phone", ""):
            status, answer = self.admin.post("/api/admin/users", {"name": "X", "phone": phone})
            self.assertEqual(status, 400, (phone, answer))
        for whatsapp in ("201001234567", "+0201001234567", "+20 1", "+20-100-abc-4567"):
            status, answer = self.admin.post("/api/admin/users", {
                "name": "X", "phone": "0951234567", "whatsapp": whatsapp})
            self.assertEqual(status, 400, (whatsapp, answer))
        made = [self.admin.post("/api/admin/users", body) for body in (
            {"name": "Ali", "phone": "+218 95 123 4567", "whatsapp": "+20 100 123 4567"},
            {"name": "Sam", "phone": "092-222-2222", "whatsapp": "00 33 6 12 34 56 78"},
            {"name": "Omar", "phone": "٠٩٣٣٣٣٣٣٣٣"})]
        self.assertEqual([s for s, _ in made], [200, 200, 200])
        status, answer = self.admin.post("/api/admin/users", {"name": "Ali again",
                                                              "phone": "00218951234567"})
        self.assertEqual((status, answer["type"]), (409, "Conflict"))
        ali_id = made[0][1]["id"]
        self.assertEqual(self.admin.post(f"/api/admin/users/{ali_id}/whatsapp",
                                         {"whatsapp": "12345"})[0], 400)
        users = {u["name"]: u for u in self.admin.get("/api/admin/users")[1]["users"]}
        for user in users.values():
            self.assertRegex(user["phone"], r"^\+2189[1-5][0-9]{7}$")
        self.assertEqual([(users[n]["phone"], users[n]["whatsapp"]) for n in
                          ("Ali", "Sam", "Omar")],
                         [("+218951234567", "+201001234567"),
                          ("+218922222222", "+33612345678"), ("+218933333333", None)])

    def test_rule_c_deactivate_only_at_zero_with_no_open_orders(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        _, sam = self.new_worker("Sam", "0922222222", "sam-wallet-88")
        self.setup_orders(ali_id)
        self.assertEqual(self.admin.post("/api/admin/tills", {"name": "main"})[0], 200)
        self.admin.post("/api/admin/move", {"from": "BANK", "to": "TILL_MAIN",
                                            "amount": "1000", "idempotency_key": "m"})
        path = f"/api/admin/users/{ali_id}"

        def deactivate():
            return self.admin.post(f"{path}/deactivate", {"reason": "left the company"})
        self.assertEqual(deactivate()[0], 400)                          # 1000 LYD left
        status, quote = ali.post("/api/quote", {"currency": "USD", "amount": "127.38"})
        order = ali.post("/api/orders", {"quote_id": quote["quote_id"],
                                         "beneficiary_name": "Ahmed", "beneficiary_id": "A1"})
        self.assertEqual(self.lyd(ali), "0.067")
        status, cash = self.admin.post("/api/admin/withdraw", {
            "user_id": ali_id, "amount": "0.067", "from_till": "TILL_MAIN",
            "idempotency_key": "w1"})
        self.assertEqual((status, self.lyd(ali)), (200, "0.000"))
        status, answer = deactivate()
        self.assertEqual(status, 400)
        self.assertIn("unfinished orders", answer["error"])
        self.assertEqual(self.admin.post(f"/api/admin/orders/{order[1]['id']}/cancel",
                                         {"reason": "worker leaving"})[0], 200)
        self.assertEqual(deactivate()[0], 400)                          # refunded: money again
        self.assertEqual(self.admin.post("/api/admin/withdraw", {
            "user_id": ali_id, "amount": "999.933", "from_till": "TILL_MAIN",
            "idempotency_key": "w2"})[0], 200)
        self.assertEqual(self.admin.post(f"{path}/deactivate", {})[0], 400)  # a reason
        self.assertEqual(deactivate()[0], 200)
        self.assertEqual(ali.get("/api/me")[0], 401)                    # logged out
        status, answer = Client(self.app).post("/api/login", {"phone": "0911111111",
                                                              "password": "ali-wallet-77"})
        self.assertEqual(status, 401)
        before = self.books()
        for status, answer in (
                self.admin.post("/api/admin/deposit", {"user_id": ali_id, "amount": "1",
                                                       "into": "BANK", "idempotency_key": "d"}),
                self.admin.post("/api/admin/reverse", {"txn_id": cash["txn_id"],
                                                       "reason": "would pay a closed account"}),
                sam.post("/api/send", {"to_phone": "0911111111", "amount": "1",
                                       "idempotency_key": "s"})):
            self.assertIn(status, (400, 404), answer)
        self.assertEqual(self.books(), before)
        self.assertEqual(self.admin.get("/api/admin/health")[1]["problems"], [])
        self.assertEqual(self.admin.post(f"{path}/reactivate", {"reason": "came back"})[0],
                         200)
        status, _ = Client(self.app).post("/api/login", {"phone": "0911111111",
                                                         "password": "ali-wallet-77"})
        self.assertEqual(status, 200)

    def test_block_freezes_a_worker_at_once_but_salary_still_arrives(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        sam_id, sam = self.new_worker("Sam", "0922222222", "sam-wallet-88")
        self.setup_orders(ali_id)
        self.admin.post("/api/admin/deposit", {"user_id": sam_id, "amount": "50",
                                               "into": "BANK", "idempotency_key": "s"})
        self.assertEqual(self.admin.post("/api/admin/tills", {"name": "main"})[0], 200)
        self.admin.post("/api/admin/move", {"from": "BANK", "to": "TILL_MAIN",
                                            "amount": "500", "idempotency_key": "m"})
        twin = Client(self.app, ip="10.0.3.3")          # Ali's phone and laptop
        self.assertEqual(twin.post("/api/login", {"phone": "0911111111",
                                                  "password": "ali-wallet-77"})[0], 200)
        _, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
        self.assertEqual(self.admin.post(f"/api/admin/users/{ali_id}/block",
                                         {"reason": "suspected fraud"})[0], 200)

        for client in (ali, twin):                      # every session ends at once
            for method, path, body in (
                    ("GET", "/api/me", None),
                    ("POST", "/api/send", {"to_phone": "0922222222", "amount": "1",
                                           "idempotency_key": "b1"}),
                    ("POST", "/api/quote", {"currency": "USD", "amount": "10"}),
                    ("POST", "/api/orders", {"quote_id": quote["quote_id"],
                                             "beneficiary_name": "Ahmed",
                                             "beneficiary_id": "A1"})):
                self.assertEqual(client.request(method, path, body)[0], 401, path)
        status, answer = Client(self.app).post("/api/login", {"phone": "0911111111",
                                                              "password": "ali-wallet-77"})
        self.assertEqual((status, answer["type"]), (403, "AccountBlocked"))
        status, answer = self.admin.post("/api/admin/withdraw", {
            "user_id": ali_id, "amount": "10", "from_till": "TILL_MAIN",
            "idempotency_key": "w"})
        self.assertEqual((status, answer["type"]), (403, "AccountBlocked"))      # no cash
        status, answer = sam.post("/api/send", {"to_phone": "0911111111", "amount": "1",
                                                "idempotency_key": "to-ali"})
        self.assertEqual(status, 404)                                         # nor receiving
        self.assertEqual(self.lyd(sam), "50.000")
        for path, body in (("/api/admin/deposit", {"user_id": ali_id, "amount": "20",
                                                   "into": "BANK"}),
                           ("/api/admin/payroll", {"items": [{"user_id": ali_id,
                                                              "amount": "900"}],
                                                   "batch_ref": "OCT-2026"})):
            status, answer = self.admin.post(path, dict(body, idempotency_key="in"))
            self.assertEqual(status, 200, (path, answer))                     # money still in
        self.assertEqual(self.lyd(user_id=ali_id), "1,920.000")

        self.assertEqual(self.admin.post(f"/api/admin/users/{ali_id}/unblock",
                                         {"reason": "cleared"})[0], 200)
        self.assertEqual(ali.get("/api/me")[0], 401)                # old sessions stay ended
        again = Client(self.app, ip="10.0.3.4")
        self.assertEqual(again.post("/api/login", {"phone": "0911111111",
                                                   "password": "ali-wallet-77"})[0], 200)
        self.assertEqual(again.post("/api/send", {"to_phone": "0922222222", "amount": "20",
                                                  "idempotency_key": "after"})[0], 200)
        self.assertEqual((self.lyd(again), self.lyd(sam)), ("1,900.000", "70.000"))

    # --- two requests at once: the write lock lets one through at a time ---
    def at_once(self, *calls):
        """Run the calls in threads of their own, each with its own database
        connection like the server's threads; their results, in order."""
        results = [None] * len(calls)

        def run(i, call):
            try:
                results[i] = call()
            except Exception as e:          # shown in the test's failure
                results[i] = e
            finally:
                self.app.close_connection()
        threads = [threading.Thread(target=run, args=(i, call))
                   for i, call in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return results

    def meet_inside(self, module, name):
        """Patch module.name so a call first waits (a moment) for a second one
        to get there too. Inside the write lock a second one can't, so the wait
        just runs out; without the lock both would go on side by side, each
        thinking it is alone."""
        real, barrier = getattr(module, name), threading.Barrier(2, timeout=0.5)

        def wait_then_call(*args, **kwargs):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return real(*args, **kwargs)
        return unittest.mock.patch.object(module, name, wait_then_call)

    def twin(self, worker):
        """Another client with the same session: a second tab, or a retry."""
        other = Client(self.app, ip=worker.ip)
        other.cookie = worker.cookie
        return other

    def test_a_transfer_sent_twice_at_once_moves_the_money_once(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        _, sam = self.new_worker("Sam", "0922222222", "sam-wallet-88")
        self.admin.post("/api/admin/deposit", {"user_id": ali_id, "amount": "100",
                                               "into": "BANK", "idempotency_key": "d"})
        send = {"to_phone": "0922222222", "amount": "40", "idempotency_key": "confirm-7"}
        twin = self.twin(ali)
        with self.meet_inside(L, "_idempotent"):
            first, second = self.at_once(lambda: ali.post("/api/send", send),
                                         lambda: twin.post("/api/send", send))
        self.assertEqual((first[0], second[0]), (200, 200), (first, second))
        self.assertEqual(first[1]["txn_id"], second[1]["txn_id"])
        self.assertEqual((self.lyd(ali), self.lyd(sam)), ("60.000", "40.000"))

    def test_two_orders_sent_at_once_on_one_quote_charge_once(self):
        ali_id, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        self.setup_orders(ali_id)
        twin = self.twin(ali)
        for second_id in ("A1", "B2"):      # a double tap, then two beneficiaries
            _, quote = ali.post("/api/quote", {"currency": "USD", "amount": "10"})
            body = {"quote_id": quote["quote_id"], "beneficiary_name": "Ahmed",
                    "beneficiary_id": "A1"}
            with self.meet_inside(O, "_new_receipt_code"):
                answers = self.at_once(
                    lambda: ali.post("/api/orders", body),
                    lambda: twin.post("/api/orders", dict(body, beneficiary_id=second_id)))
            statuses = sorted(answer[0] for answer in answers)
            if second_id == "A1":
                self.assertEqual(statuses, [200, 200], answers)
                self.assertEqual(answers[0][1]["receipt_code"], answers[1][1]["receipt_code"])
            else:
                self.assertEqual(statuses, [200, 409], answers)
        self.assertEqual(len(ali.get("/api/orders")[1]["orders"]), 2)
        self.assertEqual(self.lyd(ali), "843.000")              # 1000 - 2 x 78.5
        self.assertEqual(self.admin.get("/api/admin/health")[1]["problems"], [])

    # --- sessions: logout everywhere, change password, a new setup code ---
    def test_which_sessions_end_over_http(self):
        ali_id, phone = self.new_worker("Ali", "0911111111", "ali-wallet-77")
        login = {"phone": "0911111111", "password": "ali-wallet-77"}

        def sessions(n):
            clients = [Client(self.app, ip=f"10.0.4.{i}") for i in range(n)]
            for client in clients:
                self.assertEqual(client.post("/api/login", login)[0], 200)
            return clients
        laptop, tablet = sessions(2)
        self.assertEqual(laptop.post("/api/logout")[0], 200)            # this one only
        self.assertEqual((phone.get("/api/me")[0], laptop.get("/api/me")[0],
                          tablet.get("/api/me")[0]), (200, 401, 200))
        self.assertEqual(Client(self.app).post("/api/logout-everywhere")[0], 401)
        self.assertEqual(tablet.post("/api/logout-everywhere")[0], 200)  # all of them
        self.assertIsNone(tablet.cookie)
        self.assertEqual((phone.get("/api/me")[0], tablet.get("/api/me")[0]), (401, 401))

        laptop, tablet = sessions(2)
        change = {"old_password": "ali-wallet-77", "new_password": "ali-wallet-2027"}
        self.assertEqual(laptop.post("/api/change-password", change)[0], 200)
        self.assertEqual((laptop.get("/api/me")[0], tablet.get("/api/me")[0]), (200, 401))
        self.assertEqual(Client(self.app).post("/api/login", login)[0], 401)
        self.assertEqual(Client(self.app).post("/api/login", dict(
            login, password="ali-wallet-2027"))[0], 200)

        status, code = self.admin.post(f"/api/admin/users/{ali_id}/setup-code", {})
        self.assertEqual(status, 200, code)                 # forgot it: a new code
        self.assertEqual(laptop.get("/api/me")[0], 401)     # ends every session
        self.assertEqual(Client(self.app).post("/api/login", dict(
            login, password="ali-wallet-2027"))[0], 401)    # and the old password
        again = Client(self.app, ip="10.0.4.9")
        setup = {"phone": "0911111111", "code": code["setup_code"].lower(),
                 "password": "ali-wallet-2028"}
        self.assertEqual(again.post("/api/setup-password", setup)[0], 200)
        self.assertEqual(again.get("/api/me")[0], 200)
        self.assertEqual(Client(self.app).post("/api/setup-password", dict(
            setup, password="thief-wallet-1"))[0], 401)     # used once only

    def test_an_idle_admin_session_ends_but_a_workers_does_not(self):
        _, ali = self.new_worker("Ali", "0911111111", "ali-wallet-77")

        def everyone_idle_for(minutes):
            conn = A.connect(self.db_path)
            try:
                conn.execute("UPDATE sessions SET last_seen = ?", (
                    (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(),))
            finally:
                conn.close()
        everyone_idle_for(A.ADMIN_IDLE_MINUTES - 2)
        self.assertEqual(self.admin.get("/api/admin/users")[0], 200)
        everyone_idle_for(A.ADMIN_IDLE_MINUTES + 1)
        status, answer = self.admin.get("/api/admin/users")
        self.assertEqual((status, answer["type"]), (401, "NotLoggedIn"))
        self.assertEqual(self.admin.post("/api/admin/tills", {"name": "main"})[0], 401)
        self.assertEqual(ali.get("/api/me")[0], 200)        # a worker's week has no idle limit

    # --- the README describes what the app does ---
    def test_readme_names_every_route_and_error_status(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "README.md"),
                  encoding="utf-8") as f:
            readme = f.read()
        for method, regex, _ in web.ROUTES:
            last = regex.pattern[1:-1].rsplit("/", 1)[-1]
            self.assertIn(f"/{last}", readme, regex.pattern)
        with open(web.__file__, encoding="utf-8") as f:
            source = f.read()
        statuses = {code for _, code in web.ERROR_STATUS} | {500} | {
            int(s) for s in re.findall(r"HttpError\((\d{3})", source)}     # 405 too
        errors = readme.split("Errors:", 1)[1].split("\n\n", 1)[0]
        for status in sorted(statuses):
            self.assertIn(str(status), errors, f"README Errors: {status} is not described")


if __name__ == "__main__":
    unittest.main()
