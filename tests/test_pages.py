"""The workers' screens' files in app/static, read as text without a browser:
the page's markup (with what a screen reader and a phone's keyboard need),
the strings file's shape and keys, and the rules app.js keeps (no HTML
sinks, no outside addresses, a quote only on a tap). They are what the
page's Content-Security-Policy and tests/test_browser.py rely on. Nothing
here imports app.web, so these run before the server's page serving is in."""
import json
import re
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
FILES = ("index.html", "app.js", "strings.js", "style.css", "icon.svg")

SCREENS = ("loading", "offline", "login", "setup", "home", "send", "currency", "orders",
           "account")
NAV = ("home", "send", "currency", "orders", "account")

# The ids tests/test_browser.py relies on.
CONTRACT_IDS = """
    notice nav offline-text offline-retry
    login-form login-phone login-password login-submit login-error login-to-setup
    setup-form setup-phone setup-code setup-password setup-password2 setup-submit
    setup-error setup-to-login
    home-name home-balance home-updated home-refresh home-error statement-list
    statement-empty statement-more
    send-balance send-form send-phone send-amount send-note send-next send-error
    send-confirm-name send-confirm-phone send-confirm-amount send-confirm-note
    send-confirm send-edit send-cancel send-confirm-error
    send-done-text send-done-balance send-done-ok send-again
    prices-list prices-empty prices-error prices-refresh
    quote-form quote-currency quote-amount quote-submit quote-error quote-notice
    quote-summary quote-rate quote-until quote-left
    order-form order-name order-id order-submit order-error order-new-price order-cancel
    receipt-code receipt-copy receipt-details receipt-cost receipt-status receipt-balance
    receipt-orders receipt-new
    orders-list orders-empty orders-error orders-refresh orders-more
    account-name account-phone password-form password-old password-new password-new2
    password-submit password-error password-done logout logout-all logout-all-confirm
    logout-all-yes logout-all-no account-error
""".split()

# Message elements: hidden while empty, and holding exactly one key's text.
MESSAGE_IDS = ("notice", "login-error", "setup-error", "home-error", "send-error",
               "send-confirm-error", "prices-error", "quote-error", "quote-notice",
               "order-error", "orders-error", "password-error", "password-done",
               "account-error")

# The keys tests/test_browser.py relies on, with their placeholders: the
# texts it checks, and every error line (R1 to R40 in app.js), which have
# none and are listed in the order of app.js's rules.
CONTRACT_KEYS = {
    "money.lyd": {"amount"}, "home.hello": {"name"}, "send.balance": {"amount"},
    "send.done_balance": {"amount"}, "receipt.balance": {"amount"},
    "send.confirm": {"amount"}, "send.done_text": {"amount", "name"},
    "currency.price_line": {"currency", "rate"},
    "quote.summary": {"amount", "currency", "cost"}, "order.submit": {"cost"},
    "setup.done": set(), "home.empty": set(), "entry.sent": set(), "entry.received": set(),
    "currency.empty": set(), "quote.expired_wait": set(), "orders.empty": set(),
    "order.status.placed": set(), "order.status.assigned": set(),
    "order.status.paid": set(), "order.status.cancelled": set(),
    "password.done": set(), "logout.done": set(), "logout.all_done": set(),
    "check.required": set(), "check.amount_lyd": set(), "check.amount_foreign": set(),
    "check.passwords_differ": set(), "check.password_short": set(),
    "send.unsure": set(), "send.cancelled_check": set(), "order.unsure": set(),
    "order.cancelled_check": set(), "setup.unsure": set(), "password.unsure": set(),
}
ERROR_KEYS = """
    account_unusable session_ended workers_only other_site login_failed setup_failed
    account_closed account_blocked too_many_tries too_many_logins locked wrong_password
    changed_meanwhile password_short password_long password_simple password_common
    password_phone too_many_requests phone_format unknown_number send_to_self
    amount_format amount_too_large amount_decimals amount_decimals_foreign
    amount_too_small balance_too_low currency_unavailable quote_expired price_corrected
    quote_used quote_not_found fields_missing field_too_long server unavailable network
    timeout unknown
""".split()
CONTRACT_KEYS.update({"error." + name: set() for name in ERROR_KEYS})

KEY = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def strings_json(text):
    """The object after `window.STRINGS =`, as strings.js must write it:
    strict JSON, from the first { after the marker to the last }."""
    start = text.index("{", text.index("window.STRINGS ="))
    return json.loads(text[start:text.rindex("}") + 1])


def placeholders(text):
    return set(PLACEHOLDER.findall(text))


class Page(HTMLParser):
    """index.html as a list of elements with their attributes (and the ids
    of the elements each one is inside), its text and the element each piece
    of text is in."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.doctype, self.elements, self.inside, self.texts = None, [], [], []
        self.stack = []         # the open elements: (tag, id or None)
        self.feed(text)
        self.close()

    def handle_decl(self, decl):
        self.doctype = decl

    def handle_starttag(self, tag, attrs):
        self.handle_startendtag(tag, attrs)
        if tag not in VOID:
            self.stack.append((tag, dict(attrs).get("id")))

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        self.inside.append({name for _, name in self.stack if name})

    def handle_endtag(self, tag):
        if tag in [t for t, _ in self.stack]:
            while self.stack.pop()[0] != tag:
                pass

    def handle_data(self, data):
        if data.strip():
            self.texts.append((self.stack[-1][0] if self.stack else None, data.strip()))

    def all(self, tag=None, **attrs):
        return [a for t, a in self.elements if (tag is None or t == tag)
                and all(a.get(k.replace("_", "-")) == v for k, v in attrs.items())]

    def by_id(self):
        return {a["id"]: (t, a) for t, a in self.elements if "id" in a}

    def within(self, container_id):
        """The elements inside the element with this id, in page order."""
        return [element for element, inside in zip(self.elements, self.inside)
                if container_id in inside]


def js_calls(code, name):
    """Every call of the function `name` in `code` (not a method, not its
    definition), as the text from the opening parenthesis on."""
    out = []
    for match in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"\(", code):
        if code[:match.start()].endswith("function "):
            continue
        out.append(code[match.end() - 1:])
    return out


def literal_arg(call):
    """The string literal that is a call's first argument, or None."""
    match = re.match(r'\(\s*"([^"\\]*)"\s*[,)]', call)
    return match.group(1) if match else None


def balanced(text, start):
    """The text from the bracket at `start` to its matching one, skipping
    string literals and comments (app.js has no template literals)."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack, i, quote = [], start, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                break
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 1
        elif c in "\"'":
            quote = c
        elif c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return text[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced from {text[start:start + 40]!r}")


def function_body(code, name):
    match = re.search(r"function " + re.escape(name) + r"\([^)]*\)\s*\{", code)
    if not match:
        raise AssertionError(f"no function {name} in app.js")
    return balanced(code, match.end() - 1)


def block(code, title):
    """The part of app.js under a `// ---------- <title> ----------` line, up to
    the next such line."""
    marker = f"// ---------- {title} ----------"
    if marker not in code:
        raise AssertionError(f"app.js has no section {marker!r}")
    return code.split(marker, 1)[1].split("// ---------- ", 1)[0]


class FileTests(unittest.TestCase):
    def test_the_five_files_exist_and_are_utf8(self):
        for name in FILES:
            with self.subTest(name):
                (STATIC / name).read_bytes().decode("utf-8")


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("index.html")
        cls.page = Page(cls.text)
        cls.en = strings_json(read("strings.js"))["en"]

    def test_the_head(self):
        self.assertTrue(self.text.startswith("<!doctype html>\n"))
        self.assertEqual(self.page.doctype.lower(), "doctype html")
        self.assertEqual(self.page.all("html"), [{"lang": "en", "dir": "ltr"}])
        self.assertEqual(self.page.all("meta", charset="utf-8"), [{"charset": "utf-8"}])
        viewport = self.page.all("meta", name="viewport")
        self.assertEqual(len(viewport), 1)
        self.assertIn("width=device-width", viewport[0]["content"])
        self.assertEqual(self.page.all("link", rel="stylesheet"),
                         [{"rel": "stylesheet", "href": "/static/style.css"}])
        self.assertEqual(self.page.all("link", rel="icon"),
                         [{"rel": "icon", "href": "/static/icon.svg", "type": "image/svg+xml"}])

    def test_exactly_two_scripts_from_the_static_folder(self):
        scripts = self.page.all("script")
        self.assertEqual([s.get("src") for s in scripts],
                         ["/static/strings.js", "/static/app.js"])
        for script in scripts:
            self.assertIn("defer", script)
        self.assertNotIn("script", [tag for tag, _ in self.page.texts])
        self.assertEqual(len(re.findall(r"<script\b", self.text)), 2)
        for body in re.findall(r"<script\b[^>]*>(.*?)</script>", self.text, re.S):
            self.assertEqual(body.strip(), "")

    def test_nothing_inline_and_nothing_from_elsewhere(self):
        self.assertEqual(self.page.all("style"), [])
        self.assertNotRegex(self.text.lower(), r"javascript:|<style\b|\bstyle=")
        for tag, attrs in self.page.elements:
            for name, value in attrs.items():
                with self.subTest(tag=tag, attribute=name):
                    self.assertFalse(name.startswith("on"), "event attribute")
                    self.assertNotEqual(name, "style")
                    if name in ("src", "href", "action", "formaction", "srcset", "poster",
                                "data", "xlink:href"):
                        if not (tag == "form" and name == "action"):
                            self.assertTrue(value.startswith(("/static/", "#/")), value)
        self.assertNotRegex(self.text, r"https?://")

    def test_no_text_but_the_title(self):
        self.assertEqual(self.page.texts, [("title", self.en["app.title"])])
        self.assertEqual(self.page.all("title"), [{"data-i18n": "app.title"}])

    def test_every_form_is_posted_nowhere_if_the_script_fails(self):
        forms = self.page.all("form")
        self.assertEqual(len(forms), 6)
        for form in forms:
            with self.subTest(form["id"]):
                self.assertEqual(form.get("method"), "post")
                self.assertEqual(form.get("action"), "/")
                self.assertIn("novalidate", form)

    def test_every_control_has_an_id_and_every_input_a_label(self):
        labels = {a.get("for") for a in self.page.all("label")}
        for tag, attrs in self.page.elements:
            if tag in ("input", "select", "button", "textarea"):
                with self.subTest(tag=tag, attrs=attrs):
                    self.assertIn("id", attrs)
                    if tag != "button":
                        self.assertIn(attrs["id"], labels)
            if tag == "input" and attrs.get("type") == "password":
                with self.subTest(attrs["id"]):
                    self.assertIn(attrs.get("autocomplete"), ("current-password", "new-password"))
                    self.assertEqual(attrs.get("maxlength"), "128")
        for name, (tag, _) in self.page.by_id().items():
            if tag == "label":
                self.fail(f"label with id {name}")

    def test_the_ids_that_labels_and_aria_name_exist(self):
        found = self.page.by_id()
        for tag, attrs in self.page.elements:
            names = attrs.get("aria-labelledby", "").split() + attrs.get(
                "aria-describedby", "").split()
            if tag == "label":
                names.append(attrs.get("for"))
                self.assertIn(found.get(attrs.get("for"), ("?",))[0], ("input", "select"),
                              f"label for {attrs.get('for')}")
            for name in names:
                with self.subTest(tag=tag, id=name):
                    self.assertIn(name, found)

    def test_every_button_says_what_it_does(self):
        # A button inside a form submits it unless it says type="button".
        for attrs in self.page.all("button"):
            with self.subTest(attrs["id"]):
                self.assertIn(attrs.get("type"), ("button", "submit"))
        for form in self.page.all("form"):
            with self.subTest(form["id"]):
                submits = [a for t, a in self.page.within(form["id"])
                           if t == "button" and a.get("type") == "submit"]
                self.assertEqual(len(submits), 1)
        self.assertEqual(self.page.all("input", type="submit"), [])

    def test_the_focus_can_move_to_headings_and_lines(self):
        # app.js moves the focus to a new screen's (or step's) first heading,
        # or to the line that tells what happened, so a screen reader reads it.
        steps = [a["id"] for a in self.page.all("div") if a.get("class") == "step"]
        for container in ["screen-" + n for n in SCREENS] + steps:
            headings = [a for t, a in self.page.within(container) if t in ("h1", "h2")]
            if headings:
                with self.subTest(container):
                    self.assertEqual(headings[0].get("tabindex"), "-1")
        found = self.page.by_id()
        for name in MESSAGE_IDS:
            if name != "password-done":         # shown, not focused: it is a status
                with self.subTest(name):
                    self.assertEqual(found[name][1].get("tabindex"), "-1")

    def test_the_inputs_suit_a_phone(self):
        found = self.page.by_id()

        def attrs(name):
            return found[name][1]
        for name in ("login-phone", "setup-phone", "send-phone"):
            with self.subTest(name):
                self.assertEqual(attrs(name).get("type"), "tel")
                self.assertEqual(attrs(name).get("inputmode"), "tel")
        for name in ("login-phone", "setup-phone"):
            self.assertEqual(attrs(name).get("autocomplete"), "username")
        for name in ("send-amount", "quote-amount"):
            with self.subTest(name):
                self.assertEqual(attrs(name).get("inputmode"), "decimal")
        code = attrs("setup-code")
        self.assertEqual((code.get("autocomplete"), code.get("autocapitalize"),
                          code.get("spellcheck")), ("one-time-code", "characters", "false"))
        self.assertEqual(attrs("send-note").get("maxlength"), "200")
        self.assertEqual(attrs("order-name").get("maxlength"), "100")
        self.assertEqual(attrs("order-id").get("maxlength"), "100")
        self.assertEqual(attrs("login-password").get("autocomplete"), "current-password")
        self.assertEqual(attrs("password-old").get("autocomplete"), "current-password")
        for name in ("setup-password", "setup-password2", "password-new", "password-new2"):
            self.assertEqual(attrs(name).get("autocomplete"), "new-password")

    def test_the_contract_ids_exist_once(self):
        ids = [a["id"] for _, a in self.page.elements if "id" in a]
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids")
        for name in CONTRACT_IDS:
            with self.subTest(name):
                self.assertIn(name, ids)
        found = self.page.by_id()
        self.assertEqual(found["quote-currency"][0], "select")
        self.assertEqual(found["notice"][1].get("role"), "status")

    def test_one_section_per_screen_and_only_the_loading_one_shown(self):
        self.assertEqual(self.page.all("body"), [{"data-screen": "loading", "data-pending": "0"}])
        sections = self.page.all("section")
        self.assertEqual([s["id"] for s in sections], ["screen-" + n for n in SCREENS])
        for name, section in zip(SCREENS, sections):
            with self.subTest(name):
                self.assertEqual(section["data-screen"], name)
                self.assertEqual("hidden" in section, name != "loading")
        found = self.page.by_id()
        self.assertEqual(found["screen-send"][1]["data-step"], "form")
        self.assertEqual(found["screen-currency"][1]["data-step"], "ask")

    def test_the_nav(self):
        nav = self.page.by_id()["nav"][1]
        self.assertIn("hidden", nav)
        links = [a for a in self.page.all("a") if "data-nav" in a]
        self.assertEqual([(a["data-nav"], a["href"]) for a in links],
                         [(name, "#/" + name) for name in NAV])

    def test_message_elements_start_hidden_and_empty(self):
        found = self.page.by_id()
        for name in MESSAGE_IDS:
            with self.subTest(name):
                self.assertIn("hidden", found[name][1])
                self.assertNotIn("data-i18n", found[name][1])

    def test_named_texts_exist_and_take_no_placeholders(self):
        for tag, attrs in self.page.elements:
            for name in ("data-i18n", "data-i18n-placeholder", "data-i18n-label"):
                if name in attrs:
                    with self.subTest(tag=tag, key=attrs[name]):
                        self.assertIn(attrs[name], self.en)
                        self.assertEqual(placeholders(self.en[attrs[name]]), set())


class StringsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("strings.js")
        cls.strings = strings_json(cls.text)
        cls.en = cls.strings["en"]

    def test_the_shape(self):
        head, marker, rest = self.text.partition("window.STRINGS = ")
        self.assertEqual(marker, "window.STRINGS = ")
        for line in head.splitlines():
            self.assertTrue(line.startswith("//") or not line.strip(), line)
        self.assertTrue(rest.startswith("{"))
        self.assertEqual(rest[rest.rindex("}") + 1:].strip(), ";")
        self.assertIsInstance(self.strings, dict)
        self.assertIn("en", self.strings)

    def test_every_language_maps_keys_to_texts(self):
        for code, table in self.strings.items():
            self.assertRegex(code, r"^[a-z]{2}$")
            self.assertIn(table.get("meta.dir"), ("ltr", "rtl"), code)
            for key, value in table.items():
                with self.subTest(lang=code, key=key):
                    self.assertRegex(key, KEY)
                    self.assertIsInstance(value, str)
                    self.assertNotEqual(value.strip(), "")
                    # Braces only as {placeholder}.
                    self.assertNotRegex(PLACEHOLDER.sub("", value), r"[{}]")
        self.assertEqual(self.en["meta.dir"], "ltr")

    def test_other_languages_follow_english(self):
        for code, table in self.strings.items():
            for key, value in table.items():
                with self.subTest(lang=code, key=key):
                    self.assertIn(key, self.en)
                    self.assertEqual(placeholders(value), placeholders(self.en[key]))

    def test_the_keys_the_browser_tests_rely_on(self):
        for key, names in CONTRACT_KEYS.items():
            with self.subTest(key):
                self.assertIn(key, self.en)
                self.assertEqual(placeholders(self.en[key]), names)
        self.assertEqual(self.en["money.lyd"], "{amount} LYD")

    def test_texts_have_no_stray_spaces(self):
        for code, table in self.strings.items():
            for key, value in table.items():
                with self.subTest(lang=code, key=key):
                    self.assertEqual(value, value.strip())
                    self.assertNotIn("  ", value)


class ScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = read("app.js")
        cls.en = strings_json(read("strings.js"))["en"]
        cls.page = Page(read("index.html"))

    def keys(self):
        return [literal_arg(call) for name in ("t", "k") for call in js_calls(self.code, name)]

    def test_keys_are_written_out_and_exist(self):
        keys = self.keys()
        self.assertNotIn(None, keys, "a call of t or k without a written-out key")
        self.assertGreater(len(keys), 100)
        for key in keys:
            with self.subTest(key):
                self.assertIn(key, self.en)

    def test_every_text_is_used(self):
        used = set(self.keys())
        for _, attrs in self.page.elements:
            used.update(attrs[n] for n in ("data-i18n", "data-i18n-placeholder",
                                           "data-i18n-label") if n in attrs)
        self.assertEqual(sorted(set(self.en) - used), [])

    def test_elements_are_found_by_written_out_ids(self):
        ids = self.page.by_id()
        calls = [literal_arg(call) for call in js_calls(self.code, "byId")]
        self.assertNotIn(None, calls, "byId without a written-out id")
        for name in calls:
            with self.subTest(name):
                self.assertIn(name, ids)
        self.assertEqual(self.code.count("getElementById"), 1)      # inside byId
        self.assertIn("getElementById", function_body(self.code, "byId"))
        self.assertNotRegex(self.code, r"querySelector(All)?\(\s*[\"'`][^\"'`]*#")

    def test_no_html_sinks_and_nothing_from_elsewhere(self):
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                    "eval(", "new Function", 'setAttribute("style"', ".style.",
                    'setAttribute("on', "http://", "https://", "javascript:",
                    "localStorage", "import(", "importScripts"):
            with self.subTest(bad):
                self.assertNotIn(bad, self.code)
        self.assertIn('"use strict";', self.code)

    def test_nothing_newer_than_older_phones_run(self):
        # The page needs fetch with AbortController (Chrome 66, Safari 12.1);
        # these came later, and one syntax error leaves the screens blank.
        for bad in ("?.", "??", "Object.hasOwn", ".at(", "replaceAll(", "structuredClone"):
            with self.subTest(bad):
                self.assertNotIn(bad, self.code)

    def test_the_error_rules_follow_the_design(self):
        # The first rule that fits wins, so the order matters (R1 before R2,
        # which takes any other text). R1 to R36 are the table ERROR_RULES,
        # with the statuses tests/test_web_screens.py pins; R37 to R40 come
        # after it.
        start = self.code.index("var ERROR_RULES = [")
        rules = balanced(self.code, self.code.index("[", start))
        self.assertEqual(re.findall(r'k\("error\.(\w+)"\)', rules), ERROR_KEYS[:36])
        self.assertEqual([int(s) for s in re.findall(r"status: (\d+)", rules)],
                         [401, 401, 403, 403, 401, 401, 401, 403, 429, 429, 429, 403, 409,
                          400, 400, 400, 400, 400, 429, 400, 404, 400, 400, 400, 400, 400, 400,
                          409, 400, 409, 409, 409, 404, 400, 400, 500])
        rest = self.code[start + len(rules):]
        for name in ERROR_KEYS[36:]:
            self.assertIn(f'k("error.{name}")', rest)

    def test_the_constants(self):
        for line in ('var TIMEOUT_MS = 20000;', 'var QUOTE_MARGIN_MS = 5000;',
                     'var PENDING_SEND = "wallet.pendingSend";',
                     'var PENDING_ORDER = "wallet.pendingOrder";'):
            self.assertIn(line, self.code)

    def test_one_fetch_in_the_api_helper_with_the_default_mode(self):
        self.assertEqual(len(re.findall(r"\bfetch\(", self.code)), 1)
        self.assertRegex(function_body(self.code, "api"), r"\bfetch\(")
        # No mode and no referrerPolicy: under the page's no-referrer policy a
        # POST in another mode than the default carries "Origin: null".
        self.assertNotRegex(self.code, r"\bmode\s*[:=]|[\"']mode[\"']|referrerPolicy|referrer\s*:")
        self.assertIn('credentials: "same-origin"', self.code)
        self.assertIn('redirect: "error"', self.code)

    def test_no_request_runs_on_a_timer_or_a_key_press(self):
        self.assertLessEqual(len(re.findall(r"\bsetInterval\(", self.code)), 1)
        self.assertNotIn("visibilitychange", self.code)
        self.assertNotRegex(self.code, r"\brequestAnimationFrame\(|\bsetImmediate\(")
        for name in ("setTimeout", "setInterval"):
            for match in re.finditer(r"\b" + name + r"\(", self.code):
                arguments = balanced(self.code, match.end() - 1)
                with self.subTest(timer=name, arguments=arguments[:60]):
                    self.assertNotRegex(arguments, r"\bapi\(")
                    callback = re.match(r"\(\s*([A-Za-z_$][\w$]*)\s*,", arguments)
                    if callback:
                        self.assertNotRegex(function_body(self.code, callback.group(1)),
                                            r"\bapi\(")

    def test_a_quote_is_asked_for_only_by_the_form_submit(self):
        currency = block(self.code, "currency: prices, quote and order")
        self.assertNotRegex(
            currency,
            r"addEventListener\(\s*[\"'](input|change|keyup|keydown|keypress|focus|focusin|"
            r"blur|visibilitychange|pageshow)[\"']")
        self.assertNotRegex(currency, r"\.on(input|change|key\w+|focus|blur)\s*=")
        self.assertEqual(len(re.findall(r'"/api/quote"', self.code)), 1)
        self.assertIn('"/api/quote"', function_body(self.code, "onQuote"))
        self.assertIn('byId("quote-form").addEventListener("submit", onQuote)', currency)
        self.assertEqual(self.code.count("onQuote"), 2)     # its definition and the listener


class StyleTests(unittest.TestCase):
    def test_style_sheet(self):
        css = read("style.css")
        self.assertNotIn("@import", css)
        self.assertNotIn("http", css)
        for target in re.findall(r"url\(\s*([^)]*)\)", css):
            self.assertTrue(target.strip("\"' ").startswith("/static/"), target)
        self.assertRegex(css, r"\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}")
        self.assertIn("prefers-color-scheme: dark", css)


class IconTests(unittest.TestCase):
    def test_the_icon_is_shapes_only(self):
        text = read("icon.svg")
        root = ET.fromstring(text)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        for el in root.iter():
            name = el.tag.rsplit("}", 1)[-1]
            with self.subTest(name):
                self.assertNotIn(name, ("script", "style", "foreignObject", "a", "image", "use"))
                for attribute in el.attrib:
                    local = attribute.rsplit("}", 1)[-1]
                    self.assertFalse(local.startswith("on"), attribute)
                    self.assertNotIn(local, ("style", "href"))
        self.assertNotRegex(text.replace('xmlns="http://www.w3.org/2000/svg"', ""), r"https?:")


if __name__ == "__main__":
    unittest.main()
