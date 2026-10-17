import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
from wsgiref.util import setup_testing_defaults

from app import auth as A
from app import ledger as L
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


if __name__ == "__main__":
    unittest.main()
