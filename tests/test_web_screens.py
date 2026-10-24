"""What the server does for the workers' screens (app/web.py): the page files
and their headers, the Origin check, the token rule, the 'en' defaults, and
the API answers whose texts the screens rely on. The page tests serve small
stand-in files from a temporary folder, never app/static itself."""
import contextlib
import hashlib
import http.client
import inspect
import io
import json
import re
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import quote as urlquote

from app import auth as A
from app import ledger as L
from app import orders as O
from app import serve as S
from app import web

from .test_web import Client

try:
    import waitress  # noqa: F401
    HAVE_WAITRESS = True
except ImportError:
    HAVE_WAITRESS = False

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"                  # the Host of every test request unless said otherwise
SITE = "https://127.0.0.1"          # this site's Origin, for a secure app
ELSEWHERE = "https://evil.example"
API_CSP = "default-src 'none'; frame-ancestors 'none'"
REFUSED = (403, {"error": "refused: sent from another website", "type": "http"})
TYPES = {
    "/": "text/html; charset=utf-8",
    "/static/app.js": "text/javascript; charset=utf-8",
    "/static/strings.js": "text/javascript; charset=utf-8",
    "/static/style.css": "text/css; charset=utf-8",
    "/static/icon.svg": "image/svg+xml",
    "/admin": "text/html; charset=utf-8",
    "/static/admin.js": "text/javascript; charset=utf-8",
    "/static/admin.css": "text/css; charset=utf-8",
}
FILES = {                           # stand-ins for app/static, by file name
    "index.html": b"<!doctype html>\n<title>Wallet</title>\n",
    "app.js": b'"use strict";\n',
    "strings.js": b'window.STRINGS = {"en": {}};\n',
    "style.css": b"[hidden] { display: none !important; }\n",
    "icon.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>\n',
    "admin.html": b"<!doctype html>\n<title>Office</title>\n",
    "admin.js": b'"use strict"; // office\n',
    "admin.css": b".table { inline-size: 100%; }\n",
}


class Unreadable:
    """A request body that must never be read."""

    def read(self, *args):
        raise AssertionError("the request body was read")

    readline = read


def raw(app, method, path, headers=None, body=b""):
    """The app called with an environ of its own: (status, headers, body bytes),
    for answers that need not be JSON. Host is 127.0.0.1 unless `headers` say
    otherwise; a header given as None is left out (HTTP_HOST=None: no Host)."""
    path, _, query = path.partition("?")
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
               "SERVER_NAME": HOST, "SERVER_PORT": "80", "SERVER_PROTOCOL": "HTTP/1.1",
               "HTTP_HOST": HOST, "REMOTE_ADDR": "10.0.0.1", "wsgi.url_scheme": "http",
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body), "wsgi.errors": io.StringIO()}
    environ.update(headers or {})
    environ = {name: value for name, value in environ.items() if value is not None}
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = response_headers
    data = b"".join(app(environ, start_response))
    return captured["status"], dict(captured["headers"]), data


def over_socket(port, method, path, headers=None):
    """One request on a connection of its own, read to the end, up to the
    server closing it: (status, headers (any case, as waitress writes ETag as
    Etag), every byte after the headers). Unlike http.client, which reads no
    body for a HEAD or a 304 whatever is sent, this shows what was sent."""
    lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Connection: close",
             *(f"{name}: {value}" for name, value in (headers or {}).items())]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    status, _, rest = data.partition(b"\r\n")
    head, _, body = rest.partition(b"\r\n\r\n")
    return (int(status.split()[1]), http.client.parse_headers(io.BytesIO(head + b"\r\n\r\n")),
            body)


def password(name):
    return f"{name.lower()}-wallet-2026"


class ScreensTestCase(unittest.TestCase):
    """A wallet with its first admin logged in, served with stand-in page files."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.static = self.dir / "static"
        self.static.mkdir()
        for name, body in FILES.items():
            (self.static / name).write_bytes(body)
        self.db_path = str(self.dir / "wallet.db")
        conn = A.connect(self.db_path)
        A.create_first_admin(conn, "Admin", "0910000000", "Office-Pass-2026")
        conn.close()
        self.app = self.make_app()
        self.admin = Client(self.app)
        status, body = self.admin.post("/api/login", {"phone": "0910000000",
                                                      "password": "Office-Pass-2026"})
        self.assertEqual(status, 200, body)

    def make_app(self, **options):
        app = web.App(self.db_path, **{"static_dir": self.static, **options})
        self.addCleanup(app.close_connection)
        return app

    def worker(self, name, phone, lyd=0):
        """A worker who chose password(name) with their setup code, logged in on
        a Client of their own: (user id, client)."""
        status, made = self.admin.post("/api/admin/users", {"name": name, "phone": phone})
        self.assertEqual(status, 200, made)
        client = Client(self.app, ip=f"10.0.1.{made['id']}")
        status, body = client.post("/api/setup-password", {
            "phone": phone, "code": made["setup_code"], "password": password(name)})
        self.assertEqual(status, 200, body)
        if lyd:
            status, body = self.admin.post("/api/admin/deposit", {
                "user_id": made["id"], "amount": str(lyd), "into": "BANK",
                "idempotency_key": f"setup-{made['id']}"})
            self.assertEqual(status, 200, body)
        return made["id"], client

    def lyd(self, client):
        return client.get("/api/me")[1]["balance"]["lyd"]

    def sql(self, statement, *args):
        """A write straight into the database, as another program would."""
        conn = A.connect(self.db_path)
        try:
            conn.execute(statement, args)
        finally:
            conn.close()

    def count(self, statement, *args):
        conn = A.connect(self.db_path)
        try:
            return conn.execute(statement, args).fetchone()[0]
        finally:
            conn.close()


# ---------- the page files ----------

class PageTests(ScreensTestCase):
    def assert_page_headers(self, headers, etag=None):
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertRegex(headers["ETag"], r'^"[0-9a-f]{32}"$')
        if etag is not None:
            self.assertEqual(headers["ETag"], etag)
        for name, value in web.PAGE_HEADERS:
            self.assertEqual(headers[name], value, name)
        self.assertNotIn("Set-Cookie", headers)

    def assert_not_found(self, answer, what):
        status, headers, body = answer
        self.assertEqual((status, json.loads(body)), (404, {"error": "not found", "type": "http"}),
                         what)
        self.assertEqual(headers["Cache-Control"], "no-store", what)
        self.assertEqual(headers["Content-Security-Policy"], API_CSP, what)

    def test_the_page_paths_are_the_agreed_ones(self):
        # The contract with app/static and the browser tests.
        self.assertEqual(web.STATIC_FILES, {
            "/": "index.html", "/static/app.js": "app.js", "/static/strings.js": "strings.js",
            "/static/style.css": "style.css", "/static/icon.svg": "icon.svg",
            "/admin": "admin.html", "/static/admin.js": "admin.js",
            "/static/admin.css": "admin.css"})
        self.assertEqual(web.STATIC_DIR, ROOT / "app" / "static")
        self.assertEqual(inspect.signature(web.App).parameters["static_dir"].default,
                         web.STATIC_DIR)
        self.assertEqual(web.PAGE_CSP, (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; form-action 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; require-trusted-types-for 'script'; "
            "trusted-types 'none'"))
        self.assertEqual(web.ORIGIN_REFUSED, "refused: sent from another website")

    def test_root_serves_the_page(self):
        status, headers, body = raw(self.app, "GET", "/")
        self.assertEqual((status, body), (200, FILES["index.html"]))
        self.assertEqual(set(headers), {"Content-Type", "Content-Length", "Cache-Control", "ETag",
                                        *dict(web.PAGE_HEADERS)})
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assert_page_headers(headers, '"' + hashlib.sha256(body).hexdigest()[:32] + '"')
        self.assertEqual(dict(web.PAGE_HEADERS), {
            "Content-Security-Policy": web.PAGE_CSP, "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
        # Logged in, or opened from a link with a query: the same page, no cookie.
        for headers_in, path in (({"HTTP_COOKIE": f"__Host-session={self.admin.cookie}"}, "/"),
                                 ({}, "/?from=whatsapp")):
            status, headers, again = raw(self.app, "GET", path, headers_in)
            self.assertEqual((status, again), (200, body), path)
            self.assert_page_headers(headers)

    def test_each_page_file_has_its_type(self):
        for path, name in web.STATIC_FILES.items():
            status, headers, body = raw(self.app, "GET", path)
            self.assertEqual((status, body), (200, FILES[name]), path)
            self.assertEqual(headers["Content-Type"], TYPES[path], path)
            self.assertEqual(headers["Content-Length"], str(len(body)), path)
            self.assert_page_headers(headers)
        tags = {raw(self.app, "GET", path)[1]["ETag"] for path in web.STATIC_FILES}
        self.assertEqual(len(tags), len(web.STATIC_FILES))      # one tag per content

    def test_a_matching_etag_is_not_modified(self):
        _, headers, _ = raw(self.app, "GET", "/static/app.js")
        etag = headers["ETag"]
        for sent in (etag, f"W/{etag}", f'"other", {etag}', f'"a",W/{etag} ', "*"):
            status, headers, body = raw(self.app, "GET", "/static/app.js",
                                        {"HTTP_IF_NONE_MATCH": sent})
            self.assertEqual((status, body), (304, b""), sent)
            self.assert_page_headers(headers, etag)
            self.assertNotIn("Content-Type", headers)
            self.assertNotIn("Content-Length", headers)
        other = raw(self.app, "GET", "/static/style.css")[1]["ETag"]
        for sent in ('"other"', 'W/"other"', etag[:-2] + '"', other, ""):
            status, _, body = raw(self.app, "GET", "/static/app.js", {"HTTP_IF_NONE_MATCH": sent})
            self.assertEqual((status, body), (200, FILES["app.js"]), sent)

    def test_head_has_the_headers_and_no_body(self):
        for path in web.STATIC_FILES:
            _, got, _ = raw(self.app, "GET", path)
            status, headers, body = raw(self.app, "HEAD", path)
            self.assertEqual((status, headers, body), (200, got, b""), path)
            status, headers, body = raw(self.app, "HEAD", path,
                                        {"HTTP_IF_NONE_MATCH": got["ETag"]})
            self.assertEqual((status, body), (304, b""), path)
            self.assertNotIn("Content-Length", headers)
        # The API's answers to HEAD: the status and headers they had, and no
        # JSON, which a client reusing the connection would read as the next
        # answer.
        for path, status in (("/api/ping", 405), ("/api/me", 405), ("/nothing", 404),
                             ("/index.html", 404)):
            _, headers, json_body = raw(self.app, "POST" if status == 405 else "GET", path,
                                        body=b"{}")
            self.assertEqual(raw(self.app, "HEAD", path), (status, headers, b""), path)
            self.assertEqual(headers["Content-Length"], str(len(json_body)), path)

    def test_other_methods_on_pages_are_not_allowed(self):
        # Refused before the body is read: no 415 for text/plain, no 413 for a
        # huge body, and not a byte of it read.
        unread = {"CONTENT_TYPE": "text/plain", "CONTENT_LENGTH": str(10 * 2**20),
                  "wsgi.input": Unreadable()}
        for path in web.STATIC_FILES:
            for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
                for headers in ({}, unread, {**unread, "HTTP_ORIGIN": ELSEWHERE}):
                    status, got, body = raw(self.app, method, path, headers)
                    self.assertEqual((status, json.loads(body)), (
                        405, {"error": "method not allowed", "type": "http"}), (method, path))
                    self.assertEqual(got["Cache-Control"], "no-store")
                    self.assertEqual(got["Content-Security-Policy"], API_CSP)
        status, body = self.admin.post("/", {"code": "USD"})    # a form posted without script
        self.assertEqual(status, 405)

    def test_only_listed_paths_are_served(self):
        (self.static / "secret.txt").write_text("not for anyone", encoding="utf-8")
        (self.static / "sub").mkdir()
        (self.static / "sub" / "app.js").write_bytes(b"not this one")
        app = self.make_app()               # started with those in the folder
        for path in ("/index.html", "/static/", "/static", "/static/index.html",
                     "/static/app.js/", "/static//app.js", "/static/./app.js",
                     "/static/../web.py", "/static/%2e%2e/web.py", "/static/..%2fweb.py",
                     "/STATIC/app.js", "/static/APP.JS", "/static/app.js%00",
                     "/favicon.ico", "/robots.txt", "/static/secret.txt", "/static/sub/app.js",
                     "/app/web.py", "/web.py", "/send", "//", "/static/app.js;x",
                     "/admin/", "/admin.html", "/static/admin.html", "/ADMIN"):
            self.assert_not_found(raw(app, "GET", path), path)
            self.assertEqual(raw(app, "HEAD", path)[::2], (404, b""), path)

    def test_a_missing_page_file_is_not_found(self):
        (self.static / "app.js").unlink()
        (self.static / "style.css").unlink()
        (self.static / "style.css").mkdir()         # there, but not a file it can read
        with mock.patch("sys.stderr", io.StringIO()) as err:
            app = self.make_app()
            # Nothing is said when the app is made: a server's own first line,
            # or its error, comes first (app.serve's tests read it). Nor for the
            # API, or for the pages that are there.
            self.assertEqual(raw(app, "GET", "/api/ping")[0], 200)
            self.assertEqual(raw(app, "GET", "/")[0], 200)
            self.assertEqual(raw(app, "HEAD", "/static/icon.svg")[0], 200)
            self.assertEqual(raw(app, "POST", "/static/app.js")[0], 405)
            self.assertEqual(err.getvalue(), "")
            # The first request for a missing one names every one, once.
            for _ in range(3):
                self.assert_not_found(raw(app, "GET", "/static/style.css"), "style.css")
                self.assert_not_found(raw(app, "GET", "/static/app.js"), "app.js")
                self.assertEqual(raw(app, "HEAD", "/static/app.js")[::2], (404, b""))
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[0], f"warning: {self.static / 'app.js'} is missing: "
                                   "the screens won't load")
        self.assertRegex(lines[1], "^warning: " + re.escape(str(self.static / "style.css")) +
                         r" can't be read \(.+\): the screens won't load$")
        # No folder at all: every page is a 404, and the API works as ever.
        with mock.patch("sys.stderr", io.StringIO()) as err:
            app = self.make_app(static_dir=self.dir / "not-there")
            self.assertEqual(raw(app, "GET", "/api/ping")[0], 200)
            for path in web.STATIC_FILES:
                self.assert_not_found(raw(app, "GET", path), path)
        self.assertEqual(err.getvalue().count("is missing: the screens won't load"),
                         len(web.STATIC_FILES))
        self.assertEqual(len(err.getvalue().splitlines()), len(web.STATIC_FILES))
        # With every file there, nothing is ever said.
        (self.static / "style.css").rmdir()
        for name in ("app.js", "style.css"):
            (self.static / name).write_bytes(FILES[name])
        with mock.patch("sys.stderr", io.StringIO()) as err:
            complete = self.make_app()
            complete.warn_missing_pages()
            self.assertEqual(raw(complete, "GET", "/nothing")[0], 404)
        self.assertEqual(err.getvalue(), "")

    def test_page_files_are_read_at_start(self):
        _, before, page = raw(self.app, "GET", "/")
        (self.static / "index.html").write_bytes(b"<!doctype html>\n<title>Changed</title>\n")
        (self.static / "app.js").unlink()
        status, after, again = raw(self.app, "GET", "/")
        self.assertEqual((status, again, after["ETag"]), (200, page, before["ETag"]))
        self.assertEqual(raw(self.app, "GET", "/static/app.js")[2], FILES["app.js"])
        restarted = self.make_app()             # a restart reads them again
        status, headers, body = raw(restarted, "GET", "/")
        self.assertEqual(body, b"<!doctype html>\n<title>Changed</title>\n")
        self.assertNotEqual(headers["ETag"], before["ETag"])
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(raw(restarted, "GET", "/static/app.js")[0], 404)
        self.assertIn("app.js is missing", err.getvalue())

    def test_pages_need_no_database(self):
        def no_database(*args):
            raise AssertionError("the database was opened")
        with mock.patch.object(self.app, "conn", no_database), \
                mock.patch.object(web.A, "connect", no_database), \
                mock.patch("sys.stderr", io.StringIO()):
            for path in web.STATIC_FILES:
                status, headers, _ = raw(self.app, "GET", path)
                self.assertEqual(status, 200, path)
                self.assertEqual(raw(self.app, "HEAD", path)[0], 200, path)
                self.assertEqual(raw(self.app, "GET", path,
                                     {"HTTP_IF_NONE_MATCH": headers["ETag"]})[0], 304, path)
            self.assertEqual(raw(self.app, "GET", "/api/ping")[0], 500)     # the API does

    def test_api_headers_are_unchanged(self):
        answers = [
            (raw(self.app, "GET", "/api/ping"), 200),
            (raw(self.app, "GET", "/api/me"), 401),
            (raw(self.app, "GET", "/api/admin/users",
                 {"HTTP_COOKIE": f"__Host-session={self.admin.cookie}"}), 200),
            (raw(self.app, "POST", "/api/logout", body=b"{}"), 200),
            (raw(self.app, "POST", "/api/logout", {"HTTP_ORIGIN": ELSEWHERE}, b"{}"), 403),
            (raw(self.app, "POST", "/api/login", {"CONTENT_TYPE": "text/plain"}, b"{}"), 415),
            (raw(self.app, "GET", "/api/login"), 405),
            (raw(self.app, "POST", "/"), 405),
            (raw(self.app, "GET", "/api/nothing"), 404),
            (raw(self.app, "GET", "/nothing"), 404),
        ]
        for (status, headers, body), expected in answers:
            headers.pop("Set-Cookie", None)         # the logout's, which clears the cookie
            self.assertEqual(status, expected, body)
            self.assertEqual(headers, {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)), "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer", "Content-Security-Policy": API_CSP}, body)
            json.loads(body)


# ---------- the same over a real socket ----------

class RealServerTests(ScreensTestCase):
    """The pages and the Origin check as the bundled server and waitress (the
    production server) pass them on: each turns the request into the environ
    in its own way, and neither drops a body the app sends by mistake."""

    def dev_server(self, app):
        quiet = mock.patch.object(web._Handler, "log_message", lambda *args: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        server = web.dev_server(app, "127.0.0.1", 0)
        serving = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        serving.start()

        def stop():
            server.shutdown()
            server.server_close()
            for worker in server.workers:
                worker.join(5)
        self.addCleanup(stop)
        return server.server_port

    def waitress(self, app):
        server = S.create_server(app, host="127.0.0.1", port=0, threads=2)
        serving = threading.Thread(target=server.run, daemon=True)
        serving.start()

        def stop():
            server.task_dispatcher.shutdown()
            server.close()
            serving.join(10)
        self.addCleanup(stop)
        return int(server.effective_port)

    def assert_serves_pages(self, port):
        status, headers, body = over_socket(port, "GET", "/")
        self.assertEqual((status, body), (200, FILES["index.html"]))
        self.assertEqual((headers["Content-Type"], headers["Content-Security-Policy"],
                          headers["Cache-Control"]),
                         ("text/html; charset=utf-8", web.PAGE_CSP, "no-cache"))
        status, head, body = over_socket(port, "HEAD", "/")
        self.assertEqual((status, body, head["Content-Length"], head["ETag"]),
                         (200, b"", str(len(FILES["index.html"])), headers["ETag"]))
        for method in ("GET", "HEAD"):
            status, _, body = over_socket(port, method, "/", {"If-None-Match": headers["ETag"]})
            self.assertEqual((status, body), (304, b""), method)
        self.assertEqual(over_socket(port, "GET", "/static/icon.svg")[2], FILES["icon.svg"])
        # Both servers percent-decode the path into PATH_INFO, which the
        # in-process calls above never do.
        for path in ("/static/%2e%2e/web.py", "/static/..%2fweb.py", "/static/../web.py",
                     "/static/./app.js", "/static/%2e/app.js", "/%2e%2e/app/web.py"):
            status, _, body = over_socket(port, "GET", path)
            self.assertEqual((status, json.loads(body)),
                             (404, {"error": "not found", "type": "http"}), path)
        # An API answer to HEAD: headers only, so the connection can be reused.
        for path, expected in (("/api/ping", 405), ("/nothing", 404)):
            status, head, body = over_socket(port, "HEAD", path)
            self.assertEqual((status, body, head["Cache-Control"]), (expected, b"", "no-store"),
                             path)

    def assert_checks_the_origin(self, port):
        """The browser's Host header reaches the app as it was sent."""
        def logout(origin):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("POST", "/api/logout", b"{}", {
                    "Content-Type": "application/json", "Origin": origin,
                    "X-Forwarded-Host": f"127.0.0.1:{port + 1}"})
                return conn.getresponse().status
            finally:
                conn.close()
        self.assertEqual(logout(f"http://127.0.0.1:{port}"), 200)
        self.assertEqual(logout(f"http://127.0.0.1:{port + 1}"), 403)
        self.assertEqual(logout(f"http://localhost:{port}"), 403)

    def test_the_dev_server_serves_pages(self):
        port = self.dev_server(self.make_app(secure_cookies=False))
        self.assert_serves_pages(port)
        self.assert_checks_the_origin(port)

    @unittest.skipUnless(HAVE_WAITRESS, "waitress is not installed")
    def test_waitress_serves_pages(self):
        port = self.waitress(self.make_app(secure_cookies=False))
        self.assert_serves_pages(port)
        self.assert_checks_the_origin(port)

    def test_the_dev_server_says_where_the_screens_are(self):
        started = []

        class Server:           # stands in for the bundled server: returns at once
            def __init__(self, app, host, port):
                started.append((app, host, port))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def serve_forever(self):
                pass
        real_read = web._read_static

        def read_static(folder):        # app/static itself, played by the test's folder
            self.assertEqual(folder, web.STATIC_DIR)
            return real_read(self.static)
        (self.static / "app.js").unlink()
        db = str(self.dir / "dev.db")
        log = io.StringIO()             # stdout and stderr in the order they were written
        with mock.patch.object(web, "dev_server", Server), \
                mock.patch.object(web, "_read_static", read_static), \
                contextlib.redirect_stdout(log), mock.patch("sys.stderr", log):
            web.main(["--db", db, "--port", "8123", "--insecure-cookies"])
        # Where the screens are, then (on stderr) the page file missing at start.
        self.assertEqual(log.getvalue().splitlines(), [
            f"LYD wallet on http://127.0.0.1:8123/ (the workers' screens; the office's at "
            f"/admin; API under /api/; database {db})",
            f"warning: {self.static / 'app.js'} is missing: the screens won't load"])
        [(app, host, port)] = started
        self.assertEqual((host, port, app.secure_cookies, app.origin_schemes),
                         ("127.0.0.1", 8123, False, ("https", "http")))
        with mock.patch("sys.stderr", io.StringIO()) as err:      # not said a second time
            self.assertEqual(raw(app, "GET", "/static/app.js")[0], 404)
        self.assertEqual(err.getvalue(), "")
        app.close_connection()


# ---------- the Origin check ----------

class OriginTests(ScreensTestCase):
    def logout(self, app, headers):
        """POST /api/logout (no cookie: it changes nothing) with these headers."""
        status, _, body = raw(app, "POST", "/api/logout", headers, b"{}")
        return status, json.loads(body)

    def test_a_post_from_another_website_is_refused(self):
        _, ali = self.worker("Ali", "0911111111", lyd=100)
        _, sam = self.worker("Sam", "0922222222")
        send = {"to_phone": "0922222222", "amount": "12.5", "idempotency_key": "web-1"}
        for origin in (ELSEWHERE, "https://blog.127.0.0.1", "https://127.0.0.1:8443",
                       "http://127.0.0.1", "null"):
            self.assertEqual(ali.post("/api/send", send, headers={"HTTP_ORIGIN": origin}),
                             REFUSED, origin)
            self.assertNotIn("Set-Cookie", ali.headers)
        self.assertEqual((self.lyd(ali), self.lyd(sam)), ("100.000", "0.000"))
        self.assertEqual(ali.post("/api/logout-everywhere", headers={"HTTP_ORIGIN": ELSEWHERE}),
                         REFUSED)
        self.assertEqual(ali.get("/api/me")[0], 200)            # the session still works
        # The wallet's own page goes through.
        status, body = ali.post("/api/send", send, headers={
            "HTTP_ORIGIN": SITE, "HTTP_SEC_FETCH_SITE": "same-origin"})
        self.assertEqual((status, body["to"]), (200, "Sam"))
        self.assertEqual((self.lyd(ali), self.lyd(sam)), ("87.500", "12.500"))

    def test_what_counts_as_this_site(self):
        local = self.make_app(secure_cookies=False)     # --insecure-cookies: http too
        cases = [   # (app, Host, Origin, allowed)
            (self.app, HOST, "https://127.0.0.1", True),
            (self.app, HOST, "https://127.0.0.1:443", True),
            (self.app, "127.0.0.1:443", "https://127.0.0.1", True),
            (self.app, HOST, "HTTPS://127.0.0.1", True),
            (self.app, "WALLET.example.com", "https://wallet.EXAMPLE.com", True),
            (self.app, "wallet.example.com:8443", "https://wallet.example.com:8443", True),
            (self.app, HOST, "http://127.0.0.1", False),
            (local, HOST, "http://127.0.0.1", True),
            (local, HOST, "http://127.0.0.1:80", True),
            (local, HOST, "https://127.0.0.1", True),
            (local, "127.0.0.1:8000", "http://127.0.0.1:8000", True),
            (local, "[::1]:8000", "http://[::1]:8000", True),
            (local, "[::1]:8000", "http://[::1]", False),
            (local, "[::1]:8000", "http://[::1]:8001", False),
            (local, "127.0.0.1:8000", "http://localhost:8000", False),
            (self.app, HOST, "https://127.0.0.1:8443", False),
            (self.app, "wallet.example.com:8443", "https://wallet.example.com", False),
            (self.app, HOST, "null", False),
            (self.app, HOST, "", False),
            (self.app, HOST, "https://127.0.0.1/", False),
            (self.app, HOST, "https://127.0.0.1/x", False),
            (self.app, HOST, "https://u@127.0.0.1", False),
            (self.app, HOST, "https://127.0.0.1:99999", False),
            (self.app, HOST, "https://127.0.0.1.evil.example", False),
            (self.app, HOST, "https://evil.example/https://127.0.0.1", False),
            (self.app, HOST, "127.0.0.1", False),
            (self.app, HOST, "//127.0.0.1", False),
            (self.app, HOST, "ftp://127.0.0.1", False),
            (self.app, "", "https://", False),
            (self.app, None, "https://127.0.0.1", False),       # no Host header
            (local, None, "http://127.0.0.1", False),
        ]
        for app, host, origin, allowed in cases:
            with self.subTest(host=host, origin=origin, secure=app.secure_cookies):
                self.assertEqual(self.logout(app, {"HTTP_HOST": host, "HTTP_ORIGIN": origin}),
                                 (200, {"ok": True}) if allowed else REFUSED)

    def test_without_an_origin_sec_fetch_site_decides(self):
        for site, allowed in ((None, True), ("same-origin", True), ("none", True),
                              ("same-site", False), ("cross-site", False)):
            with self.subTest(site=site):
                self.assertEqual(self.logout(self.app, {"HTTP_SEC_FETCH_SITE": site}),
                                 (200, {"ok": True}) if allowed else REFUSED)
        # With an Origin, the Origin decides.
        self.assertEqual(self.logout(self.app, {"HTTP_ORIGIN": ELSEWHERE,
                                                "HTTP_SEC_FETCH_SITE": "same-origin"}), REFUSED)

    def test_a_bearer_token_is_not_checked(self):
        _, ali = self.worker("Ali", "0911111111", lyd=100)
        _, sam = self.worker("Sam", "0922222222")
        status, body = Client(self.app, ip="10.0.2.1").post("/api/login", {
            "phone": "0911111111", "password": password("Ali")})
        token = body["token"]
        # Sam's cookie in the same request: the Bearer token is what acts.
        foreign = {"HTTP_ORIGIN": ELSEWHERE, "HTTP_COOKIE": f"__Host-session={sam.cookie}"}
        bare = Client(self.app)
        status, body = bare.post("/api/send", {
            "to_phone": "0922222222", "amount": "10", "idempotency_key": "app-1"},
            headers={**foreign, "HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertEqual((status, body["to"]), (200, "Sam"))
        self.assertEqual((self.lyd(ali), self.lyd(sam)), ("90.000", "10.000"))
        # Any other Authorization header would act with Sam's cookie (Request.token
        # reads only "Bearer "), so it is checked like no header at all.
        for header in (f"bearer {token}", f"BEARER {token}", f"Bearer{token}", f" Bearer {token}",
                       f"Basic {token}", ""):
            status, body = bare.post("/api/send", {
                "to_phone": "0911111111", "amount": "5", "idempotency_key": f"x-{header}"},
                headers={**foreign, "HTTP_AUTHORIZATION": header})
            self.assertEqual((status, body), REFUSED, header)
        self.assertEqual((self.lyd(ali), self.lyd(sam)), ("90.000", "10.000"))
        environ = {"REQUEST_METHOD": "GET", "HTTP_AUTHORIZATION": f"bearer {token}",
                   "HTTP_COOKIE": f"__Host-session={sam.cookie}"}
        self.assertIsNone(web._bearer(environ))
        self.assertEqual(web.Request(environ, False, web.SECURE_COOKIE).token, sam.cookie)
        self.assertEqual(web._bearer({"HTTP_AUTHORIZATION": f"Bearer {token} "}), token)

    def test_a_login_from_another_website_is_refused(self):
        status, made = self.admin.post("/api/admin/users", {"name": "Ali",
                                                            "phone": "0911111111"})
        setup = {"phone": "0911111111", "code": made["setup_code"], "password": password("Ali")}
        login = {"phone": "0911111111", "password": password("Ali")}
        elsewhere = ({"HTTP_ORIGIN": ELSEWHERE}, {"HTTP_ORIGIN": "https://127.0.0.1:8443"},
                     {"HTTP_SEC_FETCH_SITE": "cross-site"}, {"HTTP_SEC_FETCH_SITE": "same-site"})
        for headers in elsewhere:
            client = Client(self.app)
            self.assertEqual(client.post("/api/setup-password", setup, headers=headers), REFUSED)
            self.assertNotIn("Set-Cookie", client.headers)
        page = Client(self.app)         # the code was not used: it still works
        self.assertEqual(page.post("/api/setup-password", setup, headers={"HTTP_ORIGIN": SITE}),
                         (200, {"ok": True}))
        for headers in elsewhere:
            client = Client(self.app)
            self.assertEqual(client.post("/api/login", login, headers=headers), REFUSED)
            self.assertNotIn("Set-Cookie", client.headers)
        self.assertEqual(self.count("SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                                    made["id"]), 1)     # the page's only

    def test_the_origin_is_checked_before_the_body(self):
        for headers in ({"CONTENT_TYPE": "text/plain"},
                        {"CONTENT_TYPE": "application/x-www-form-urlencoded"},
                        {"CONTENT_LENGTH": str(10 * 2**20)}, {"CONTENT_LENGTH": "-1"}):
            status, _, body = raw(self.app, "POST", "/api/logout-everywhere", {
                "HTTP_ORIGIN": ELSEWHERE, "wsgi.input": Unreadable(), **headers})
            self.assertEqual((status, json.loads(body)), REFUSED, headers)
        # From this site the body rules answer as before.
        status, _, _ = raw(self.app, "POST", "/api/logout", {
            "HTTP_ORIGIN": SITE, "CONTENT_TYPE": "text/plain"}, b"{}")
        self.assertEqual(status, 415)
        status, _, _ = raw(self.app, "POST", "/api/logout", {
            "HTTP_ORIGIN": SITE, "CONTENT_LENGTH": str(10 * 2**20), "wsgi.input": Unreadable()})
        self.assertEqual(status, 413)

    def test_other_methods_and_paths_are_checked_too(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            status, _, body = raw(self.app, method, "/api/logout-everywhere",
                                  {"HTTP_ORIGIN": ELSEWHERE})
            self.assertEqual((status, json.loads(body)), REFUSED, method)
            status, _, _ = raw(self.app, method, "/api/logout-everywhere", {"HTTP_ORIGIN": SITE})
            self.assertEqual(status, 405, method)
        status, _, body = raw(self.app, "POST", "/api/no-such-thing", {"HTTP_ORIGIN": ELSEWHERE},
                              b"{}")
        self.assertEqual((status, json.loads(body)), REFUSED)

    def test_reads_are_not_checked(self):
        _, ali = self.worker("Ali", "0911111111", lyd=100)
        foreign = {"HTTP_ORIGIN": ELSEWHERE, "HTTP_SEC_FETCH_SITE": "cross-site"}
        status, me = ali.get("/api/me", headers=foreign)
        self.assertEqual((status, me["balance"]["lyd"]), (200, "100.000"))
        self.assertEqual(raw(self.app, "GET", "/", foreign)[0], 200)
        self.assertEqual(raw(self.app, "HEAD", "/", foreign)[0], 200)
        self.assertEqual(raw(self.app, "HEAD", "/api/ping", foreign)[0], 405)

    def test_x_forwarded_host_is_never_this_site(self):
        for app in (self.app, self.make_app(trust_proxy=True)):
            self.assertEqual(self.logout(app, {
                "HTTP_X_FORWARDED_HOST": "evil.example", "HTTP_X_FORWARDED_FOR": "203.0.113.5",
                "HTTP_ORIGIN": "https://evil.example"}), REFUSED)


# ---------- the token in the login answer ----------

class TokenTests(ScreensTestCase):
    BROWSERS = ({"HTTP_ORIGIN": SITE}, {"HTTP_SEC_FETCH_SITE": "same-origin"},
                {"HTTP_ORIGIN": SITE, "HTTP_SEC_FETCH_SITE": "same-origin"})

    def assert_phone_app(self, answer, client):
        """A client that isn't a browser gets the token, which works as Bearer."""
        status, body = answer
        self.assertEqual((status, set(body)), (200, {"ok", "token"}), body)
        self.assertEqual(client.cookie, body["token"])      # the cookie is set for everyone
        status, me = Client(self.app).get("/api/me", headers={
            "HTTP_AUTHORIZATION": f"Bearer {body['token']}"})
        self.assertEqual((status, me["name"]), (200, "Ali"))

    def test_a_browser_login_gets_no_token(self):
        self.worker("Ali", "0911111111")
        login = {"phone": "0911111111", "password": password("Ali")}
        for headers in self.BROWSERS:
            page = Client(self.app)
            self.assertEqual(page.post("/api/login", login, headers=headers), (200, {"ok": True}))
            self.assertIn("HttpOnly", page.headers["Set-Cookie"])
            status, me = page.get("/api/me")                # the cookie is the session
            self.assertEqual((status, me["name"]), (200, "Ali"), headers)
        app = Client(self.app)
        self.assert_phone_app(app.post("/api/login", login), app)

    def test_a_browser_setup_gets_no_token(self):
        ali_id, _ = self.worker("Ali", "0911111111")

        def setup(client, headers=None):
            status, made = self.admin.post(f"/api/admin/users/{ali_id}/setup-code", {})
            self.assertEqual(status, 200, made)
            return client.post("/api/setup-password", {
                "phone": "0911111111", "code": made["setup_code"],
                "password": password("Ali")}, headers=headers)
        for headers in self.BROWSERS:
            page = Client(self.app)
            self.assertEqual(setup(page, headers), (200, {"ok": True}))
            self.assertEqual(page.get("/api/me")[0], 200, headers)
        app = Client(self.app)
        self.assert_phone_app(setup(app), app)


class ShownWorkerTests(ScreensTestCase):
    """A send or an order names the worker its page shows (from_phone). The
    tabs of a browser share one cookie: after another worker logged in in
    another tab, the session is theirs, and a tab still showing the first
    worker must not pay from the second's account."""

    def test_a_money_request_for_another_worker_is_refused(self):
        ali_id, _ = self.worker("Ali", "0911111111", lyd=100)
        sam_id, _ = self.worker("Sam", "0922222222")
        bob_id, bob = self.worker("Bob", "0933333333", lyd=100)
        refused = (401, {"error": "logged in as another worker; please log in",
                         "type": "NotLoggedIn"})
        send = {"to_phone": "0922222222", "amount": "10", "idempotency_key": "web-1",
                "from_phone": "+218911111111"}
        self.assertEqual(bob.post("/api/send", send), refused)
        order = {"quote_id": "q", "beneficiary_name": "Ahmed", "beneficiary_id": "A1",
                 "from_phone": "+218911111111"}
        self.assertEqual(bob.post("/api/orders", order), refused)
        conn = A.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual((L.balance(conn, ali_id), L.balance(conn, bob_id),
                          L.balance(conn, sam_id)), (100_000, 100_000, 0))
        # The worker's own phone, however written, or none (another client).
        for from_phone in ("0933333333", "+218 93 333 3333", None):
            with self.subTest(from_phone=from_phone):
                body = {**send, "idempotency_key": f"web-{from_phone}"}
                if from_phone is None:
                    del body["from_phone"]
                else:
                    body["from_phone"] = from_phone
                status, answer = bob.post("/api/send", body)
                self.assertEqual(status, 200, answer)
        self.assertEqual(L.balance(conn, bob_id), 70_000)


# ---------- English by default ----------

class LanguageTests(ScreensTestCase):
    def language(self, user_id, db_path=None):
        conn = A.connect(db_path or self.db_path)
        try:
            return conn.execute("SELECT language FROM users WHERE id = ?",
                                (user_id,)).fetchone()[0]
        finally:
            conn.close()

    def test_new_users_get_english_by_default(self):
        made = {}
        for name, phone, language in (("Ali", "0911111111", None), ("Sam", "0922222222", ""),
                                      ("Huda", "0933333333", "ar"), ("Rami", "0944444444", "fr"),
                                      ("Lina", "0955555555", "en")):
            body = {"name": name, "phone": phone}
            if language is not None:
                body["language"] = language
            status, made[name] = self.admin.post("/api/admin/users", body)
            self.assertEqual(status, 200, made[name])
        users = {u["name"]: u["language"] for u in self.admin.get("/api/admin/users")[1]["users"]}
        self.assertEqual(users, {"Admin": "en", "Ali": "en", "Sam": "en", "Huda": "ar",
                                 "Rami": "fr", "Lina": "en"})
        ali = Client(self.app, ip="10.0.3.1")
        self.assertEqual(ali.post("/api/setup-password", {
            "phone": "0911111111", "code": made["Ali"]["setup_code"],
            "password": password("Ali")})[0], 200)
        self.assertEqual(ali.get("/api/me")[1]["language"], "en")
        status, answer = self.admin.post("/api/admin/users", {
            "name": "Otto", "phone": "0912222222", "language": "de"})
        self.assertEqual((status, answer), (400, {"error": "language must be one of ar, en, fr",
                                                  "type": "LedgerError"}))
        users = self.admin.get("/api/admin/users")[1]["users"]
        self.assertNotIn("Otto", {u["name"] for u in users})

    def test_the_code_defaults_are_english(self):
        conn = A.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(self.language(L.create_user(conn, "customer", "Ali", "0911111111",
                                                     admin_id=1)), "en")
        new_db = str(self.dir / "new.db")
        other = A.connect(new_db)
        self.addCleanup(other.close)
        admin = A.create_first_admin(other, "Huda", "0920000000", "Office-Pass-2026")
        self.assertEqual(self.language(admin, new_db), "en")
        self.assertEqual(L.LANGUAGES, ("ar", "en", "fr"))
        # The table's own default is left as it was: it is never used (below).
        self.assertIn("language TEXT NOT NULL DEFAULT 'ar'", L.SCHEMA)

    def test_the_users_sql_default_is_never_used(self):
        found = 0
        for path in sorted((ROOT / "app").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+users\b\s*(\(([^)]*)\))?",
                                     source, re.IGNORECASE):
                found += 1
                self.assertIsNotNone(match.group(2), f"{path.name}: {match.group(0)!r} names "
                                                     "no columns")
                self.assertIn("language", match.group(2), f"{path.name}: {match.group(0)!r}")
        self.assertGreaterEqual(found, 1)


# ---------- the answers the screens match their texts to ----------

class ErrorTextTests(ScreensTestCase):
    """The screens choose what to show by an answer's status, type and exact
    text (ERROR_RULES in app/static/app.js, DESIGN section 2). Changing one of
    these texts means changing ERROR_RULES too."""

    def expect(self, rule, answer, status, kind, error):
        got, body = answer
        self.assertEqual((got, body.get("type")), (status, kind), f"{rule}: {body}")
        if isinstance(error, re.Pattern):
            self.assertRegex(body["error"], error, rule)
        else:
            self.assertEqual(body["error"], error, rule)

    def test_the_errors_the_screens_rely_on(self):
        expect = self.expect
        # The app's own limiters, with their own texts, out of the way until used.
        for limiter in (self.app.login_limiter, self.app.account_limiter,
                        self.app.worker_limiter):
            limiter.limit = 1000
        ali_id, ali = self.worker("Ali", "0911111111", lyd=1000)
        sam_id, sam = self.worker("Sam", "0922222222")

        def login(phone, secret, ip="10.0.2.9"):
            return Client(self.app, ip=ip).post("/api/login", {"phone": phone,
                                                               "password": secret})

        # Sessions (R1, R2) and the admin on the workers' screens (R3).
        expect("R2/S1", Client(self.app).get("/api/me"), 401, "NotLoggedIn", "please log in")
        old = Client(self.app, ip="10.0.2.2")
        self.assertEqual(old.post("/api/login", {"phone": "0922222222",
                                                 "password": password("Sam")})[0], 200)
        self.sql("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                 (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                 A._sha256(old.cookie))
        expect("R2/S2", old.get("/api/me"), 401, "NotLoggedIn",
               "session expired, please log in again")
        iman_id, iman = self.worker("Iman", "0933333333")
        self.sql("UPDATE users SET blocked = 1 WHERE id = ?", iman_id)  # by hand: sessions stay
        expect("R1/S3", iman.get("/api/me"), 401, "NotLoggedIn",
               "this account can't be used; please contact the office")
        expect("R8", login("0933333333", password("Iman")), 403, "AccountBlocked",
               "this account is blocked; please contact the office")
        # What a worker blocked by the office really sees: block_user ends their
        # sessions, so "please log in" (S1), then the login is refused (R8).
        jala_id, jala = self.worker("Jala", "0944444444")
        conn = A.connect(self.db_path)
        L.block_user(conn, jala_id, admin_id=1, reason="checking a transfer")
        conn.close()
        expect("block_user: S1", jala.get("/api/me"), 401, "NotLoggedIn", "please log in")
        expect("block_user: R8", login("0944444444", password("Jala")), 403, "AccountBlocked",
               "this account is blocked; please contact the office")
        expect("R3", self.admin.get("/api/me"), 403, "PermissionDenied",
               "this is for workers' accounts")
        expect("R4", ali.post("/api/send", {"to_phone": "0922222222", "amount": "1",
                                            "idempotency_key": "r4"},
                              headers={"HTTP_ORIGIN": ELSEWHERE}),
               403, "http", "refused: sent from another website")

        # Logging in and choosing a password (R5-R10, R14-R18).
        login_failed = "wrong phone number or password, or too many wrong tries: wait 15 minutes"
        expect("R5", login("0911111111", "not-the-password"), 401, "LoginFailed", login_failed)
        expect("R5 (unknown)", login("0919999999", "not-the-password"), 401, "LoginFailed",
               login_failed)
        new_password = {"phone": "0911111111", "code": "ABCD-EFGH-JKLM",
                        "password": "a-new-password-1"}
        expect("R6", Client(self.app).post("/api/setup-password", new_password), 401,
               "LoginFailed", "wrong phone number or code")
        hana_id, _ = self.worker("Hana", "0955555555")
        self.assertEqual(self.admin.post(f"/api/admin/users/{hana_id}/deactivate",
                                         {"reason": "left the company"})[0], 200)
        expect("R7", login("0955555555", password("Hana")), 401, "LoginFailed",
               "this account is closed")
        self.app.login_limiter.limit = 1
        login("0911111111", "not-the-password", ip="10.0.2.10")
        expect("R9", login("0911111111", "not-the-password", ip="10.0.2.10"), 429, "http",
               "too many tries from this connection; wait a few minutes")
        self.app.login_limiter.limit = 1000
        self.app.account_limiter.limit = 1      # Ali's setup counted once already
        expect("R10", login("0911111111", password("Ali"), ip="10.0.2.11"), 429, "http",
               "too many logins to this account; wait a few minutes")
        self.app.account_limiter.limit = 1000
        for rule, secret, error in (
                ("R14", "short-pw1", "password must be at least 10 characters"),
                ("R15", "x" * 129, "password is too long"),
                ("R16", "abababababab", "password is too simple"),
                ("R17", "password123", "password is too common; choose another"),
                ("R18", "my-911111111-pass", "password can't contain your phone number")):
            expect(rule, Client(self.app).post("/api/setup-password",
                                               dict(new_password, password=secret)),
                   400, "LedgerError", error)

        # Changing the password (R11-R13).
        fadi_id, fadi = self.worker("Fadi", "0912222222")
        real_hash = A.hash_password

        def changed_meanwhile(secret):      # someone else sets it while this one runs
            made = real_hash(secret)
            self.sql("UPDATE users SET password_hash = ? WHERE id = ?",
                     real_hash("set-elsewhere-1"), fadi_id)
            return made
        change = {"old_password": password("Fadi"), "new_password": "fadi-wallet-2027"}
        with mock.patch.object(A, "hash_password", changed_meanwhile):
            expect("R13", fadi.post("/api/change-password", change), 409, "Conflict",
                   "the password was changed meanwhile; try again")
        wrong = dict(change, old_password="not-my-password")
        expect("R12", fadi.post("/api/change-password", wrong), 403, "WrongPassword",
               "current password is wrong")
        for _ in range(A.MAX_FAILED_LOGINS - 1):
            fadi.post("/api/change-password", wrong)
        expect("R11", fadi.post("/api/change-password",
                                dict(change, old_password="set-elsewhere-1")),
               429, "AccountLocked",
               re.compile(r"^too many wrong tries; try again after \d\d:\d\d UTC$"))

        # Finding a colleague and sending money (R19-R25, R28).
        self.app.worker_limiter.limit = 1
        self.assertEqual(sam.get("/api/recipient?phone=0911111111")[0], 200)
        expect("R19", sam.get("/api/recipient?phone=0911111111"), 429, "http",
               "too many requests; wait a minute")
        self.app.worker_limiter.limit = 1000
        for phone, error in (("123", "phone number must be a Libyan mobile, e.g. 091 234 5678"),
                             ("abc", "phone number: digits only"),
                             ("", "phone number: digits only")):
            expect(f"R20 {phone!r}", ali.get(f"/api/recipient?phone={urlquote(phone)}"), 400,
                   "LedgerError", error)
        for phone in ("0957777777", "0910000000", "0944444444"):     # unknown, admin, blocked
            expect(f"R21 {phone}", ali.get(f"/api/recipient?phone={phone}"), 404,
                   "UnknownAccount", "no worker with that phone number")
        expect("R22", ali.get("/api/recipient?phone=%2B218911111111"), 400, "LedgerError",
               "cannot send to yourself")

        def send(amount, to="0922222222"):
            return ali.post("/api/send", {"to_phone": to, "amount": amount,
                                          "idempotency_key": f"web-{amount}-{to}"})
        expect("R21 (send)", send("1", to="0957777777"), 404, "UnknownAccount",
               "no worker with that phone number")
        expect("R22 (send)", send("1", to="0911111111"), 400, "LedgerError",
               "cannot send to yourself")
        expect("R20 (send)", send("1", to="123"), 400, "LedgerError",
               "phone number must be a Libyan mobile, e.g. 091 234 5678")
        for amount, error in (("abc", "not a number"), ("١٢٫٥", "not a number"),
                              ("0", "amount must be a positive whole number of dirhams"),
                              ("-5", "amount must be a positive whole number of dirhams")):
            expect(f"R23 {amount!r}", send(amount), 400, "InvalidAmount", error)
        expect("R24", send("2000000000"), 400, "InvalidAmount", "amount too large")
        expect("R25", send("1.2345"), 400, "InvalidAmount", "at most 3 decimal places")
        expect("R28", send("5000"), 409, "InsufficientFunds", "balance too low")
        self.assertEqual(self.lyd(ali), "1,000.000")        # every one a refusal

        # Prices and quotes (R23, R24, R26, R27, R29).
        for path, body in (("/api/admin/currencies", {"code": "USD"}),
                           ("/api/admin/prices", {"currency": "USD", "rate": "7.85"}),
                           ("/api/admin/currencies", {"code": "EUR"}),      # no price yet
                           ("/api/admin/currencies", {"code": "JPY"}),
                           ("/api/admin/prices", {"currency": "JPY", "rate": "0.000001"})):
            self.assertEqual(self.admin.post(path, body)[0], 200, path)

        def quote(amount, currency="USD", client=ali):
            return client.post("/api/quote", {"currency": currency, "amount": amount})
        for amount, error in (("abc", "amount is not a number"),
                              ("0", "amount must be more than zero")):
            expect(f"R23 {amount!r} (quote)", quote(amount), 400, "InvalidAmount", error)
        expect("R24 (quote)", quote("10000000000000"), 400, "InvalidAmount",
               "amount is too large")
        expect("R24 (cost)", quote("999999999999"), 400, "InvalidAmount", "amount too large")
        expect("R26", quote("1.234"), 400, "InvalidAmount", "amount: at most 2 decimal places")
        expect("R27", quote("0.01", "JPY"), 400, "InvalidAmount", "amount too small")
        expect("R29", quote("10", "xx"), 400, "LedgerError", "XX is not available")
        expect("R29 (no price)", quote("10", "EUR"), 400, "LedgerError", "no price set for EUR")

        # Orders (R28, R30-R35).
        def order(quote_id, name="Ahmed Ali", id_number="A123"):
            return ali.post("/api/orders", {"quote_id": quote_id, "beneficiary_name": name,
                                            "beneficiary_id": id_number})
        status, big = quote("1000")             # 7,850 LYD: more than Ali has
        self.assertEqual(status, 200, big)
        expect("R28 (order)", order(big["quote_id"]), 409, "InsufficientFunds",
               "balance too low")
        _, late = quote("10")
        eleven_minutes_on = datetime.now(timezone.utc) + timedelta(minutes=11)
        with mock.patch.object(O, "_now", lambda now=None: eleven_minutes_on):
            expect("R30", order(late["quote_id"]), 409, "QuoteExpired",
                   "price expired, get a new one")
        _, first = quote("10")
        self.assertEqual(order(first["quote_id"])[0], 200)
        expect("R32", order(first["quote_id"], name="Someone Else"), 409, "Conflict",
               "this quote was already used for an order to another beneficiary; "
               "get a new quote")
        _, voided = quote("10")
        self.assertEqual(self.admin.post("/api/admin/prices", {
            "currency": "USD", "rate": "7.90", "void_quotes": True})[0], 200)
        expect("R31", order(voided["quote_id"]), 409, "QuoteExpired",
               "the price was corrected, get a new one")
        _, sams = quote("10", client=sam)
        for quote_id in ("no-such-quote", sams["quote_id"]):        # unknown, another's
            expect(f"R33 {quote_id}", order(quote_id), 404, "NotFound", "quote not found")
        expect("R34", ali.post("/api/orders", {"quote_id": first["quote_id"],
                                               "beneficiary_name": "Ahmed Ali"}),
               400, "http", "'beneficiary_id' is required")
        expect("R34 (login)", Client(self.app).post("/api/login", {"password": "x"}), 400,
               "http", "'phone' is required")
        expect("R35", order(first["quote_id"], name="x" * 101), 400, "http",
               "'beneficiary_name' is too long (at most 100 characters)")

        # The server's own failure (R36): nothing internal shown.
        with mock.patch.object(web.api, "me", side_effect=RuntimeError("a detail")), \
                mock.patch("sys.stderr", io.StringIO()):
            expect("R36", ali.get("/api/me"), 500, "internal", "internal error")


# ---------- what the README says about them ----------

class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.flat = " ".join(self.readme.split())

    def test_the_readme_names_every_page_path(self):
        for path in web.STATIC_FILES:
            self.assertIn(f"`{path}`", self.readme, path)

    def test_the_readme_says_english_is_the_default(self):
        self.assertIn("`en` (default)", self.readme)
        self.assertNotIn("`ar` (default)", self.readme)

    def test_the_readme_states_the_origin_rule(self):
        for said in ("its `Origin` must be `https://` and the request's `Host` header",
                     "proxy_set_header Host $http_host;", "--insecure-cookies",
                     "`Authorization: Bearer`", "never `X-Forwarded-Host`",
                     "only to a client that sends neither `Origin` nor `Sec-Fetch-Site`"):
            self.assertIn(said, self.flat)

    def test_the_readme_says_how_to_run_the_browser_tests(self):
        for said in (".venv/bin/pip install playwright", ".venv/bin/python -m unittest "
                     "tests.test_browser", "WALLET_TEST_CHROMIUM"):
            self.assertIn(said, self.flat)


if __name__ == "__main__":
    unittest.main()
