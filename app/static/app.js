// The workers' screens: everything app/static/index.html does. Plain
// JavaScript with no outside code: it talks to the wallet's JSON API (one
// helper, api, makes every request) and builds every text from strings.js.
//
// Rules this file keeps (tests/test_pages.py checks them):
// - Texts come from strings.js only. Each key is written out in full, as
//   the first argument of t or, in a table, of k; the wallet's own error
//   text is never shown.
// - Every text goes in with textContent or createElement: never as HTML.
// - Elements are found with byId and a written-out id.
// - The API is only called because the worker did something (a tap, a
//   submit, opening a screen): never on a key press or a timer. The one
//   repeating timer is the quote's countdown, which calls nothing.
(function () {
  "use strict";

  var TIMEOUT_MS = 20000;               // a call not answered by then is given up
  var QUOTE_MARGIN_MS = 5000;           // a quote counts as expired this long before its time
  var PENDING_SEND = "wallet.pendingSend";
  var PENDING_ORDER = "wallet.pendingOrder";
  var PAGE = 50;                        // list items loaded at first
  var PAGE_MORE = 200;                  // "Show more": the most the API gives

  // ---------- strings ----------

  var STRINGS = window.STRINGS || {en: {}};
  var lang = "en";
  var PLACEHOLDER = /\{([a-z_]+)\}/g;

  // Values written left to right even inside right-to-left text; any other
  // value (a name, a time) is isolated with <bdi>, which finds its direction.
  var LTR_VALUES = {amount: true, cost: true, rate: true, code: true, phone: true};

  function has(object, key) {
    return object !== null && object !== undefined &&
      Object.prototype.hasOwnProperty.call(object, key);
  }

  // A key kept in a table (error rules, labels): returned as it is, so that
  // every key in this file is written out as the first argument of t or k.
  function k(key) {
    return key;
  }

  function template(key) {
    if (has(STRINGS, lang) && has(STRINGS[lang], key)) {
      return STRINGS[lang][key];
    }
    if (has(STRINGS.en, key)) {
      return STRINGS.en[key];
    }
    console.warn("wallet: no text for " + key);
    return key;
  }

  // The text of a key from a table, with its {placeholders} filled in.
  function textOf(key, params) {
    return template(key).replace(PLACEHOLDER, function (whole, name) {
      return has(params, name) ? String(params[name]) : whole;
    });
  }

  function t(key, params) {
    return textOf(key, params);
  }

  // Sets el's text to the key's text, as textOf() gives it, with each filled
  // placeholder in an element of its own (see LTR_VALUES). The element's
  // textContent is exactly textOf(key, params).
  function fill(el, key, params) {
    var text = template(key), pattern = new RegExp(PLACEHOLDER.source, "g");
    var last = 0, match, part;
    el.textContent = "";
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) {
        el.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      if (has(params, match[1])) {
        part = document.createElement(LTR_VALUES[match[1]] ? "span" : "bdi");
        if (LTR_VALUES[match[1]]) {
          part.className = "num";
        }
        part.textContent = String(params[match[1]]);
        el.appendChild(part);
      } else {
        el.appendChild(document.createTextNode(match[0]));
      }
      last = pattern.lastIndex;
    }
    if (last < text.length) {
      el.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  function each(list, action) {
    Array.prototype.forEach.call(list, action);
  }

  // Fills every element that names its text in index.html: data-i18n (its
  // text), data-i18n-placeholder and data-i18n-label (its aria-label).
  function applyStrings() {
    each(document.querySelectorAll("[data-i18n]"), function (el) {
      el.textContent = template(el.getAttribute("data-i18n"));
    });
    each(document.querySelectorAll("[data-i18n-placeholder]"), function (el) {
      el.setAttribute("placeholder", template(el.getAttribute("data-i18n-placeholder")));
    });
    each(document.querySelectorAll("[data-i18n-label]"), function (el) {
      el.setAttribute("aria-label", template(el.getAttribute("data-i18n-label")));
    });
  }

  // The worker's language when strings.js has it, else English. Sets the
  // page's lang and direction and refills every fixed text.
  function chooseLanguage(code) {
    lang = typeof code === "string" && has(STRINGS, code) ? code : "en";
    document.documentElement.setAttribute("lang", lang);
    document.documentElement.setAttribute("dir", t("meta.dir") === "rtl" ? "rtl" : "ltr");
    applyStrings();
  }

  // ---------- page helpers ----------

  function byId(id) {
    return document.getElementById(id);
  }

  function make(tag, className, text) {
    var el = document.createElement(tag);
    if (className) {
      el.className = className;
    }
    if (text !== undefined) {
      el.textContent = text;
    }
    return el;
  }

  // Shows a message element holding the key's text (and nothing else).
  function say(el, key, params) {
    fill(el, key, params);
    el.hidden = false;
  }

  // Shows the line and moves the focus to it, so a screen reader reads it
  // and a phone scrolls to it.
  function showError(el, key) {
    say(el, key);
    el.focus();
  }

  function silence(el) {
    el.textContent = "";
    el.hidden = true;
  }

  // The line above every screen (#notice), shown after the screen changed
  // ("You're logged out.", "Please log in again."): it gets the focus, so a
  // screen reader reads it before the new screen.
  function notify(key) {
    showError(byId("notice"), key);
  }

  function focusHeading(container) {
    var heading = container.querySelector("h1, h2");
    if (heading) {
      heading.focus();
    }
  }

  // A form's busy state: while busy its buttons are disabled (the handlers
  // also check their own busy flag, so a second tap or Enter does nothing).
  function setBusy(form, buttons, busy) {
    if (form) {
      form.setAttribute("aria-busy", busy ? "true" : "false");
    }
    buttons.forEach(function (button) {
      button.disabled = busy;
    });
  }

  function filled(text) {
    return typeof text === "string" && text.trim() !== "";
  }

  // Characters, as the wallet counts them (an emoji is one, not two).
  function chars(text) {
    return Array.from(text).length;
  }

  // ---------- formatting ----------

  function money(lyd) {
    return t("money.lyd", {amount: lyd});
  }

  var DIGITS = "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹";   // Arabic-Indic, then Persian
  var LYD_AMOUNT = /^\d+(\.\d{1,3})?$/;
  var FOREIGN_AMOUNT = /^\d+(\.\d{1,2})?$/;

  // A typed amount as the wallet takes it: trimmed, Arabic-Indic and Persian
  // digits as 0-9, the Arabic decimal mark as a point.
  function normalizeAmount(text) {
    var out = "";
    Array.from(text.trim()).forEach(function (c) {
      var digit = DIGITS.indexOf(c);
      out += digit >= 0 ? String(digit % 10) : c === "٫" ? "." : c;
    });
    return out;
  }

  function isAmount(text, pattern) {
    return pattern.test(text) && /[1-9]/.test(text);
  }

  // A checked LYD amount ("12.5") written as the wallet writes amounts
  // ("12.500", "1,000.000"), with text only, so nothing is rounded.
  function lydText(amount) {
    var parts = amount.split(".");
    var whole = parts[0].replace(/^0+(?=\d)/, "");
    var fraction = parts[1] || "";
    while (fraction.length < 3) {
      fraction += "0";
    }
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + fraction;
  }

  // The wallet's times have 6 digits after the second's point, or none;
  // some browsers take at most 3.
  function parseTime(text) {
    return typeof text === "string" ? Date.parse(text.replace(/(\.\d{3})\d+/, "$1")) : NaN;
  }

  var formatters = {};

  function formatter(withDate) {
    var name = lang + (withDate ? ":date" : ":time");
    if (!has(formatters, name)) {
      var options = withDate ? {dateStyle: "medium", timeStyle: "short"} : {timeStyle: "short"};
      try {
        formatters[name] = new Intl.DateTimeFormat(lang, options);
      } catch (e) {     // an older browser without dateStyle
        formatters[name] = new Intl.DateTimeFormat(lang);
      }
    }
    return formatters[name];
  }

  // A wallet time as the phone's local date and time ("Oct 19, 2026, 9:05 AM").
  function when(text) {
    var ms = parseTime(text);
    return isNaN(ms) ? String(text) : formatter(true).format(new Date(ms));
  }

  // The local time of day only ("9:05 AM").
  function clockText(ms) {
    return isNaN(ms) ? "" : formatter(false).format(new Date(ms));
  }

  // +218912345678 as Libyans write it: 091 234 5678.
  function phoneText(phone) {
    var match = /^\+218(9\d)(\d{3})(\d{4})$/.exec(phone || "");
    return match ? "0" + match[1] + " " + match[2] + " " + match[3] : String(phone || "");
  }

  function minutes(ms) {
    var seconds = Math.max(0, Math.ceil(ms / 1000));
    var rest = seconds % 60;
    return Math.floor(seconds / 60) + ":" + (rest < 10 ? "0" : "") + rest;
  }

  // ---------- storage ----------
  // A pending send or order is kept in sessionStorage (this tab only) so a
  // reload after a lost answer can still retry it with the same key or
  // quote. Without storage (a private window may refuse it) it is kept in
  // memory, which a reload forgets.

  var memory = {};

  function writePending(name, value) {
    memory[name] = value;
    try {
      window.sessionStorage.setItem(name, JSON.stringify(value));
    } catch (e) {
      // kept in memory only
    }
  }

  function dropPending(name) {
    delete memory[name];
    try {
      window.sessionStorage.removeItem(name);
    } catch (e) {
      // nothing stored
    }
  }

  // The pending send or order of the worker logged in, or null. One left by
  // someone else in this tab is dropped, never resumed.
  function readPending(name) {
    var value = has(memory, name) ? memory[name] : null;
    try {
      var raw = window.sessionStorage.getItem(name);
      if (raw !== null) {
        value = JSON.parse(raw);
      }
    } catch (e) {
      // unreadable: what memory has
    }
    var valid = name === PENDING_SEND ? isPendingSend(value) : isPendingOrder(value);
    if (!valid || !state.me || value.owner !== state.me.phone) {
      if (value !== null) {
        dropPending(name);
      }
      return null;
    }
    return value;
  }

  // ---------- answers ----------
  // What a 200 must hold to be the wallet's answer. Anything else (a proxy's
  // page, a captive portal's) is "not the wallet's answer" (error.unavailable).

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function isText(value) {
    return typeof value === "string";
  }

  function isNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  function isMoney(value) {
    return isObject(value) && isText(value.lyd);
  }

  function isOk(data) {
    return data.ok === true;
  }

  function isMe(data) {
    return isText(data.name) && isText(data.phone) && isMoney(data.balance);
  }

  function isEntry(entry) {
    return isObject(entry) && isText(entry.kind) && isNumber(entry.amount) &&
      isNumber(entry.txn_id) && isText(entry.created_at) && isText(entry.amount_lyd) &&
      isText(entry.balance_after_lyd);
  }

  function isStatement(data) {
    return Array.isArray(data.entries) && data.entries.every(isEntry);
  }

  function isRecipient(data) {
    return isText(data.name) && isText(data.phone);
  }

  function isSent(data) {
    return isNumber(data.txn_id) && isText(data.to) && isMoney(data.balance);
  }

  function isPrice(price) {
    return isObject(price) && isText(price.currency) && isText(price.rate) &&
      isText(price.set_at);
  }

  function isPrices(data) {
    return Array.isArray(data.prices) && data.prices.every(isPrice);
  }

  function isQuote(data) {
    return isObject(data) && isText(data.quote_id) && isText(data.currency) &&
      isText(data.amount) && isText(data.rate) && isText(data.cost_lyd) &&
      isText(data.expires_at);
  }

  function isOrder(data) {
    return isObject(data) && isText(data.receipt_code) && isText(data.currency) &&
      isText(data.foreign_amount) && isText(data.cost_lyd) &&
      isText(data.beneficiary_name) && isText(data.status) && isText(data.created_at);
  }

  function isOrders(data) {
    return Array.isArray(data.orders) && data.orders.every(isOrder);
  }

  function isPendingSend(value) {
    return isObject(value) && isText(value.owner) && isText(value.to_phone) &&
      isText(value.name) && isText(value.amount) && isAmount(value.amount, LYD_AMOUNT) &&
      isText(value.note) && isText(value.key) && value.key !== "";
  }

  function isPendingOrder(value) {
    return isObject(value) && isText(value.owner) && isQuote(value.quote) &&
      (value.date === null || isText(value.date)) && isNumber(value.arrived) &&
      filled(value.beneficiary_name) && filled(value.beneficiary_id);
  }

  // ---------- calls ----------

  // One call to the wallet's API. Resolves to {status, data, date} for a 200
  // whose JSON passes `expect`; anything else rejects with {kind, status,
  // type, message}: kind "network" (no answer), "timeout" (none within
  // TIMEOUT_MS) or "http" (an answer: type and message are the wallet's
  // error, or type null when it is not the wallet's answer).
  // No mode and no referrer option: with the page's no-referrer policy, a
  // POST in any mode but the default ("cors") carries "Origin: null" in a
  // browser that follows the Fetch standard, and the wallet refuses that.
  function api(method, path, body, expect) {
    var controller = new AbortController();
    var timer = setTimeout(function () {
      controller.abort();
    }, TIMEOUT_MS);
    var options = {
      method: method,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: {"Accept": "application/json"},
      signal: controller.signal
    };
    if (method === "POST") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body || {});
    }
    return fetch(path, options).then(function (response) {
      return response.text().then(function (text) {
        return {response: response, text: text};
      });
    }).then(function (got) {
      clearTimeout(timer);
      var status = got.response.status, data = null;
      try {
        data = JSON.parse(got.text);
      } catch (e) {
        data = null;
      }
      if (status === 200 && isObject(data) && expect(data)) {
        return {status: status, data: data, date: got.response.headers.get("Date")};
      }
      var wallet = status !== 200 && isObject(data) && isText(data.error) && isText(data.type);
      // For whoever debugs: the wallet's own text, never shown on the page.
      console.debug("wallet: " + method + " " + path + " answered " + status +
                    (wallet ? ": " + data.error : " (not the wallet's answer)"));
      throw {kind: "http", status: status, type: wallet ? data.type : null,
             message: wallet ? data.error : ""};
    }, function (problem) {
      clearTimeout(timer);
      throw {kind: controller.signal.aborted ? "timeout" : "network", status: 0,
             type: null, message: String(problem)};
    });
  }

  // ---------- errors ----------
  // Every answer is matched to a text by its status, its type and, where
  // several share a type, its exact text; the first rule that fits wins.
  // The rules are R1 to R36 in this order, then R37 to R40 below them;
  // tests/test_web_screens.py calls them so and pins the wallet's texts
  // they match. Actions: "login" (logged out: to the login screen),
  // "workers" (an admin's account: logged out, then told), "retry" (stay
  // and offer to try again), "ask" (the currency screen goes back to asking
  // for a price), "form" (stay, show the line). `lost` marks the rules where
  // it is unknown whether the request was done.

  var ERROR_RULES = [
    {status: 401, type: "NotLoggedIn", text: "this account can't be used; please contact the office",
     key: k("error.account_unusable"), action: "login"},
    {status: 401, type: "NotLoggedIn", key: k("error.session_ended"), action: "login"},
    {status: 403, type: "PermissionDenied", key: k("error.workers_only"), action: "workers"},
    {status: 403, type: "http", text: "refused: sent from another website",
     key: k("error.other_site"), action: "form"},
    {status: 401, type: "LoginFailed",
     text: "wrong phone number or password, or too many wrong tries: wait 15 minutes",
     key: k("error.login_failed"), action: "form"},
    {status: 401, type: "LoginFailed", text: "wrong phone number or code",
     key: k("error.setup_failed"), action: "form"},
    {status: 401, type: "LoginFailed", text: "this account is closed",
     key: k("error.account_closed"), action: "form"},
    {status: 403, type: "AccountBlocked", key: k("error.account_blocked"), action: "form"},
    {status: 429, type: "http", text: "too many tries from this connection; wait a few minutes",
     key: k("error.too_many_tries"), action: "form"},
    {status: 429, type: "http", text: "too many logins to this account; wait a few minutes",
     key: k("error.too_many_logins"), action: "form"},
    {status: 429, type: "AccountLocked", text: /^too many wrong tries; try again after \d\d:\d\d UTC$/,
     key: k("error.locked"), action: "form"},
    {status: 403, type: "WrongPassword", key: k("error.wrong_password"), action: "form"},
    {status: 409, type: "Conflict", text: "the password was changed meanwhile; try again",
     key: k("error.changed_meanwhile"), action: "form"},
    {status: 400, type: "LedgerError", text: "password must be at least 10 characters",
     key: k("error.password_short"), action: "form"},
    {status: 400, type: "LedgerError", text: "password is too long",
     key: k("error.password_long"), action: "form"},
    {status: 400, type: "LedgerError", text: "password is too simple",
     key: k("error.password_simple"), action: "form"},
    {status: 400, type: "LedgerError", text: "password is too common; choose another",
     key: k("error.password_common"), action: "form"},
    {status: 400, type: "LedgerError", text: "password can't contain your phone number",
     key: k("error.password_phone"), action: "form"},
    {status: 429, type: "http", text: "too many requests; wait a minute",
     key: k("error.too_many_requests"), action: "retry"},
    {status: 400, type: "LedgerError", text: /^phone number/,
     key: k("error.phone_format"), action: "form"},
    {status: 404, type: "UnknownAccount", text: "no worker with that phone number",
     key: k("error.unknown_number"), action: "form"},
    {status: 400, type: "LedgerError", text: "cannot send to yourself",
     key: k("error.send_to_self"), action: "form"},
    {status: 400, type: "InvalidAmount",
     text: /^(not a number|amount must be a positive whole number of dirhams|amount is not a number|amount must be more than zero)$/,
     key: k("error.amount_format"), action: "form"},
    {status: 400, type: "InvalidAmount", text: /^(amount too large|amount is too large)$/,
     key: k("error.amount_too_large"), action: "form"},
    {status: 400, type: "InvalidAmount", text: "at most 3 decimal places",
     key: k("error.amount_decimals"), action: "form"},
    {status: 400, type: "InvalidAmount", text: "amount: at most 2 decimal places",
     key: k("error.amount_decimals_foreign"), action: "form"},
    {status: 400, type: "InvalidAmount", text: "amount too small",
     key: k("error.amount_too_small"), action: "form"},
    {status: 409, type: "InsufficientFunds", key: k("error.balance_too_low"), action: "form"},
    {status: 400, type: "LedgerError", text: /( is not available$|^no price set for )/,
     key: k("error.currency_unavailable"), action: "ask"},
    {status: 409, type: "QuoteExpired", text: "price expired, get a new one",
     key: k("error.quote_expired"), action: "ask"},
    {status: 409, type: "QuoteExpired", text: "the price was corrected, get a new one",
     key: k("error.price_corrected"), action: "ask"},
    {status: 409, type: "Conflict", text: /already used for an order to another beneficiary/,
     key: k("error.quote_used"), action: "ask"},
    {status: 404, type: "NotFound", text: "quote not found",
     key: k("error.quote_not_found"), action: "ask"},
    {status: 400, type: "http", text: /^'[a-z_]+' is required$/,
     key: k("error.fields_missing"), action: "form"},
    {status: 400, type: "http", text: /is too long \(at most \d+ characters\)$/,
     key: k("error.field_too_long"), action: "form"},
    {status: 500, type: "internal", key: k("error.server"), action: "retry", lost: true}
  ];
  // Any other status of 500 or more (a proxy's 502, Cloudflare's 520 to 527),
  // or an answer that is not the wallet's (R37); no answer (R38); no answer
  // in time (R39); any other answer of the wallet's (R40).
  var UNAVAILABLE = {key: k("error.unavailable"), action: "retry", lost: true};
  var NETWORK = {key: k("error.network"), action: "retry", lost: true};
  var TIMEOUT = {key: k("error.timeout"), action: "retry", lost: true};
  var UNKNOWN = {key: k("error.unknown"), action: "form"};

  function textFits(text, message) {
    if (text === undefined) {
      return true;
    }
    return typeof text === "string" ? text === message : text.test(message);
  }

  // The rule for a failed call. Anything else thrown (a mistake in this
  // file) is thrown on, so it shows as an error instead of a polite line.
  function explain(error) {
    if (!isObject(error) || !isText(error.kind)) {
      throw error;
    }
    if (error.kind === "timeout") {
      return TIMEOUT;
    }
    if (error.kind === "network" || error.type === null) {
      return error.kind === "network" ? NETWORK : UNAVAILABLE;
    }
    for (var i = 0; i < ERROR_RULES.length; i += 1) {
      var rule = ERROR_RULES[i];
      if (rule.status === error.status && rule.type === error.type &&
          textFits(rule.text, error.message)) {
        return rule;
      }
    }
    return error.status >= 500 ? UNAVAILABLE : UNKNOWN;
  }

  // What a failed money call (a send, an order) means, whatever its text:
  // "definite" when the wallet itself refused it (its 400, 403, 404 or 409:
  // nothing was posted), "login" when it was logged out (401: nothing was
  // posted either), and "unsure" for everything else (no answer, a time-out,
  // 429, any 5xx, an answer that is not the wallet's), where the money may
  // have moved and only a retry with the same key or quote is safe.
  function outcome(error) {
    if (!isObject(error) || error.kind !== "http" || error.type === null) {
      return "unsure";
    }
    if (error.status === 401) {
      return "login";
    }
    if ([400, 403, 404, 409].indexOf(error.status) >= 0) {
      return "definite";
    }
    return "unsure";
  }

  // The answers every screen handles alike: logged out (R1, R2) and an
  // admin's account (R3). Returns a promise when it handled the rule.
  function common(rule) {
    if (rule.action === "login") {
      return toLogin(rule.key);
    }
    if (rule.action === "workers") {
      return workersOnly();
    }
    return null;
  }

  // ---------- state, actions and the router ----------

  var state = {
    me: null,           // the /api/me answer while logged in
    booted: false,      // the first /api/me answered: the address may pick screens
    screen: "loading",
    epoch: 0            // goes up at every reset; answers to older calls are dropped
  };

  var sections = {};
  var navLinks = [];
  var enterScreen = {};     // name -> what opening that screen does (returns a promise)
  var leaveScreen = {};     // name -> what leaving it does
  var actions = 0;          // user actions under way: body[data-pending]

  var ROUTES = ["login", "setup", "home", "send", "currency", "orders", "account"];

  // Runs a user action (a submit, a tap, opening a screen) and counts it in
  // body[data-pending] from its start to the end of its whole chain, so "0"
  // means every answer was handled and the page is up to date. A new action
  // started when none was under way clears the last notice.
  function track(action) {
    if (actions === 0) {
      silence(byId("notice"));
    }
    actions += 1;
    document.body.setAttribute("data-pending", String(actions));
    var finished = function () {
      actions -= 1;
      document.body.setAttribute("data-pending", String(actions));
    };
    var result;
    try {
      result = Promise.resolve(action());
    } catch (e) {
      result = Promise.reject(e);
    }
    return result.then(function (value) {
      finished();
      return value;
    }, function (problem) {
      finished();
      throw problem;
    });
  }

  function hashScreen() {
    var match = /^#\/([a-z]+)$/.exec(window.location.hash);
    return match && ROUTES.indexOf(match[1]) >= 0 ? match[1] : null;
  }

  // The route guard: logged out, only the login and setup screens; logged
  // in, never those two.
  function allowed(name) {
    if (!state.me) {
      return name === "setup" ? "setup" : "login";
    }
    return !name || name === "login" || name === "setup" ? "home" : name;
  }

  // Opens a screen (as the guard allows) and puts it in the address. The
  // screen's own loading is part of the calling action.
  function go(name, replace) {
    name = allowed(name);
    var hash = "#/" + name;
    if (window.location.hash !== hash) {
      window.history[replace ? "replaceState" : "pushState"](null, "", hash);
    }
    return showScreen(name);
  }

  // Shows a screen and runs what entering it does. A notice belongs to the
  // screen it was shown on: the flows that show one (log out, a 401, a
  // cancel) show it after the screen changed.
  function showScreen(name) {
    if (state.screen !== name) {
      silence(byId("notice"));
      if (has(leaveScreen, state.screen)) {
        leaveScreen[state.screen]();
      }
    }
    state.screen = name;
    Object.keys(sections).forEach(function (screen) {
      sections[screen].hidden = screen !== name;
    });
    document.body.setAttribute("data-screen", name);
    navLinks.forEach(function (link) {
      if (link.getAttribute("data-nav") === name) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
    window.scrollTo(0, 0);
    // The enter step fills the screen's own texts at once (the home's
    // "Hello, …"), so the heading has its text when it gets the focus. When
    // that step already put the focus on a line of this screen (a pending
    // send's "We couldn't confirm…"), the focus stays there.
    var entered = has(enterScreen, name) ? track(enterScreen[name]) : Promise.resolve();
    if (!sections[name].contains(document.activeElement)) {
      focusHeading(sections[name]);
    }
    return entered;
  }

  // Links to #/<screen> open it at once, inside the tap's action, rather
  // than on the hashchange that would follow.
  function onLinkClick(event) {
    var link = event.target && event.target.closest ? event.target.closest("a[href^='#/']") : null;
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey ||
        event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    go(link.getAttribute("href").slice(2));
  }

  // The back button, or an address typed by hand.
  function onHashChange() {
    if (state.booted) {
      go(hashScreen(), true);
    }
  }

  // Whether an /api/me answer is still the worker this tab shows. The tabs
  // of a browser share its cookie: once another worker has logged in in
  // another tab, this one is logged out (R2) rather than shown their account.
  function sameWorker(me) {
    return !!state.me && me.phone === state.me.phone;
  }

  function loggedIn(me) {
    state.me = me;
    chooseLanguage(me.language);
    byId("nav").hidden = false;
  }

  // Where a worker lands after start-up or a login: their unsure send or
  // order first, if one waits; else the screen asked for, or home.
  function openAfterLogin(me, wanted) {
    loggedIn(me);
    if (readPending(PENDING_SEND)) {
      return go("send", true);
    }
    if (readPending(PENDING_ORDER)) {
      return go("currency", true);
    }
    return go(wanted || "home", true);
  }

  // Clears every screen and the state, so the next worker to log in in this
  // tab never sees the last one's balance, activity or orders. The pending
  // send or order is kept only when asked (after a 401: it is retried after
  // the next login by the same worker).
  function resetAll(keepPending) {
    state.epoch += 1;
    state.me = null;
    if (!keepPending) {
      dropPending(PENDING_SEND);
      dropPending(PENDING_ORDER);
    }
    byId("nav").hidden = true;
    chooseLanguage("en");
    resetLogin();
    resetHome();
    resetSend();
    resetCurrency();
    resetOrders();
    resetAccount();
    each(document.querySelectorAll("button"), function (button) {
      button.disabled = false;
    });
    each(document.querySelectorAll("form, [aria-busy]"), function (el) {
      el.setAttribute("aria-busy", "false");
    });
  }

  // Every 401 (R1, R2): logged out; the login screen shows why.
  function toLogin(key) {
    resetAll(true);
    return go("login", true).then(function () {
      notify(key);
    });
  }

  // An admin's account on the workers' screens (R3): logged out (the cookie
  // can only be cleared by the wallet), then told so.
  function workersOnly() {
    var done = function () {
      resetAll(false);
      return go("login", true).then(function () {
        showError(byId("login-error"), k("error.workers_only"));
      });
    };
    return api("POST", "/api/logout", {}, isOk).then(done, done);
  }

  // After log out, or log out everywhere.
  function loggedOut(key) {
    resetAll(false);
    return go("login", true).then(function () {
      notify(key);
    });
  }

  // Start-up: who is logged in decides the first screen.
  function boot() {
    return track(function () {
      state.booted = false;
      showScreen("loading");
      return api("GET", "/api/me", null, isMe).then(function (answer) {
        state.booted = true;
        return openAfterLogin(answer.data, hashScreen());
      }, function (error) {
        var rule = explain(error);
        if (rule.key === k("error.account_unusable")) {
          state.booted = true;      // R1: the worker is told why
          return toLogin(rule.key);
        }
        if (error.kind === "http" && error.status === 401 && error.type !== null) {
          state.booted = true;      // not logged in: the login screen, no line
          return go(hashScreen() === "setup" ? "setup" : "login", true);
        }
        if (rule.action === "workers") {
          state.booted = true;
          return workersOnly();
        }
        showScreen("offline");
        say(byId("offline-text"), rule.key);
      });
    });
  }

  // ---------- log in and choose a password ----------

  var login = {busy: false};
  var setup = {busy: false};

  function resetLogin() {
    login.busy = false;
    setup.busy = false;
    byId("login-form").reset();
    byId("setup-form").reset();
    silence(byId("login-error"));
    silence(byId("setup-error"));
  }

  // After a login or a new password: who is it? An admin is logged out
  // again (R3); a worker gets their screens, and the notice when one is given.
  function afterLogin(onFailure, noticeKey) {
    return api("GET", "/api/me", null, isMe).then(function (answer) {
      return openAfterLogin(answer.data, "home").then(function () {
        if (noticeKey) {
          notify(noticeKey);
        }
      });
    }, function (error) {
      var rule = explain(error);
      return common(rule) || onFailure(rule);
    });
  }

  function onLogin(event) {
    event.preventDefault();
    if (login.busy) {
      return;
    }
    var form = byId("login-form"), error = byId("login-error");
    var phone = byId("login-phone"), password = byId("login-password");
    silence(error);
    if (!filled(phone.value) || password.value === "") {
      showError(error, k("check.required"));
      return;
    }
    login.busy = true;
    setBusy(form, [byId("login-submit")], true);
    track(function () {
      return api("POST", "/api/login", {phone: phone.value, password: password.value}, isOk)
        .then(function () {
          password.value = "";
          return afterLogin(function (rule) {
            showError(error, rule.key);
          });
        }, function (problem) {
          var rule = explain(problem);
          if (rule.key === k("error.login_failed")) {
            password.value = "";
          }
          return common(rule) || showError(error, rule.key);
        }).then(function () {
          login.busy = false;
          setBusy(form, [byId("login-submit")], false);
        });
    });
  }

  function onSetup(event) {
    event.preventDefault();
    if (setup.busy) {
      return;
    }
    var form = byId("setup-form"), error = byId("setup-error");
    var phone = byId("setup-phone"), code = byId("setup-code");
    var password = byId("setup-password"), again = byId("setup-password2");
    silence(error);
    if (!filled(phone.value) || !filled(code.value) || password.value === "" || again.value === "") {
      showError(error, k("check.required"));
      return;
    }
    if (password.value !== again.value) {
      showError(error, k("check.passwords_differ"));
      return;
    }
    if (chars(password.value) < 10) {
      showError(error, k("check.password_short"));
      return;
    }
    setup.busy = true;
    setBusy(form, [byId("setup-submit")], true);
    track(function () {
      var body = {phone: phone.value, code: code.value, password: password.value};
      return api("POST", "/api/setup-password", body, isOk).then(function () {
        var typed = phone.value;
        form.reset();
        return afterLogin(function () {
          // The password is set, but who is logged in could not be read:
          // the login screen, with the phone filled in.
          byId("login-phone").value = typed;
          return go("login", true).then(function () {
            notify(k("setup.done"));
          });
        }, k("setup.done"));
      }, function (problem) {
        var rule = explain(problem);
        // No answer: the code may be used up already, so a retry could only
        // say it is wrong; the worker is told to try logging in first.
        return common(rule) || showError(error, rule.lost ? k("setup.unsure") : rule.key);
      }).then(function () {
        setup.busy = false;
        setBusy(form, [byId("setup-submit")], false);
      });
    });
  }

  // The phone typed on one of the two screens is carried to the other.
  function carryPhone(from, to) {
    return function () {
      if (filled(from.value) && !filled(to.value)) {
        to.value = from.value;
      }
    };
  }

  function wireLogin() {
    byId("login-form").addEventListener("submit", onLogin);
    byId("setup-form").addEventListener("submit", onSetup);
    byId("login-to-setup").addEventListener("click",
                                            carryPhone(byId("login-phone"), byId("setup-phone")));
    byId("setup-to-login").addEventListener("click",
                                            carryPhone(byId("setup-phone"), byId("login-phone")));
    byId("offline-retry").addEventListener("click", boot);
  }

  // ---------- balance and statement ----------

  var home = {busy: false};

  var ENTRY_LABELS = {
    deposit: k("entry.deposit"),
    payroll: k("entry.payroll"),
    withdrawal: k("entry.withdrawal")
  };
  // What the wallet writes as a reversal's note, for the worker.
  var REVERSAL_LABELS = {
    "Order cancelled": k("entry.order_cancelled"),
    "Correction by admin": k("entry.correction")
  };

  function resetHome() {
    home.busy = false;
    byId("home-name").textContent = "";
    byId("home-balance").textContent = "";
    byId("home-updated").textContent = "";
    byId("statement-list").textContent = "";
    byId("statement-empty").hidden = true;
    byId("statement-more").hidden = true;
    silence(byId("home-error"));
  }

  function showHomeMe() {
    fill(byId("home-name"), k("home.hello"), {name: state.me.name});
    byId("home-balance").textContent = money(state.me.balance.lyd);
  }

  function entryLabel(entry) {
    if (entry.kind === "transfer") {
      return entry.amount > 0 ? k("entry.received") : k("entry.sent");
    }
    if (entry.kind === "order") {
      return k("entry.order");
    }
    if (entry.kind === "reversal" && has(REVERSAL_LABELS, entry.note)) {
      return REVERSAL_LABELS[entry.note];
    }
    return has(ENTRY_LABELS, entry.kind) ? ENTRY_LABELS[entry.kind] : k("entry.other");
  }

  function entryItem(entry) {
    var incoming = entry.amount > 0;
    var item = make("li", "entry");
    item.setAttribute("data-kind", entry.kind);
    item.setAttribute("data-dir", incoming ? "in" : "out");
    item.setAttribute("data-txn-id", String(entry.txn_id));
    var top = make("div", "entry-top"), label = make("span", "entry-label");
    // An order's reference is its receipt code; a deposit's is a till's
    // code, which means nothing to the worker.
    fill(label, entryLabel(entry), {code: entry.reference || ""});
    top.appendChild(label);
    top.appendChild(make("span", "entry-amount num " + (incoming ? "in" : "out"),
                         money((incoming ? "+" : "") + entry.amount_lyd)));
    var below = make("div", "entry-below"), balance = make("span", "entry-balance");
    below.appendChild(make("span", "entry-date", when(entry.created_at)));
    fill(balance, k("entry.balance_after"), {amount: money(entry.balance_after_lyd)});
    below.appendChild(balance);
    item.appendChild(top);
    item.appendChild(below);
    // A reversal's note is the wallet's own wording, already in the label.
    if (entry.kind !== "reversal" && filled(entry.note)) {
      item.appendChild(make("p", "entry-note", entry.note));
    }
    return item;
  }

  // Loads the balance and the newest entries (limit PAGE), or only the
  // entries (PAGE_MORE, "Show more").
  function loadHome(limit) {
    if (home.busy) {
      return Promise.resolve();
    }
    var list = byId("statement-list"), error = byId("home-error"), epoch = state.epoch;
    var buttons = [byId("home-refresh"), byId("statement-more")];
    home.busy = true;
    setBusy(null, buttons, true);
    list.setAttribute("aria-busy", "true");
    silence(error);
    var calls = [api("GET", "/api/statement?limit=" + limit, null, isStatement)];
    if (limit === PAGE) {
      calls.push(api("GET", "/api/me", null, isMe));
    }
    return Promise.all(calls).then(function (answers) {
      if (epoch !== state.epoch) {
        return;
      }
      if (answers.length > 1) {
        if (!sameWorker(answers[1].data)) {
          return toLogin(k("error.session_ended"));
        }
        state.me = answers[1].data;
        showHomeMe();
      }
      var entries = answers[0].data.entries;
      list.textContent = "";
      entries.forEach(function (entry) {
        list.appendChild(entryItem(entry));
      });
      byId("statement-empty").hidden = entries.length > 0;
      byId("statement-more").hidden = !(limit === PAGE && entries.length >= PAGE);
      fill(byId("home-updated"), k("home.updated"), {time: clockText(Date.now())});
    }, function (problem) {
      if (epoch !== state.epoch) {
        return;
      }
      var rule = explain(problem);
      return common(rule) || showError(error, rule.key);
    }).then(function () {
      if (epoch === state.epoch) {
        home.busy = false;
        setBusy(null, buttons, false);
        list.setAttribute("aria-busy", "false");
      }
    });
  }

  function enterHome() {
    showHomeMe();
    return loadHome(PAGE);
  }

  function wireHome() {
    enterScreen.home = enterHome;
    byId("home-refresh").addEventListener("click", function () {
      track(function () {
        return loadHome(PAGE);
      });
    });
    byId("statement-more").addEventListener("click", function () {
      track(function () {
        return loadHome(PAGE_MORE);
      });
    });
  }

  // ---------- send money ----------
  // Three steps: form, confirm, done. Each confirm step gets one new
  // idempotency key; a retry after an unsure answer reuses it, so the money
  // can't be sent twice (also after a reload: the pending send is stored).

  var send = {busy: false, step: "form", seen: true, unsure: false, key: null, to: null,
              amount: "", note: ""};

  function resetSend() {
    send = {busy: false, step: "form", seen: true, unsure: false, key: null, to: null,
            amount: "", note: ""};
    byId("send-form").reset();
    byId("send-balance").textContent = "";
    byId("send-confirm-name").textContent = "";
    byId("send-confirm-phone").textContent = "";
    byId("send-confirm-amount").textContent = "";
    byId("send-confirm-note").textContent = "";
    byId("send-done-text").textContent = "";
    byId("send-done-balance").textContent = "";
    silence(byId("send-error"));
    silence(byId("send-confirm-error"));
    setSendStep("form", false);
  }

  // Shows one step. `seen` says whether the worker had the screen open
  // then: a send answered while they were elsewhere still shows its done
  // step when they come back.
  function setSendStep(step, focus) {
    var parts = {form: byId("send-step-form"), confirm: byId("send-step-confirm"),
                 done: byId("send-step-done")};
    send.step = step;
    send.seen = state.screen === "send";
    byId("screen-send").setAttribute("data-step", step);
    Object.keys(parts).forEach(function (name) {
      parts[name].hidden = name !== step;
    });
    if (focus) {
      focusHeading(step === "form" ? byId("screen-send") : parts[step]);
    }
  }

  function sendBusy(busy) {
    send.busy = busy;
    setBusy(byId("send-form"), [byId("send-next"), byId("send-confirm"), byId("send-edit"),
                                byId("send-cancel")], busy);
  }

  function showSendBalance() {
    if (state.me) {
      fill(byId("send-balance"), k("send.balance"), {amount: money(state.me.balance.lyd)});
    }
  }

  // A fresh balance: GET /api/me (it reads, never writes).
  function refreshMe(epoch, then) {
    return api("GET", "/api/me", null, isMe).then(function (answer) {
      if (epoch !== state.epoch) {
        return;
      }
      if (!sameWorker(answer.data)) {
        return toLogin(k("error.session_ended"));
      }
      state.me = answer.data;
      then();
    }, function (problem) {
      if (epoch === state.epoch) {
        return common(explain(problem));    // otherwise the last balance stays
      }
    });
  }

  // "web-" and 32 hex digits. crypto.getRandomValues works on plain http
  // too, unlike crypto.randomUUID.
  function newKey() {
    var bytes = new Uint8Array(16), hex = "web-";
    window.crypto.getRandomValues(bytes);
    Array.prototype.forEach.call(bytes, function (b) {
      hex += (b < 16 ? "0" : "") + b.toString(16);
    });
    return hex;
  }

  function confirmButtons() {
    var button = byId("send-confirm");
    if (send.unsure) {
      button.textContent = t("common.retry");
    } else {
      fill(button, k("send.confirm"), {amount: money(lydText(send.amount))});
    }
    byId("send-edit").hidden = send.unsure;
    byId("send-cancel").hidden = !send.unsure;
  }

  function showConfirm() {
    byId("send-confirm-name").textContent = send.to.name;
    byId("send-confirm-phone").textContent = phoneText(send.to.phone);
    byId("send-confirm-amount").textContent = money(lydText(send.amount));
    byId("send-confirm-note").textContent = send.note;
    byId("send-confirm-note-row").hidden = send.note === "";
    confirmButtons();
  }

  // A send whose answer was lost (also before a reload, or a login): its
  // confirm step again, with the same key, offering only Try again and Cancel.
  function openPendingSend(pending) {
    send.to = {name: pending.name, phone: pending.to_phone};
    send.amount = pending.amount;
    send.note = pending.note;
    send.key = pending.key;
    send.unsure = true;
    byId("send-phone").value = phoneText(pending.to_phone);
    byId("send-amount").value = pending.amount;
    byId("send-note").value = pending.note;
    showConfirm();
    setSendStep("confirm", false);
    showError(byId("send-confirm-error"), k("send.unsure"));
  }

  function enterSend() {
    showSendBalance();
    if (!send.busy) {
      var pending = readPending(PENDING_SEND);
      if (pending) {
        openPendingSend(pending);
      } else if (send.step === "done" && send.seen) {
        byId("send-form").reset();
        setSendStep("form", false);
      } else if (send.step === "done") {
        send.seen = true;
      } else if (send.step === "confirm") {
        send.key = null;          // a new confirm step makes a new key
        setSendStep("form", false);
      }
    }
    return refreshMe(state.epoch, showSendBalance);
  }

  function onSendNext(event) {
    event.preventDefault();
    if (send.busy) {
      return;
    }
    var error = byId("send-error");
    var phone = byId("send-phone").value, amount = normalizeAmount(byId("send-amount").value);
    var note = byId("send-note").value.trim();
    silence(error);
    if (!filled(phone) || amount === "") {
      showError(error, k("check.required"));
      return;
    }
    if (!isAmount(amount, LYD_AMOUNT)) {
      showError(error, k("check.amount_lyd"));
      return;
    }
    sendBusy(true);
    track(function () {
      var epoch = state.epoch;
      return api("GET", "/api/recipient?phone=" + encodeURIComponent(phone), null, isRecipient)
        .then(function (answer) {
          if (epoch !== state.epoch) {
            return;
          }
          send.to = {name: answer.data.name, phone: answer.data.phone};
          send.amount = amount;
          send.note = note;
          send.key = newKey();
          send.unsure = false;
          silence(byId("send-confirm-error"));
          showConfirm();
          setSendStep("confirm", true);
        }, function (problem) {
          if (epoch !== state.epoch) {
            return;
          }
          var rule = explain(problem);
          return common(rule) || showError(error, rule.key);
        }).then(function () {
          if (epoch === state.epoch) {
            sendBusy(false);
          }
        });
    });
  }

  function onSendConfirm() {
    if (send.busy || !send.key || !state.me) {
      return;
    }
    var error = byId("send-confirm-error");
    // from_phone: the wallet refuses the send if the cookie is now another
    // worker's (logged in in another tab).
    var body = {to_phone: send.to.phone, amount: send.amount, idempotency_key: send.key,
                from_phone: state.me.phone};
    if (send.note !== "") {
      body.note = send.note;
    }
    sendBusy(true);
    silence(error);
    writePending(PENDING_SEND, {owner: state.me.phone, to_phone: send.to.phone,
                                name: send.to.name, amount: send.amount, note: send.note,
                                key: send.key});
    track(function () {
      var epoch = state.epoch;
      return api("POST", "/api/send", body, isSent).then(function (answer) {
        if (epoch !== state.epoch) {
          return;
        }
        dropPending(PENDING_SEND);
        state.me.balance = answer.data.balance;
        send.unsure = false;
        send.key = null;
        fill(byId("send-done-text"), k("send.done_text"),
             {amount: money(lydText(send.amount)), name: answer.data.to});
        fill(byId("send-done-balance"), k("send.done_balance"),
             {amount: money(answer.data.balance.lyd)});
        showSendBalance();
        setSendStep("done", true);
      }, function (problem) {
        if (epoch !== state.epoch) {
          return;
        }
        var rule = explain(problem), result = outcome(problem);
        if (result === "login") {
          return toLogin(rule.key);      // the pending send waits for the login
        }
        if (result === "unsure") {
          send.unsure = true;
          confirmButtons();
          showError(error, rule.key === k("error.too_many_requests") ? rule.key : k("send.unsure"));
          return;
        }
        // The wallet refused it: nothing was sent. Back to the form, whose
        // fields still hold what was typed; the next confirm makes a new key.
        dropPending(PENDING_SEND);
        send.unsure = false;
        send.key = null;
        if (rule.action === "workers") {
          return workersOnly();
        }
        setSendStep("form", false);
        showError(byId("send-error"), rule.key);
        if (rule.key === k("error.balance_too_low")) {
          return refreshMe(epoch, showSendBalance);
        }
      }).then(function () {
        if (epoch === state.epoch) {
          sendBusy(false);
        }
      });
    });
  }

  function wireSend() {
    enterScreen.send = enterSend;
    byId("send-form").addEventListener("submit", onSendNext);
    byId("send-confirm").addEventListener("click", onSendConfirm);
    byId("send-edit").addEventListener("click", function () {
      if (!send.busy && !send.unsure) {
        send.key = null;
        setSendStep("form", false);
        byId("send-amount").focus();
      }
    });
    // Unsure, and the worker gives up: they are sent to their activity to
    // see whether the money went.
    byId("send-cancel").addEventListener("click", function () {
      if (send.busy) {
        return;
      }
      track(function () {
        dropPending(PENDING_SEND);
        resetSend();
        return go("home").then(function () {
          notify(k("send.cancelled_check"));
        });
      });
    });
    byId("send-done-ok").addEventListener("click", function () {
      track(function () {
        return go("home");
      });
    });
    byId("send-again").addEventListener("click", function () {
      byId("send-form").reset();
      setSendStep("form", false);
      byId("send-phone").focus();
    });
  }

  // ---------- currency: prices, quote and order ----------
  // Three steps: ask, quote, receipt. A quote is a stored row at the
  // wallet, so one is asked for only when the worker taps "See the price"
  // (the form's submit; Enter is the same submit): no key press, change of
  // the select, timer or return to the tab asks for one. The countdown
  // calls nothing; at its end the worker is taken back to ask, never while
  // an order is under way or unsure (it may have gone through). An order the
  // wallet refused keeps its line there (cur.refusal), so the worker still
  // learns why, also when the refusal came after the price's time.

  var cur = {
    step: "ask", pricesBusy: false, quoteBusy: false, orderBusy: false, unsure: false,
    quote: null, date: null, arrived: 0, timer: null, seen: true, refusal: null
  };

  var ORDER_STATUS = {
    placed: k("order.status.placed"),
    assigned: k("order.status.assigned"),
    paid: k("order.status.paid"),
    cancelled: k("order.status.cancelled")
  };

  function statusText(status) {
    return has(ORDER_STATUS, status) ? textOf(ORDER_STATUS[status]) : String(status);
  }

  function resetCurrency() {
    stopCountdown();
    cur = {
      step: "ask", pricesBusy: false, quoteBusy: false, orderBusy: false, unsure: false,
      quote: null, date: null, arrived: 0, timer: null, seen: true, refusal: null
    };
    byId("prices-list").textContent = "";
    byId("prices-empty").hidden = true;
    byId("quote-currency").textContent = "";
    byId("quote-form").reset();
    byId("quote-form").hidden = true;
    byId("order-form").reset();
    orderFieldsLocked(false);
    silence(byId("prices-error"));
    silence(byId("quote-error"));
    silence(byId("quote-notice"));
    silence(byId("order-error"));
    [byId("quote-summary"), byId("quote-rate"), byId("quote-until"), byId("quote-left"),
     byId("quote-balance"), byId("order-submit"), byId("receipt-code"), byId("receipt-copied"),
     byId("receipt-details"), byId("receipt-cost"), byId("receipt-status")].forEach(function (el) {
      el.textContent = "";
    });
    silence(byId("receipt-balance"));
    setCurrencyStep("ask", false);
  }

  // Shows one step. `seen` says whether the worker had the screen open
  // then: an order answered while they were elsewhere still shows its
  // receipt when they come back; a receipt they saw makes way for a new
  // order.
  function setCurrencyStep(step, focus) {
    var parts = {ask: byId("currency-step-ask"), quote: byId("currency-step-quote"),
                 receipt: byId("currency-step-receipt")};
    cur.step = step;
    cur.seen = state.screen === "currency";
    byId("screen-currency").setAttribute("data-step", step);
    Object.keys(parts).forEach(function (name) {
      parts[name].hidden = name !== step;
    });
    if (step !== "quote") {
      stopCountdown();
    }
    if (focus) {
      focusHeading(parts[step]);
    }
  }

  function showPrices(prices) {
    var list = byId("prices-list"), select = byId("quote-currency"), chosen = select.value;
    list.textContent = "";
    select.textContent = "";
    prices.forEach(function (price) {
      var item = make("li", "price"), rate = make("span", "price-rate");
      var time = make("span", "price-time");
      item.setAttribute("data-currency", price.currency);
      fill(rate, k("currency.price_line"), {currency: price.currency, rate: price.rate});
      fill(time, k("currency.price_time"), {time: when(price.set_at)});
      item.appendChild(rate);
      item.appendChild(time);
      list.appendChild(item);
      var option = make("option", null, price.currency);
      option.value = price.currency;
      select.appendChild(option);
      if (price.currency === chosen) {
        option.selected = true;
      }
    });
    byId("prices-empty").hidden = prices.length > 0;
    byId("quote-form").hidden = prices.length === 0;
  }

  // GET /api/prices: it reads only; no quote is made.
  function loadPrices() {
    if (cur.pricesBusy) {
      return Promise.resolve();
    }
    var list = byId("prices-list"), error = byId("prices-error"), epoch = state.epoch;
    cur.pricesBusy = true;
    setBusy(null, [byId("prices-refresh")], true);
    list.setAttribute("aria-busy", "true");
    silence(error);
    return api("GET", "/api/prices", null, isPrices).then(function (answer) {
      if (epoch === state.epoch) {
        showPrices(answer.data.prices);
      }
    }, function (problem) {
      if (epoch !== state.epoch) {
        return;
      }
      var rule = explain(problem);
      return common(rule) || showError(error, rule.key);
    }).then(function () {
      if (epoch === state.epoch) {
        cur.pricesBusy = false;
        setBusy(null, [byId("prices-refresh")], false);
        list.setAttribute("aria-busy", "false");
      }
    });
  }

  function showQuoteBalance() {
    if (state.me) {
      fill(byId("quote-balance"), k("currency.balance"), {amount: money(state.me.balance.lyd)});
    }
  }

  // How long the quote still holds, in ms, by the wallet's clock: its
  // expiry minus the Date of its answer, minus the time since it arrived
  // (so a phone set to the wrong time doesn't matter).
  function timeLeft() {
    if (!cur.quote) {
      return 0;
    }
    var expires = parseTime(cur.quote.expires_at), served = Date.parse(cur.date || "");
    if (isNaN(expires)) {
      return 0;
    }
    return isNaN(served) ? expires - Date.now() : expires - served - (Date.now() - cur.arrived);
  }

  function stopCountdown() {
    if (cur.timer !== null) {
      clearInterval(cur.timer);
      cur.timer = null;
    }
  }

  // Once a second on the quote step. It only changes what is shown.
  function tick() {
    var left = timeLeft() - QUOTE_MARGIN_MS, el = byId("quote-left");
    if (left > 0) {
      fill(el, k("quote.left"), {time: minutes(left)});
    } else if (cur.orderBusy || cur.unsure) {
      el.textContent = t("quote.expired_wait");
    } else {
      backToAsk(cur.refusal || k("error.quote_expired"), false);
    }
  }

  function startCountdown() {
    stopCountdown();
    tick();
    if (cur.step === "quote") {
      cur.timer = setInterval(tick, 1000);
    }
  }

  function orderFieldsLocked(locked) {
    byId("order-name").readOnly = locked;
    byId("order-id").readOnly = locked;
  }

  function orderButtons() {
    var submit = byId("order-submit");
    if (cur.unsure) {
      submit.textContent = t("common.retry");
    } else {
      fill(submit, k("order.submit"), {cost: money(cur.quote.cost_lyd)});
    }
    byId("order-new-price").hidden = cur.unsure;
    byId("order-cancel").hidden = !cur.unsure;
    orderFieldsLocked(cur.unsure);
  }

  function showQuote() {
    var quote = cur.quote;
    fill(byId("quote-summary"), k("quote.summary"),
         {amount: quote.amount, currency: quote.currency, cost: money(quote.cost_lyd)});
    fill(byId("quote-rate"), k("quote.rate"), {currency: quote.currency, rate: quote.rate});
    fill(byId("quote-until"), k("quote.until"), {time: clockText(parseTime(quote.expires_at))});
    showQuoteBalance();
    orderButtons();
  }

  // Back to asking for a price, with a line in #quote-notice when given.
  // The amount and the beneficiary stay typed, unless `fresh` (a new order).
  function backToAsk(noticeKey, fresh) {
    stopCountdown();
    cur.quote = null;
    cur.date = null;
    cur.unsure = false;
    cur.refusal = null;
    orderFieldsLocked(false);
    silence(byId("order-error"));
    silence(byId("quote-error"));
    if (fresh) {
      byId("quote-amount").value = "";
      byId("order-form").reset();
    }
    setCurrencyStep("ask", !noticeKey);
    if (noticeKey) {
      showError(byId("quote-notice"), noticeKey);
    } else {
      silence(byId("quote-notice"));
    }
  }

  // An order whose answer was lost (also before a reload, or a login): the
  // quote step again, unsure, offering only Try again (the same quote and
  // beneficiary, which the wallet answers with the same order) and Cancel.
  function openPendingOrder(pending) {
    cur.quote = pending.quote;
    cur.date = pending.date;
    cur.arrived = pending.arrived;
    cur.unsure = true;
    byId("order-name").value = pending.beneficiary_name;
    byId("order-id").value = pending.beneficiary_id;
    showQuote();
    setCurrencyStep("quote", false);
    showError(byId("order-error"), k("order.unsure"));
  }

  function enterCurrency() {
    var pending = cur.orderBusy ? null : readPending(PENDING_ORDER);
    if (pending) {
      openPendingOrder(pending);
    }
    if (cur.step === "quote") {
      showQuoteBalance();
      startCountdown();         // back to ask at once if the quote ran out meanwhile
    } else if (cur.step === "receipt" && cur.seen) {
      backToAsk(null, true);
    } else if (cur.step === "receipt") {
      cur.seen = true;
    }
    return loadPrices();
  }

  function onQuote(event) {
    event.preventDefault();
    if (cur.quoteBusy) {
      return;
    }
    var error = byId("quote-error");
    var currency = byId("quote-currency").value, amount = normalizeAmount(byId("quote-amount").value);
    silence(error);
    silence(byId("quote-notice"));
    if (!filled(currency) || amount === "") {
      showError(error, k("check.required"));
      return;
    }
    if (!isAmount(amount, FOREIGN_AMOUNT)) {
      showError(error, k("check.amount_foreign"));
      return;
    }
    cur.quoteBusy = true;
    setBusy(byId("quote-form"), [byId("quote-submit")], true);
    track(function () {
      var epoch = state.epoch;
      return api("POST", "/api/quote", {currency: currency, amount: amount}, isQuote)
        .then(function (answer) {
          if (epoch !== state.epoch) {
            return;
          }
          cur.quote = answer.data;
          cur.date = answer.date;
          cur.arrived = Date.now();
          cur.unsure = false;
          silence(byId("order-error"));
          showQuote();
          setCurrencyStep("quote", true);
          startCountdown();
        }, function (problem) {
          if (epoch !== state.epoch) {
            return;
          }
          var rule = explain(problem);
          var handled = common(rule);
          if (handled) {
            return handled;
          }
          showError(error, rule.key);
          if (rule.key === k("error.currency_unavailable")) {
            return loadPrices();
          }
        }).then(function () {
          if (epoch === state.epoch) {
            cur.quoteBusy = false;
            setBusy(byId("quote-form"), [byId("quote-submit")], false);
          }
        });
    });
  }

  function orderBusy(busy) {
    cur.orderBusy = busy;
    setBusy(byId("order-form"), [byId("order-submit"), byId("order-new-price"),
                                 byId("order-cancel")], busy);
  }

  function showReceipt(order) {
    var code = byId("receipt-code");
    code.textContent = "";
    // Two groups of 5, apart on screen only: the element's text, and a
    // copy of it, are exactly the code.
    code.appendChild(make("span", null, order.receipt_code.slice(0, 5)));
    code.appendChild(make("span", null, order.receipt_code.slice(5)));
    fill(byId("receipt-details"), k("receipt.details"),
         {amount: order.foreign_amount, currency: order.currency, name: order.beneficiary_name});
    fill(byId("receipt-cost"), k("receipt.cost"), {cost: money(order.cost_lyd)});
    fill(byId("receipt-status"), k("receipt.status"), {status: statusText(order.status)});
    byId("receipt-balance").hidden = true;
    byId("receipt-copied").textContent = "";
    cur.quote = null;
    cur.unsure = false;
    byId("order-form").reset();
    orderFieldsLocked(false);
    silence(byId("order-error"));
    setCurrencyStep("receipt", true);
  }

  function onOrder(event) {
    event.preventDefault();
    if (cur.orderBusy || !cur.quote || !state.me) {
      return;
    }
    var error = byId("order-error");
    var name = byId("order-name").value.trim(), id = byId("order-id").value.trim();
    if (name === "" || id === "") {
      showError(error, k("check.required"));
      return;
    }
    silence(error);
    cur.refusal = null;
    orderBusy(true);
    var quote = cur.quote, owner = state.me.phone;
    writePending(PENDING_ORDER, {owner: owner, quote: quote, date: cur.date,
                                 arrived: cur.arrived, beneficiary_name: name,
                                 beneficiary_id: id});
    track(function () {
      var epoch = state.epoch;
      var body = {quote_id: quote.quote_id, beneficiary_name: name, beneficiary_id: id,
                  from_phone: owner};
      return api("POST", "/api/orders", body, isOrder).then(function (answer) {
        if (epoch !== state.epoch) {
          return;
        }
        dropPending(PENDING_ORDER);
        showReceipt(answer.data);
        return refreshMe(epoch, function () {
          say(byId("receipt-balance"), k("receipt.balance"), {amount: money(state.me.balance.lyd)});
        });
      }, function (problem) {
        if (epoch !== state.epoch) {
          return;
        }
        var rule = explain(problem), result = outcome(problem);
        if (result === "login") {
          return toLogin(rule.key);      // the pending order waits for the login
        }
        if (result === "unsure") {
          cur.unsure = true;
          orderButtons();
          showError(error, k("order.unsure"));
          return;
        }
        // The wallet refused it: nothing was ordered.
        dropPending(PENDING_ORDER);
        cur.unsure = false;
        orderButtons();
        if (rule.action === "workers") {
          return workersOnly();
        }
        if (rule.action === "ask") {
          backToAsk(rule.key, false);
          if (rule.key === k("error.currency_unavailable")) {
            return loadPrices();
          }
          return;
        }
        showError(error, rule.key);
        cur.refusal = rule.key;
        if (rule.key === k("error.balance_too_low")) {
          return refreshMe(epoch, showQuoteBalance);
        }
      }).then(function () {
        if (epoch === state.epoch) {
          orderBusy(false);
        }
      });
    });
  }

  function canCopy() {
    return !!(navigator.clipboard && navigator.clipboard.writeText);
  }

  function onCopy() {
    var status = byId("receipt-copied");
    navigator.clipboard.writeText(byId("receipt-code").textContent).then(function () {
      status.textContent = t("receipt.copied");
      setTimeout(function () {
        status.textContent = "";
      }, 2000);
    }, function () {
      status.textContent = "";
    });
  }

  function wireCurrency() {
    enterScreen.currency = enterCurrency;
    leaveScreen.currency = stopCountdown;
    byId("quote-form").addEventListener("submit", onQuote);
    byId("order-form").addEventListener("submit", onOrder);
    byId("prices-refresh").addEventListener("click", function () {
      track(loadPrices);
    });
    byId("order-new-price").addEventListener("click", function () {
      if (!cur.orderBusy && !cur.unsure) {
        backToAsk(null, false);
      }
    });
    // Unsure, and the worker gives up: they are sent to their orders to see
    // whether it was placed.
    byId("order-cancel").addEventListener("click", function () {
      if (cur.orderBusy) {
        return;
      }
      track(function () {
        dropPending(PENDING_ORDER);
        backToAsk(null, false);
        return go("orders").then(function () {
          notify(k("order.cancelled_check"));
        });
      });
    });
    byId("receipt-new").addEventListener("click", function () {
      backToAsk(null, true);
    });
    byId("receipt-copy").hidden = !canCopy();
    byId("receipt-copy").addEventListener("click", onCopy);
  }

  // ---------- my orders ----------

  var orders = {busy: false};

  function resetOrders() {
    orders.busy = false;
    byId("orders-list").textContent = "";
    byId("orders-empty").hidden = true;
    byId("orders-more").hidden = true;
    silence(byId("orders-error"));
  }

  function orderItem(order) {
    var item = make("li", "order"), top = make("div", "order-top");
    item.setAttribute("data-receipt", order.receipt_code);
    item.setAttribute("data-status", order.status);
    top.appendChild(make("span", "order-code num", order.receipt_code));
    top.appendChild(make("span", "order-status", statusText(order.status)));
    item.appendChild(top);
    var line = make("p", "order-line"), cost = make("p", "order-cost");
    var date = make("p", "order-when order-date");
    fill(line, k("orders.line"),
         {amount: order.foreign_amount, currency: order.currency, name: order.beneficiary_name});
    fill(cost, k("receipt.cost"), {cost: money(order.cost_lyd)});
    fill(date, k("orders.date"), {time: when(order.created_at)});
    item.appendChild(line);
    item.appendChild(cost);
    item.appendChild(date);
    if (isText(order.paid_at)) {
      var paid = make("p", "order-when order-paid");
      fill(paid, k("orders.paid"), {time: when(order.paid_at)});
      item.appendChild(paid);
    }
    if (isText(order.cancelled_at)) {
      var cancelled = make("p", "order-when order-cancelled");
      fill(cancelled, k("orders.cancelled"), {time: when(order.cancelled_at)});
      item.appendChild(cancelled);
    }
    return item;
  }

  function loadOrders(limit) {
    if (orders.busy) {
      return Promise.resolve();
    }
    var list = byId("orders-list"), error = byId("orders-error"), epoch = state.epoch;
    var buttons = [byId("orders-refresh"), byId("orders-more")];
    orders.busy = true;
    setBusy(null, buttons, true);
    list.setAttribute("aria-busy", "true");
    silence(error);
    return api("GET", "/api/orders?limit=" + limit, null, isOrders).then(function (answer) {
      if (epoch !== state.epoch) {
        return;
      }
      var items = answer.data.orders;
      list.textContent = "";
      items.forEach(function (order) {
        list.appendChild(orderItem(order));
      });
      byId("orders-empty").hidden = items.length > 0;
      byId("orders-more").hidden = !(limit === PAGE && items.length >= PAGE);
    }, function (problem) {
      if (epoch !== state.epoch) {
        return;
      }
      var rule = explain(problem);
      return common(rule) || showError(error, rule.key);
    }).then(function () {
      if (epoch === state.epoch) {
        orders.busy = false;
        setBusy(null, buttons, false);
        list.setAttribute("aria-busy", "false");
      }
    });
  }

  function wireOrders() {
    enterScreen.orders = function () {
      return loadOrders(PAGE);
    };
    byId("orders-refresh").addEventListener("click", function () {
      track(function () {
        return loadOrders(PAGE);
      });
    });
    byId("orders-more").addEventListener("click", function () {
      track(function () {
        return loadOrders(PAGE_MORE);
      });
    });
  }

  // ---------- my account ----------

  var account = {passwordBusy: false, logoutBusy: false};

  function resetAccount() {
    account.passwordBusy = false;
    account.logoutBusy = false;
    byId("account-name").textContent = "";
    byId("account-phone").textContent = "";
    byId("password-form").reset();
    silence(byId("password-error"));
    silence(byId("password-done"));
    silence(byId("account-error"));
    byId("logout-all-confirm").hidden = true;
  }

  function enterAccount() {
    byId("account-name").textContent = state.me.name;
    byId("account-phone").textContent = phoneText(state.me.phone);
    byId("logout-all-confirm").hidden = true;
    silence(byId("account-error"));
    if (!account.passwordBusy) {        // a change under way still shows its answer
      silence(byId("password-error"));
      silence(byId("password-done"));
    }
  }

  function onPassword(event) {
    event.preventDefault();
    if (account.passwordBusy) {
      return;
    }
    var form = byId("password-form"), error = byId("password-error"), done = byId("password-done");
    var old = byId("password-old"), password = byId("password-new"), again = byId("password-new2");
    silence(error);
    silence(done);
    if (old.value === "" || password.value === "" || again.value === "") {
      showError(error, k("check.required"));
      return;
    }
    if (password.value !== again.value) {
      showError(error, k("check.passwords_differ"));
      return;
    }
    if (chars(password.value) < 10) {
      showError(error, k("check.password_short"));
      return;
    }
    account.passwordBusy = true;
    setBusy(form, [byId("password-submit")], true);
    track(function () {
      var epoch = state.epoch;
      var body = {old_password: old.value, new_password: password.value};
      return api("POST", "/api/change-password", body, isOk).then(function () {
        if (epoch === state.epoch) {
          form.reset();
          say(done, k("password.done"));
        }
      }, function (problem) {
        if (epoch !== state.epoch) {
          return;
        }
        var rule = explain(problem);
        var handled = common(rule);
        if (handled) {
          return handled;
        }
        if (rule.key === k("error.wrong_password")) {
          old.value = "";
        }
        showError(error, rule.lost ? k("password.unsure") : rule.key);
      }).then(function () {
        if (epoch === state.epoch) {
          account.passwordBusy = false;
          setBusy(form, [byId("password-submit")], false);
        }
      });
    });
  }

  function logoutButtons(busy) {
    account.logoutBusy = busy;
    setBusy(null, [byId("logout"), byId("logout-all"), byId("logout-all-yes"),
                   byId("logout-all-no")], busy);
  }

  // Log out: the wallet clears the cookie (only it can: the cookie is
  // HttpOnly). A 401 means the session had ended already: logged out too.
  function onLogout() {
    if (account.logoutBusy) {
      return;
    }
    logoutButtons(true);
    silence(byId("account-error"));
    track(function () {
      var epoch = state.epoch;
      return api("POST", "/api/logout", {}, isOk).then(function () {
        return loggedOut(k("logout.done"));
      }, function (problem) {
        if (epoch !== state.epoch) {
          return;
        }
        if (problem.kind === "http" && problem.status === 401 && problem.type !== null) {
          return loggedOut(k("logout.done"));
        }
        logoutButtons(false);
        showError(byId("account-error"), explain(problem).key);
      });
    });
  }

  // Log out everywhere. Answered 401, the session had ended and the wallet
  // left the cookie as it was, so /api/logout (which always clears it)
  // follows.
  function onLogoutAll() {
    if (account.logoutBusy) {
      return;
    }
    logoutButtons(true);
    silence(byId("account-error"));
    track(function () {
      var epoch = state.epoch;
      return api("POST", "/api/logout-everywhere", {}, isOk).then(function () {
        return loggedOut(k("logout.all_done"));
      }, function (problem) {
        if (epoch !== state.epoch) {
          return;
        }
        if (problem.kind === "http" && problem.status === 401 && problem.type !== null) {
          var done = function () {
            return loggedOut(k("logout.all_done"));
          };
          return api("POST", "/api/logout", {}, isOk).then(done, done);
        }
        logoutButtons(false);
        showError(byId("account-error"), explain(problem).key);
      });
    });
  }

  function wireAccount() {
    enterScreen.account = enterAccount;
    byId("password-form").addEventListener("submit", onPassword);
    byId("logout").addEventListener("click", onLogout);
    byId("logout-all").addEventListener("click", function () {
      var box = byId("logout-all-confirm");
      box.hidden = false;
      byId("logout-all-question").focus();
      // The focus brings only the question into view; its two buttons come
      // with it, above the bottom bar.
      box.scrollIntoView({block: "nearest"});
    });
    byId("logout-all-no").addEventListener("click", function () {
      byId("logout-all-confirm").hidden = true;
      byId("logout-all").focus();
    });
    byId("logout-all-yes").addEventListener("click", onLogoutAll);
  }

  // ---------- start ----------

  function start() {
    sections = {
      loading: byId("screen-loading"),
      offline: byId("screen-offline"),
      login: byId("screen-login"),
      setup: byId("screen-setup"),
      home: byId("screen-home"),
      send: byId("screen-send"),
      currency: byId("screen-currency"),
      orders: byId("screen-orders"),
      account: byId("screen-account")
    };
    navLinks = Array.prototype.slice.call(byId("nav").querySelectorAll("a[data-nav]"));
    chooseLanguage("en");
    wireLogin();
    wireHome();
    wireSend();
    wireCurrency();
    wireOrders();
    wireAccount();
    document.addEventListener("click", onLinkClick);
    window.addEventListener("hashchange", onHashChange);
    boot();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
}());
