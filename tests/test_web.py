import io
import json
import os
import tempfile
import unittest
import unittest.mock
from wsgiref.util import setup_testing_defaults

from app import auth as A
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


if __name__ == "__main__":
    unittest.main()
