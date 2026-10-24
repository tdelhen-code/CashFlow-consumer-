"""The office's screens' files in app/static (admin.html, admin.js,
admin.css), read as text without a browser: the rules the workers' page
keeps (tests/test_pages.py) that the page's Content-Security-Policy and
tests/test_admin_browser.py rely on."""
import re
import unittest

from tests.test_pages import Page, balanced, function_body, js_calls, literal_arg, read

SCREENS = ("loading", "offline", "login", "setup", "overview", "workers", "worker", "orders",
           "prices", "suppliers", "supplier", "log", "account")
NAV = ("overview", "workers", "orders", "prices", "suppliers", "log", "account")


class AdminPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("admin.html")
        cls.page = Page(cls.text)
        cls.code = read("admin.js")

    def test_the_head(self):
        self.assertEqual(self.page.doctype, "doctype html")
        self.assertEqual(self.page.all("html"), [{"lang": "en", "dir": "ltr"}])
        self.assertEqual([a["href"] for a in self.page.all("link", rel="stylesheet")],
                         ["/static/style.css", "/static/admin.css"])
        self.assertEqual(self.page.all("script"), [{"src": "/static/admin.js", "defer": None}])

    def test_nothing_inline_and_nothing_from_elsewhere(self):
        self.assertNotIn("<style", self.text)
        self.assertNotRegex(self.text, r"\son[a-z]+=")
        self.assertNotIn(" style=", self.text)
        self.assertNotRegex(self.text, r"(href|src|action)=\"(https?:)?//")

    def test_every_form_is_posted_nowhere_if_the_script_fails(self):
        for form in self.page.all("form"):
            with self.subTest(form["id"]):
                self.assertEqual((form["method"], form["action"]), ("post", "/admin"))
                self.assertIn("novalidate", form)

    def test_every_control_has_an_id_and_every_input_a_label(self):
        labels = {a["for"] for a in self.page.all("label")}
        for tag in ("input", "select", "button"):
            for control in self.page.all(tag):
                with self.subTest(tag=tag, control=control):
                    self.assertIn("id", control)
                    if tag != "button":
                        self.assertIn(control["id"], labels)
        for button in self.page.all("button"):
            self.assertIn(button.get("type"), ("button", "submit"), button["id"])

    def test_one_section_per_screen_and_only_the_loading_one_shown(self):
        sections = self.page.all("section")
        self.assertEqual([s["data-screen"] for s in sections], list(SCREENS))
        self.assertEqual([s["id"] for s in sections], [f"screen-{s}" for s in SCREENS])
        self.assertEqual([s["data-screen"] for s in sections if "hidden" not in s], ["loading"])
        screens = re.search(r'var SCREENS = \[([^\]]*)\]', self.code).group(1)
        self.assertEqual(re.findall(r'"(\w+)"', screens), list(SCREENS))

    def test_the_nav(self):
        links = [a for a in self.page.all("a") if "data-nav" in a]
        self.assertEqual([(a["data-nav"], a["href"]) for a in links],
                         [(name, f"#/{name}") for name in NAV])

    def test_message_lines_start_hidden(self):
        for element in self.page.all("p"):
            if element.get("id") == "offline-text":     # its screen is shown only with it
                continue
            if "error" in element.get("class", "").split() or element.get("role") == "status":
                with self.subTest(element["id"]):
                    self.assertIn("hidden", element)
                    self.assertEqual(element.get("tabindex"), "-1" if "error" in
                                     element["class"].split() else None)

    def test_elements_are_found_by_written_out_ids(self):
        ids = self.page.by_id()
        calls = [literal_arg(call) for call in js_calls(self.code, "byId")]
        for name in filter(None, calls):
            with self.subTest(name):
                self.assertIn(name, ids)
        # Ids put together: a screen's section, and the setup code boxes' parts.
        for screen in SCREENS:
            self.assertIn(f"screen-{screen}", ids)
        for box in ("code", "worker-code"):
            for part in ("value", "text", "title"):
                self.assertIn(f"{box}-{part}", ids)
        self.assertEqual(self.code.count("getElementById"), 1)

    def test_no_html_sinks_and_nothing_from_elsewhere(self):
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                    "eval(", "new Function", 'setAttribute("style"', ".style.",
                    'setAttribute("on', "http://", "https://", "javascript:",
                    "localStorage", "sessionStorage", "import(", "importScripts",
                    "?.", "??", "replaceAll(", "`"):
            with self.subTest(bad):
                self.assertNotIn(bad, self.code)
        self.assertIn('"use strict";', self.code)

    def test_one_fetch_in_the_api_helper_and_no_timers_that_call_it(self):
        self.assertEqual(len(re.findall(r"\bfetch\(", self.code)), 1)
        self.assertRegex(function_body(self.code, "api"), r"\bfetch\(")
        self.assertNotRegex(self.code, r"\bmode\s*[:=]|referrerPolicy|referrer\s*:")
        self.assertIn('credentials: "same-origin"', self.code)
        self.assertNotRegex(self.code, r"\bsetInterval\(|\brequestAnimationFrame\(")
        [timer] = re.findall(r"\bsetTimeout\(", self.code)
        start = self.code.index("setTimeout(")
        self.assertIn("controller.abort()", balanced(self.code, start + len("setTimeout")))

    def test_every_money_request_carries_a_key_made_once(self):
        # A payment is confirmed in the question box: its key is made when the
        # box opens, so pressing confirm again after a lost answer reuses it.
        questions = []
        for match in re.finditer(r"var key = newKey\(\);\s*(?:var [^;]+;\s*)*ask\(", self.code):
            questions.append(balanced(self.code, match.end() - 1))
        for path in ('"/api/admin/deposit"', '"/api/admin/withdraw"', '"/api/admin/reverse"',
                     '"/pay"'):
            with self.subTest(path):
                self.assertEqual(self.code.count(path), 1)
                [question] = [q for q in questions if path in q]
                self.assertIn("idempotency_key: key", question)
                self.assertIn("key: key", question.replace("idempotency_key: key", ""))

    def test_style_sheet(self):
        css = read("admin.css")
        self.assertNotIn("@import", css)
        self.assertNotIn("http", css)
        self.assertNotIn("url(", css)


if __name__ == "__main__":
    unittest.main()
