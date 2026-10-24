// The office's screens: everything app/static/admin.html does. Plain
// JavaScript with no outside code, like the workers' app.js: one helper, api,
// makes every request; every text goes in with textContent, never as HTML;
// the API is only called because someone did something (opened a screen,
// pressed a button), never on a timer.
//
// Texts: every text is written here (or in admin.html) in English, as the
// argument of t ("Block {name}?", with {placeholders}) or, in a table, of k.
// admin-strings.js gives each English text in Arabic and French; the admin
// sees their account's language, and English for a text it lacks
// (tests/test_admin_pages.py checks every text has both).
//
// The office reads the wallet's own error text (in English): it says exactly
// what was refused ("order 4 is not placed"), which is what staff need.
// Only a lost connection, a timeout and a logged-out session get texts of
// their own.
(function () {
  "use strict";

  var TIMEOUT_MS = 20000;
  var PAGE = 50;            // rows loaded at first
  var PAGE_MORE = 200;      // "Show more": the most the API gives

  // ---------- strings ----------

  var STRINGS = window.ADMIN_STRINGS || {};
  var LANGUAGES = {en: "English", ar: "العربية", fr: "Français"};
  var DIRECTION = {ar: "rtl"};
  var LOCALE = {en: "en-GB", ar: "ar-LY", fr: "fr-FR"};    // dates with the digits 0-9
  var PLACEHOLDER = /\{([a-z_]+)\}/g;
  var lang = "en";

  function has(object, key) {
    return object !== null && object !== undefined &&
      Object.prototype.hasOwnProperty.call(object, key);
  }

  // An English text kept in a table, translated where it is shown (t).
  function k(english) {
    return english;
  }

  // The English text in the admin's language, with its {placeholders}
  // filled in. Right to left, each value is isolated (U+2068 to U+2069), so
  // a phone number or an amount keeps its own order inside Arabic text.
  function t(english, params) {
    var table = has(STRINGS, lang) ? STRINGS[lang] : null;
    var text = table && has(table, english) ? table[english] : english;
    var isolate = DIRECTION[lang] === "rtl";
    return text.replace(PLACEHOLDER, function (whole, name) {
      if (!has(params, name)) {
        return whole;
      }
      return isolate ? "\u2068" + String(params[name]) + "\u2069" : String(params[name]);
    });
  }

  // admin.html's texts: each element marked data-t keeps its English text
  // (data-en, noted the first time), and data-t-label its aria-label.
  function applyStrings() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-t]"), function (el) {
      if (!el.hasAttribute("data-en")) {
        el.setAttribute("data-en", el.textContent.replace(/\s+/g, " ").trim());
      }
      el.textContent = t(el.getAttribute("data-en"));
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-t-label]"), function (el) {
      if (!el.hasAttribute("data-en-label")) {
        el.setAttribute("data-en-label", el.getAttribute("aria-label"));
      }
      el.setAttribute("aria-label", t(el.getAttribute("data-en-label")));
    });
  }

  function chooseLanguage(code) {
    lang = has(LANGUAGES, code) && (code === "en" || has(STRINGS, code)) ? code : "en";
    document.documentElement.setAttribute("lang", lang);
    document.documentElement.setAttribute("dir", DIRECTION[lang] || "ltr");
    timeFormat = null;
    applyStrings();
    Array.prototype.forEach.call(byId("lang-switch").querySelectorAll("button"),
                                 function (button) {
      button.setAttribute("aria-pressed",
                          button.getAttribute("data-lang") === lang ? "true" : "false");
    });
  }

  // Before a login: the language picked on the switch, else the first of the
  // browser's languages there is, else English. Logged in: the admin's own.
  var picked = null;

  function preferredLanguage() {
    if (picked) {
      return picked;
    }
    var wanted = navigator.languages || [navigator.language || ""];
    for (var i = 0; i < wanted.length; i += 1) {
      var code = String(wanted[i]).slice(0, 2).toLowerCase();
      if (has(LANGUAGES, code) && (code === "en" || has(STRINGS, code))) {
        return code;
      }
    }
    return "en";
  }

  function wireLanguageSwitch() {
    Array.prototype.forEach.call(byId("lang-switch").querySelectorAll("button"),
                                 function (button) {
      var code = button.getAttribute("data-lang");
      button.textContent = LANGUAGES[code];
      button.hidden = code !== "en" && !has(STRINGS, code);
      button.addEventListener("click", function () {
        picked = code;
        chooseLanguage(code);
      });
    });
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
    if (text !== undefined && text !== null) {
      el.textContent = String(text);
    }
    return el;
  }

  function clear(el) {
    while (el.firstChild) {
      el.removeChild(el.firstChild);
    }
  }

  function say(el, text) {
    el.textContent = text;
    el.hidden = false;
  }

  function showError(el, text) {
    say(el, text);
    el.focus();
  }

  function silence(el) {
    el.textContent = "";
    el.hidden = true;
  }

  function notify(text) {
    showError(byId("notice"), text);
  }

  function filled(text) {
    return typeof text === "string" && text.trim() !== "";
  }

  function cell(row, text, className) {
    var td = make("td", className, text);
    row.appendChild(td);
    return td;
  }

  function button(text, className, action) {
    var b = make("button", "button small " + (className || "secondary"), text);
    b.type = "button";
    b.addEventListener("click", action);
    return b;
  }

  function option(select, value, text) {
    var o = make("option", null, text);
    o.value = value;
    select.appendChild(o);
    return o;
  }

  // Keeps what was picked when the choices are filled again.
  function fillSelect(select, choices) {
    var before = select.value;
    clear(select);
    choices.forEach(function (choice) {
      option(select, choice[0], choice[1]);
    });
    if (choices.some(function (choice) { return choice[0] === before; })) {
      select.value = before;
    }
  }

  // ---------- formatting ----------

  // Dirhams (1 LYD = 1000) as "1,234.500 LYD", with whole numbers only.
  function lyd(dirhams) {
    var negative = dirhams < 0, n = Math.abs(dirhams);
    var whole = String(Math.floor(n / 1000)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    var fraction = String(n % 1000);
    while (fraction.length < 3) {
      fraction = "0" + fraction;
    }
    return t("{amount} LYD", {amount: (negative ? "−" : "") + whole + "." + fraction});
  }

  function signed(dirhams) {
    return (dirhams > 0 ? "+" : "") + lyd(dirhams);
  }

  // A typed LYD amount ("12.5") as the wallet takes it, or null.
  var DIGITS = "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹";
  function amountText(text) {
    var out = "";
    Array.from(text.trim()).forEach(function (c) {
      var digit = DIGITS.indexOf(c);
      out += digit >= 0 ? String(digit % 10) : c === "٫" || c === "," ? "." : c;
    });
    return out;
  }

  function isLyd(text) {
    return /^\d+(\.\d{1,3})?$/.test(text) && /[1-9]/.test(text);
  }

  // A checked amount ("12.5") in dirhams (12500), from the text.
  function dirhamsOf(text) {
    var parts = text.split(".");
    return Number(parts[0]) * 1000 + Number(((parts[1] || "") + "000").slice(0, 3));
  }

  // "12.5" as "12.500 LYD", from the text (nothing rounded).
  function typedLyd(text) {
    return lyd(dirhamsOf(text));
  }

  function parseTime(text) {
    return typeof text === "string" ? Date.parse(text.replace(/(\.\d{3})\d+/, "$1")) : NaN;
  }

  var timeFormat = null;     // for the language shown (chooseLanguage clears it)

  function when(text) {
    var ms = parseTime(text);
    if (isNaN(ms)) {
      return String(text || "");
    }
    if (!timeFormat) {
      try {
        timeFormat = new Intl.DateTimeFormat(LOCALE[lang], {dateStyle: "medium",
                                                            timeStyle: "short"});
      } catch (e) {
        timeFormat = new Intl.DateTimeFormat(LOCALE[lang]);
      }
    }
    return timeFormat.format(new Date(ms));
  }

  function phoneText(phone) {
    var match = /^\+218(9\d)(\d{3})(\d{4})$/.exec(phone || "");
    return match ? "0" + match[1] + " " + match[2] + " " + match[3] : String(phone || "");
  }

  function newKey() {
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, function (b) {
      return (b < 16 ? "0" : "") + b.toString(16);
    }).join("");
  }

  var KIND = {
    deposit: k("Deposit"), payroll: k("Salary"), transfer: k("Transfer between workers"),
    order: k("Currency order"), withdrawal: k("Cash withdrawal"), reversal: k("Reversal"),
    settlement: k("Paid to supplier"), payout: k("Order paid out"),
    capital: k("Owner's money"), move: k("Moved between bank and tills")
  };

  function kindText(kind) {
    return has(KIND, kind) ? t(KIND[kind]) : kind;
  }

  // ---------- calls ----------

  var pending = 0;

  function busy(change) {
    pending += change;
    document.body.setAttribute("data-pending", String(pending));
  }

  // One call. Resolves to the JSON of a 200; rejects with {kind: "network",
  // "timeout" or "http", status, type, message}.
  function api(method, path, body) {
    var controller = new AbortController();
    var timer = setTimeout(function () {
      controller.abort();
    }, TIMEOUT_MS);
    var options = {
      method: method, credentials: "same-origin", cache: "no-store", redirect: "error",
      headers: {"Accept": "application/json"}, signal: controller.signal
    };
    if (method === "POST") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body || {});
    }
    busy(1);
    return fetch(path, options).then(function (response) {
      return response.text().then(function (text) {
        var data = null;
        try {
          data = JSON.parse(text);
        } catch (e) {
          data = null;
        }
        if (response.status === 200 && data && typeof data === "object") {
          return data;
        }
        var wallet = data && typeof data.error === "string";
        throw {kind: "http", status: response.status, type: wallet ? data.type : null,
               message: wallet ? data.error : ""};
      });
    }, function () {
      throw {kind: controller.signal.aborted ? "timeout" : "network", status: 0,
             type: null, message: ""};
    }).then(function (data) {
      clearTimeout(timer);
      busy(-1);
      return data;
    }, function (problem) {
      clearTimeout(timer);
      busy(-1);
      throw problem;
    });
  }

  function get(path) {
    return api("GET", path);
  }

  function post(path, body) {
    return api("POST", path, body);
  }

  // Whether a request may have been done although no answer came.
  function lost(problem) {
    return problem.kind !== "http" || problem.status >= 500;
  }

  function errorText(problem) {
    if (problem.kind === "network") {
      return t("No connection to the wallet. Check the internet and try again.");
    }
    if (problem.kind === "timeout") {
      return t("The wallet didn't answer in time. Try again.");
    }
    if (problem.status >= 500 || !problem.message) {
      return t("Something went wrong on the wallet's side ({status}). Try again.",
               {status: problem.status});
    }
    var text = problem.message;
    text = text.charAt(0).toUpperCase() + text.slice(1) + (/[.!?]$/.test(text) ? "" : ".");
    return lang === "en" ? text : t("The wallet says: {reason}", {reason: text});
  }

  // A failed call: a session that ended goes to the login screen (true);
  // anything else is shown in the line (false).
  function handle(problem, line) {
    if (problem.kind === "http" && problem.status === 401 && problem.type === "NotLoggedIn" &&
        state.me) {
      loggedOut(t("Your session ended. Please log in again."));
      return true;
    }
    if (problem.kind === "http" && problem.status === 403 &&
        problem.type === "PermissionDenied" && state.me) {
      loggedOut(t("That account isn't an office account any more."));
      return true;
    }
    showError(line, errorText(problem));
    return false;
  }

  // ---------- state and screens ----------

  var state = {
    me: null,
    users: [],          // the last list of accounts
    holdings: [],       // BANK and the tills' codes
    suppliers: [],
    currencies: [],
    screen: null,
    param: null
  };

  var SCREENS = ["loading", "offline", "login", "setup", "overview", "workers", "worker",
                 "money", "orders", "prices", "suppliers", "supplier", "log", "account"];
  var NAV_OF = {worker: "workers", supplier: "suppliers"};

  function show(name) {
    SCREENS.forEach(function (screen) {
      byId("screen-" + screen).hidden = screen !== name;
    });
    document.body.setAttribute("data-screen", name);
    state.screen = name;
    var loggedIn = state.me !== null && name !== "login" && name !== "setup";
    byId("nav").hidden = !loggedIn;
    byId("lang-switch").hidden = name !== "login" && name !== "setup";
    Array.prototype.forEach.call(document.querySelectorAll("#nav a"), function (a) {
      if (a.getAttribute("data-nav") === (NAV_OF[name] || name)) {
        a.setAttribute("aria-current", "page");
      } else {
        a.removeAttribute("aria-current");
      }
    });
    var heading = byId("screen-" + name).querySelector("h1");
    if (heading && byId("notice").hidden) {
      heading.focus();
    }
  }

  function route() {
    var parts = (location.hash || "").replace(/^#\/?/, "").split("/");
    var name = parts[0], param = parts[1] ? Number(parts[1]) : null;
    silence(byId("notice"));
    closeAsk();
    if (!state.me) {
      show(name === "setup" ? "setup" : "login");
      return;
    }
    if ((name === "worker" || name === "supplier") && !(param > 0)) {
      name = name + "s";
    }
    if (!OPEN[name]) {
      location.replace("#/overview");
      return;
    }
    state.param = param;
    show(name);
    OPEN[name](param);
  }

  function go(hash) {
    if (location.hash === hash) {
      route();
    } else {
      location.hash = hash;
    }
  }

  function loggedIn(me) {
    state.me = me;
    chooseLanguage(me.language);
    var who = byId("bar-who");
    who.textContent = me.name;
    who.hidden = false;
    byId("account-name").textContent = me.name;
    byId("account-phone").textContent = phoneText(me.phone);
  }

  // Logged out, the screens stay in the language they were in.
  function loggedOut(message) {
    state.me = null;
    byId("bar-who").hidden = true;
    closeAsk();
    if (location.hash !== "#/login") {
      history.replaceState(null, "", "#/login");
    }
    show("login");
    if (message) {
      notify(message);
    }
  }

  function start() {
    chooseLanguage(state.me ? state.me.language : preferredLanguage());
    show("loading");
    get("/api/admin/me").then(function (me) {
      loggedIn(me);
      route();
    }, function (problem) {
      if (problem.kind === "http" && (problem.status === 401 || problem.status === 403)) {
        if (problem.status === 403) {
          // A worker's session in this browser: not for these screens.
          post("/api/logout").catch(function () {});
        }
        route();
        return;
      }
      byId("offline-text").textContent = errorText(problem);
      show("offline");
    });
  }

  // ---------- one question at a time ----------
  // ask({title, facts: [[label, value]], text, reason: true, supplier: [...],
  //      ref: true, rate: true, yes, danger, act(values) -> promise})
  // act's promise resolving closes the box; rejecting shows the error and
  // keeps it open, so the same press can be tried again (a payment keeps
  // its key, so it is never done twice).

  var asking = null;

  function ask(question) {
    asking = question;
    byId("ask-title").textContent = question.title;
    var facts = byId("ask-facts");
    clear(facts);
    (question.facts || []).forEach(function (fact) {
      var row = make("div");
      row.appendChild(make("dt", null, fact[0]));
      row.appendChild(make("dd", fact[2] || null, fact[1]));
      facts.appendChild(row);
    });
    facts.hidden = !(question.facts && question.facts.length);
    var text = byId("ask-text");
    text.textContent = question.text || "";
    text.hidden = !question.text;
    byId("ask-reason-field").hidden = !question.reason;
    byId("ask-reason").value = "";
    byId("ask-supplier-field").hidden = !question.supplier;
    if (question.supplier) {
      fillSelect(byId("ask-supplier"), question.supplier);
    }
    byId("ask-ref-field").hidden = !question.ref;
    byId("ask-ref").value = "";
    byId("ask-rate-field").hidden = !question.rate;
    byId("ask-rate").value = "";
    silence(byId("ask-error"));
    var yes = byId("ask-yes");
    yes.textContent = question.yes;
    yes.className = "button " + (question.danger ? "danger" : "primary");
    yes.disabled = false;
    byId("ask").hidden = false;
    var first = byId("ask-form").querySelector(".field:not([hidden]) input, " +
                                              ".field:not([hidden]) select");
    (first || byId("ask-title")).focus();
  }

  function closeAsk() {
    asking = null;
    byId("ask").hidden = true;
  }

  byId("ask-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var question = asking, error = byId("ask-error");
    if (!question || byId("ask-yes").disabled) {
      return;
    }
    var values = {
      reason: byId("ask-reason").value.trim(),
      supplier_id: question.supplier ? Number(byId("ask-supplier").value) : null,
      payout_ref: byId("ask-ref").value.trim(),
      rate: amountText(byId("ask-rate").value)
    };
    if ((question.reason && !values.reason) || (question.ref && !values.payout_ref) ||
        (question.supplier && !(values.supplier_id > 0))) {
      showError(error, t("Fill in every field first."));
      return;
    }
    silence(error);
    byId("ask-yes").disabled = true;
    question.act(values).then(function () {
      if (asking === question) {
        closeAsk();
      }
    }, function (problem) {
      byId("ask-yes").disabled = false;
      if (asking !== question || handle(problem, error)) {
        return;
      }
      if (question.key && lost(problem)) {
        showError(error, errorText(problem) + " " + t("It may have gone through: press " +
                  "\"{button}\" again to make sure. It won't be done twice.",
                                                      {button: question.yes}));
      }
    });
  });

  byId("ask-no").addEventListener("click", closeAsk);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && asking) {
      closeAsk();
    }
  });

  // A form whose submit makes one call: busy while it runs, its error line
  // filled when it fails.
  function onSubmit(formId, errorId, action) {
    var form = byId(formId), error = byId(errorId), running = false;
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (running) {
        return;
      }
      silence(error);
      var call = action();
      if (!call) {
        return;
      }
      running = true;
      form.setAttribute("aria-busy", "true");
      call.then(function () {
        running = false;
        form.setAttribute("aria-busy", "false");
      }, function (problem) {
        running = false;
        form.setAttribute("aria-busy", "false");
        handle(problem, error);
      });
    });
    return error;
  }

  function onClick(id, errorId, action) {
    var el = byId(id), running = false;
    el.addEventListener("click", function () {
      if (running) {
        return;
      }
      if (errorId) {
        silence(byId(errorId));
      }
      running = true;
      el.disabled = true;
      action().then(function () {
        running = false;
        el.disabled = false;
      }, function (problem) {
        running = false;
        el.disabled = false;
        handle(problem, byId(errorId));
      });
    });
  }

  function done(id, text) {
    say(byId(id), text);
  }

  // ---------- login, setup, account ----------

  onSubmit("login-form", "login-error", function () {
    var phone = byId("login-phone").value, password = byId("login-password").value;
    if (!filled(phone) || !password) {
      showError(byId("login-error"), t("Fill in your phone number and password."));
      return null;
    }
    return post("/api/login", {phone: phone, password: password}).then(afterLogin);
  });

  function afterLogin() {
    byId("login-password").value = "";
    return get("/api/admin/me").then(function (me) {
      loggedIn(me);
      go(location.hash === "#/login" || location.hash === "#/setup" || !location.hash ?
         "#/overview" : location.hash);
    }, function (problem) {
      if (problem.kind === "http" && problem.status === 403) {
        return post("/api/logout").catch(function () {}).then(function () {
          show("login");
          showError(byId("login-error"), t("This is a worker's account. Workers use the " +
                    "wallet at the main address, not the office screens."));
        });
      }
      throw problem;
    });
  }

  onSubmit("setup-form", "setup-error", function () {
    var phone = byId("setup-phone").value, code = byId("setup-code").value;
    var password = byId("setup-password").value;
    if (!filled(phone) || !filled(code) || !password) {
      showError(byId("setup-error"), t("Fill in every field."));
      return null;
    }
    if (password !== byId("setup-password2").value) {
      showError(byId("setup-error"), t("The two new passwords are different."));
      return null;
    }
    return post("/api/setup-password", {phone: phone, code: code, password: password})
      .then(function () {
        byId("setup-password").value = byId("setup-password2").value = "";
        byId("setup-code").value = "";
        return afterLogin();
      });
  });

  onSubmit("password-form", "password-error", function () {
    var old = byId("password-old").value, fresh = byId("password-new").value;
    silence(byId("password-done"));
    if (!old || !fresh) {
      showError(byId("password-error"), t("Fill in every field."));
      return null;
    }
    if (fresh !== byId("password-new2").value) {
      showError(byId("password-error"), t("The two new passwords are different."));
      return null;
    }
    return post("/api/change-password", {old_password: old, new_password: fresh})
      .then(function () {
        byId("password-form").reset();
        done("password-done",
             t("Your password is changed. You're logged out everywhere else."));
      });
  });

  // The admin's own language: saved on their account, and shown at once.
  onSubmit("my-language-form", "my-language-error", function () {
    var code = byId("my-language").value;
    return post("/api/admin/users/" + state.me.id + "/language", {language: code})
      .then(function () {
        state.me.language = code;
        chooseLanguage(code);
        notify(t("The office screens are now in {language}.", {language: LANGUAGES[code]}));
      });
  });

  onClick("logout", "account-error", function () {
    return post("/api/logout").then(function () {
      loggedOut(t("You're logged out."));
    });
  });

  onClick("logout-all", "account-error", function () {
    return post("/api/logout-everywhere").then(function () {
      loggedOut(t("You're logged out on every device."));
    });
  });

  byId("offline-retry").addEventListener("click", start);

  // ---------- overview ----------

  function loadHealth() {
    return get("/api/admin/health").then(function (health) {
      var codes = Object.keys(health.holdings || {});
      state.holdings = codes;
      var checks = byId("overview-checks"), problems = byId("overview-problems");
      clear(problems);
      if (health.problems.length) {
        checks.textContent = health.problems.length === 1 ?
          t("The books' checks found 1 problem:") :
          t("The books' checks found {count} problems:", {count: health.problems.length});
        checks.className = "check-line bad";
        health.problems.forEach(function (problem) {
          problems.appendChild(make("li", null, problem));
        });
      } else {
        checks.textContent = t("The books' checks are all fine.");
        checks.className = "check-line good";
      }
      problems.hidden = !health.problems.length;
      byId("overview-owed").textContent = lyd(health.owed_to_workers.dirhams);
      byId("overview-margin").textContent = lyd(health.margin.dirhams);
      byId("overview-fees").textContent = lyd(health.fees.dirhams);
      var rows = byId("overview-holdings");
      clear(rows);
      codes.forEach(function (code) {
        var row = make("tr");
        cell(row, code, "num");
        cell(row, lyd(health.holdings[code].dirhams), "num end");
        rows.appendChild(row);
      });
      byId("overview-maintenance").textContent = maintenanceText(health.maintenance);
    });
  }

  function maintenanceText(m) {
    if (!m) {
      return t("This server doesn't run the hourly checks or the backups (the test " +
        "server). The production server (app.serve) does.");
    }
    var parts = [];
    parts.push(!m.last_check ? t("No hourly check yet.") : m.last_check.ok ?
               t("Last hourly check {time}: fine.", {time: when(m.last_check.at)}) :
               t("Last hourly check {time}: found problems.", {time: when(m.last_check.at)}));
    if (!m.backups_on) {
      parts.push(t("Backups are off."));
    } else {
      parts.push(m.last_good_backup_at ?
                 t("Last good backup {time}.", {time: when(m.last_good_backup_at)}) :
                 t("No good backup yet."));
      if (m.backup_overdue) {
        parts.push(t("A backup is overdue: check the server's log."));
      }
    }
    return parts.join(" ");
  }

  onClick("overview-refresh", "overview-error", loadHealth);

  onSubmit("till-form", "till-error", function () {
    var name = byId("till-name").value;
    silence(byId("till-done"));
    if (!filled(name)) {
      showError(byId("till-error"), t("Type the till's name."));
      return null;
    }
    return post("/api/admin/tills", {name: name}).then(function (made) {
      byId("till-name").value = "";
      done("till-done", t("{code} is added.", {code: made.code}));
      return loadHealth();
    });
  });

  onSubmit("margin-form", "margin-error", function () {
    var since = byId("margin-since").value, until = byId("margin-until").value;
    var query = [];
    if (since) {
      query.push("since=" + encodeURIComponent(since));
    }
    if (until) {
      query.push("until=" + encodeURIComponent(until));
    }
    return get("/api/admin/margin" + (query.length ? "?" + query.join("&") : ""))
      .then(function (report) {
        var rows = byId("margin-rows");
        clear(rows);
        report.rows.forEach(function (r) {
          var row = make("tr");
          cell(row, r.currency, "num");
          cell(row, r.supplier, null).dir = "auto";
          cell(row, r.orders, "num end");
          cell(row, r.foreign_amount + " " + r.currency, "num end");
          cell(row, lyd(r.charged), "num end");
          cell(row, lyd(r.supplier_cost), "num end");
          cell(row, lyd(r.margin), "num end strong");
          rows.appendChild(row);
        });
        byId("margin-table").hidden = !report.rows.length;
        byId("margin-empty").hidden = !!report.rows.length;
      });
  });

  // ---------- accounts ----------

  function loadUsers() {
    return get("/api/admin/users").then(function (data) {
      state.users = data.users;
      return data.users;
    });
  }

  function userById(id) {
    for (var i = 0; i < state.users.length; i += 1) {
      if (state.users[i].id === id) {
        return state.users[i];
      }
    }
    return null;
  }

  function statusText(user) {
    var parts = [];
    if (user.role === "admin") {
      parts.push(t("Office staff"));
    }
    if (!user.active) {
      parts.push(t("Closed"));
    } else if (user.blocked) {
      parts.push(t("Blocked"));
    } else if (!user.has_password) {
      parts.push(t("Waiting for a password"));
    } else {
      parts.push(t("Active"));
    }
    return parts.join(" · ");
  }

  function statusClass(user) {
    return !user.active ? "tag tag-off" : user.blocked ? "tag tag-bad" :
      !user.has_password ? "tag tag-wait" : "tag tag-good";
  }

  function drawUsers() {
    var search = byId("workers-search").value.trim().toLowerCase();
    var digits = search.replace(/\D/g, "").replace(/^0/, "");
    var rows = byId("workers-rows"), shown = 0;
    clear(rows);
    state.users.forEach(function (user) {
      var matches = !search || user.name.toLowerCase().indexOf(search) >= 0 ||
        (digits.length >= 3 && user.phone.replace(/\D/g, "").indexOf(digits) >= 0);
      if (!matches) {
        return;
      }
      shown += 1;
      var row = make("tr");
      var name = cell(row, null);
      var link = make("a", null, user.name);
      link.href = "#/worker/" + user.id;
      link.dir = "auto";
      name.appendChild(link);
      cell(row, phoneText(user.phone), "num");
      cell(row, user.role === "admin" ? "" : t("{amount} LYD", {amount: user.balance_lyd}),
           "num end");
      cell(row, null).appendChild(make("span", statusClass(user), statusText(user)));
      rows.appendChild(row);
    });
    byId("workers-empty").hidden = shown > 0;
  }

  byId("workers-search").addEventListener("input", drawUsers);

  byId("workers-add-open").addEventListener("click", function () {
    byId("worker-add").hidden = false;
    byId("code-box").hidden = true;
    byId("worker-add-name").focus();
  });

  byId("worker-add-close").addEventListener("click", function () {
    byId("worker-add").hidden = true;
    byId("workers-add-open").focus();
  });

  function showCode(box, title, value, text, user) {
    byId(box + "-value").textContent = value;
    byId(box + "-text").textContent = t("Give it to {name} in person or by WhatsApp. " +
      "They use it once, with their phone number, to choose their password, within 3 days. " +
      "It isn't shown again; a new code replaces it.", {name: user});
    if (title) {
      byId(box + "-title").textContent = title;
    }
    byId(box === "code" ? "code-box" : "worker-code-box").hidden = false;
    byId(box + "-title").focus();
  }

  onSubmit("worker-add-form", "worker-add-error", function () {
    var name = byId("worker-add-name").value, phone = byId("worker-add-phone").value;
    if (!filled(name) || !filled(phone)) {
      showError(byId("worker-add-error"), t("Fill in the name and the phone number."));
      return null;
    }
    var body = {name: name.trim(), phone: phone, role: byId("worker-add-role").value,
                language: byId("worker-add-language").value};
    if (filled(byId("worker-add-whatsapp").value)) {
      body.whatsapp = byId("worker-add-whatsapp").value;
    }
    return post("/api/admin/users", body).then(function (made) {
      byId("worker-add-form").reset();
      byId("worker-add").hidden = true;
      showCode("code", t("{name}'s account is ready", {name: body.name}), made.setup_code,
               "", body.name);
      return loadUsers().then(drawUsers);
    });
  });

  byId("code-done").addEventListener("click", function () {
    byId("code-box").hidden = true;
    byId("code-value").textContent = "";
    byId("workers-add-open").focus();
  });

  byId("worker-code-done").addEventListener("click", function () {
    byId("worker-code-box").hidden = true;
    byId("worker-code-value").textContent = "";
  });

  function openWorkers() {
    silence(byId("workers-error"));
    return loadUsers().then(drawUsers, function (problem) {
      handle(problem, byId("workers-error"));
    });
  }

  // ---------- one account ----------

  var worker = {id: null, limit: PAGE, seq: 0};

  function holdingChoices(tillsOnly) {
    return state.holdings.filter(function (code) {
      return !tillsOnly || code.indexOf("TILL_") === 0;
    }).map(function (code) {
      return [code, code === "BANK" ? t("Bank (BANK)") : code];
    });
  }

  function loadHoldings() {
    return get("/api/admin/health").then(function (health) {
      state.holdings = Object.keys(health.holdings || {});
    });
  }

  function openWorker(id) {
    worker.id = id;
    worker.limit = PAGE;
    ["worker-error", "deposit-error", "withdraw-error", "whatsapp-error", "whatsapp-done",
     "language-error", "language-done"].forEach(function (line) { silence(byId(line)); });
    byId("worker-code-box").hidden = true;
    byId("deposit-form").reset();
    byId("withdraw-form").reset();
    return loadWorker();
  }

  function loadWorker() {
    var id = worker.id, seq = (worker.seq += 1);
    return Promise.all([loadUsers(), loadHoldings(),
                        get("/api/admin/users/" + id + "/statement?limit=" + worker.limit)])
      .then(function (got) {
        if (seq !== worker.seq || state.param !== id) {
          return;
        }
        var user = userById(id);
        if (!user) {
          byId("worker-name").textContent = t("No such account");
          return;
        }
        drawWorker(user, got[2]);
      }, function (problem) {
        if (seq === worker.seq) {
          handle(problem, byId("worker-error"));
        }
      });
  }

  function drawWorker(user, statement) {
    var isAdmin = user.role === "admin";
    byId("worker-name").textContent = user.name;
    byId("worker-phone").textContent = phoneText(user.phone);
    byId("worker-whatsapp").textContent = user.whatsapp || t("None");
    var status = byId("worker-status");
    clear(status);
    status.appendChild(make("span", statusClass(user), statusText(user)));
    if (user.blocked && user.blocked_reason) {
      status.appendChild(make("span", "muted small block-reason",
                              t("Blocked {time}: {reason}", {time: when(user.blocked_at),
                                                             reason: user.blocked_reason})));
    }
    byId("worker-balance").textContent = isAdmin ? t("Office accounts hold no money") :
      lyd(statement.balance.dirhams);
    byId("whatsapp-number").value = user.whatsapp || "";
    byId("worker-language").textContent = LANGUAGES[user.language] || user.language;
    byId("language-choice").value = user.language;
    byId("worker-money").hidden = isAdmin || !user.active;
    fillSelect(byId("deposit-into"), holdingChoices(false));
    var tills = holdingChoices(true);
    fillSelect(byId("withdraw-till"), tills);
    byId("withdraw-submit").disabled = !tills.length;
    if (!tills.length) {
      byId("withdraw-till").title = t("Add a till on the Overview first.");
    }
    drawWorkerActions(user);

    var rows = byId("worker-rows");
    clear(rows);
    statement.entries.forEach(function (entry) {
      var row = make("tr");
      cell(row, when(entry.created_at), "nowrap");
      var what = cell(row, kindText(entry.kind));
      var detail = [entry.reference, entry.note].filter(filled).join(" · ");
      if (detail) {
        what.appendChild(make("span", "muted small detail", detail)).dir = "auto";
      }
      cell(row, signed(entry.amount), "num end " + (entry.amount < 0 ? "out" : "in"));
      cell(row, lyd(entry.balance_after), "num end");
      var actions = cell(row, null, "end");
      if (entry.kind !== "reversal" && entry.kind !== "order" && entry.kind !== "payout") {
        actions.appendChild(button(t("Reverse"), "danger-outline", function () {
          askReverse(entry, user);
        }));
      }
      rows.appendChild(row);
    });
    byId("worker-empty").hidden = statement.entries.length > 0;
    byId("worker-more").hidden = statement.entries.length < worker.limit ||
      worker.limit >= PAGE_MORE;
  }

  function drawWorkerActions(user) {
    var box = byId("worker-actions");
    clear(box);
    var who = {name: user.name};
    function action(label, path, title, text, doneText, danger) {
      box.appendChild(button(label, danger ? "danger-outline" : "secondary", function () {
        ask({title: title, text: text, reason: true, yes: label, danger: danger,
             act: function (values) {
               return post("/api/admin/users/" + user.id + "/" + path,
                           {reason: values.reason}).then(function () {
                 notify(doneText);
                 return loadWorker();
               });
             }});
      }));
    }
    if (user.active) {
      box.appendChild(button(t("New setup code"), "secondary", function () {
        askSetupCode(user);
      }));
      if (user.blocked) {
        action(t("Unblock"), "unblock", t("Unblock {name}?", who),
               t("They can log in and use their wallet again."),
               t("{name} is unblocked.", who));
      } else {
        action(t("Block"), "block", t("Block {name}?", who),
               t("They're logged out at once and can't log in, send or order until " +
                 "unblocked. Their money stays."), t("{name} is blocked.", who), true);
      }
      action(t("Lift login lock"), "unlock", t("Lift the login lock for {name}?", who),
             t("After too many wrong passwords an account is locked for a while. This " +
               "lets them try again now."), t("The login lock for {name} is lifted.", who));
      action(t("Close account"), "deactivate", t("Close {name}'s account?", who),
             t("Only an account with nothing on it can be closed. They're logged out and " +
               "can't log in."), t("{name}'s account is closed.", who), true);
    } else {
      action(t("Reopen account"), "reactivate", t("Reopen {name}'s account?", who),
             t("They'll need a new setup code to choose a password."),
             t("{name}'s account is reopened.", who));
    }
  }

  function askSetupCode(user) {
    var isAdmin = user.role === "admin";
    ask({
      title: t("A new setup code for {name}?", {name: user.name}),
      text: t("Their password stops working and they're logged out everywhere until " +
              "they use the code.") +
        (isAdmin ? " " + t("This is an office account: give a reason.") : ""),
      reason: isAdmin,
      yes: t("Make the code"),
      act: function (values) {
        var body = values.reason ? {reason: values.reason} : {};
        return post("/api/admin/users/" + user.id + "/setup-code", body).then(function (got) {
          closeAsk();
          showCode("worker-code", null, got.setup_code, "", user.name);
          return loadWorker();
        });
      }
    });
  }

  function askReverse(entry, user) {
    var key = newKey();
    ask({
      title: t("Reverse this transaction ({kind})?", {kind: kindText(entry.kind)}),
      facts: [[t("When"), when(entry.created_at)],
              [t("Amount for {name}", {name: user.name}), signed(entry.amount), "num strong"],
              [t("Transaction"), "#" + entry.txn_id, "num"]],
      text: t("Posts its exact opposite, for every account it touched. It can't be undone."),
      reason: true, yes: t("Reverse"), danger: true, key: key,
      act: function (values) {
        return post("/api/admin/reverse", {txn_id: entry.txn_id, reason: values.reason,
                                           idempotency_key: key}).then(function () {
          notify(t("Reversed."));
          return loadWorker();
        });
      }
    });
  }

  onClick("worker-refresh", "worker-error", loadWorker);
  byId("worker-more").addEventListener("click", function () {
    worker.limit = PAGE_MORE;
    loadWorker();
  });

  onSubmit("deposit-form", "deposit-error", function () {
    var amount = amountText(byId("deposit-amount").value), into = byId("deposit-into").value;
    var user = userById(worker.id), note = byId("deposit-note").value.trim();
    if (!isLyd(amount)) {
      showError(byId("deposit-error"), t("Type the amount in LYD, like 50 or 12.5."));
      return null;
    }
    var key = newKey();
    ask({
      title: t("Deposit {amount}?", {amount: typedLyd(amount)}),
      facts: [[t("For"), user.name], [t("Amount"), typedLyd(amount), "num strong"],
              [t("Received into"), into, "num"]].concat(note ? [[t("Note"), note]] : []),
      text: t("Check you have the money before you confirm."),
      yes: t("Confirm deposit"), key: key,
      act: function () {
        var body = {user_id: worker.id, amount: amount, into: into, idempotency_key: key};
        if (note) {
          body.note = note;
        }
        return post("/api/admin/deposit", body).then(function () {
          byId("deposit-form").reset();
          notify(t("{amount} deposited for {name}.", {amount: typedLyd(amount),
                                                      name: user.name}));
          return loadWorker();
        });
      }
    });
    return null;
  });

  onSubmit("withdraw-form", "withdraw-error", function () {
    var amount = amountText(byId("withdraw-amount").value);
    var fee = amountText(byId("withdraw-fee").value), till = byId("withdraw-till").value;
    var user = userById(worker.id), note = byId("withdraw-note").value.trim();
    if (!isLyd(amount)) {
      showError(byId("withdraw-error"), t("Type the amount in LYD, like 50 or 12.5."));
      return null;
    }
    if (fee !== "" && !/^\d+(\.\d{1,3})?$/.test(fee)) {
      showError(byId("withdraw-error"),
                t("Type the fee in LYD, like 1.5, or leave it empty."));
      return null;
    }
    if (!till) {
      showError(byId("withdraw-error"), t("Add a till on the Overview first."));
      return null;
    }
    var key = newKey();
    var feeDirhams = fee === "" ? 0 : dirhamsOf(fee), gross = dirhamsOf(amount);
    ask({
      title: t("Cash withdrawal of {amount}?", {amount: typedLyd(amount)}),
      facts: [[t("For"), user.name], [t("Taken from their balance"), lyd(gross), "num strong"],
              [t("Fee"), lyd(feeDirhams), "num"],
              [t("Cash to give them"), lyd(gross - feeDirhams), "num strong"],
              [t("From till"), till, "num"]].concat(note ? [[t("Note"), note]] : []),
      yes: t("Confirm withdrawal"), key: key,
      act: function () {
        var body = {user_id: worker.id, amount: amount, from_till: till, idempotency_key: key};
        if (feeDirhams) {
          body.fee = fee;
        }
        if (note) {
          body.note = note;
        }
        return post("/api/admin/withdraw", body).then(function () {
          byId("withdraw-form").reset();
          notify(t("Withdrawal done: give {name} {amount} in cash.",
                   {name: user.name, amount: lyd(gross - feeDirhams)}));
          return loadWorker();
        });
      }
    });
    return null;
  });

  onSubmit("whatsapp-form", "whatsapp-error", function () {
    silence(byId("whatsapp-done"));
    var number = byId("whatsapp-number").value.trim();
    return post("/api/admin/users/" + worker.id + "/whatsapp", {whatsapp: number || null})
      .then(function () {
        done("whatsapp-done", number ? t("WhatsApp number saved.") :
             t("WhatsApp number removed."));
        return loadWorker();
      });
  });

  onSubmit("language-form", "language-error", function () {
    var code = byId("language-choice").value;
    silence(byId("language-done"));
    return post("/api/admin/users/" + worker.id + "/language", {language: code})
      .then(function () {
        done("language-done", t("Their screens are now in {language}.",
                                 {language: LANGUAGES[code]}));
        return loadWorker();
      });
  });

  // ---------- money: salaries, moving money, the owner's money, counting ----------

  var money = {seq: 0, expected: {}};

  function openMoney() {
    ["money-error", "payroll-error", "move-error", "capital-error", "count-error"]
      .forEach(function (line) { silence(byId(line)); });
    byId("count-result").hidden = true;
    return loadMoney(true);
  }

  // Everything the screen shows. With fresh, the salaries typed are cleared too.
  function loadMoney(fresh) {
    var seq = (money.seq += 1);
    return Promise.all([loadUsers(), get("/api/admin/health")]).then(function (got) {
      if (seq !== money.seq) {
        return;
      }
      var holdings = got[1].holdings || {};
      state.holdings = Object.keys(holdings);
      money.expected = {};
      state.holdings.forEach(function (code) {
        money.expected[code] = holdings[code].dirhams;
      });
      var all = holdingChoices(false);
      fillSelect(byId("payroll-into"), all);
      fillSelect(byId("move-from"), all);
      fillSelect(byId("move-to"), all);
      if (byId("move-to").value === byId("move-from").value && all.length > 1) {
        byId("move-to").value = all[1][0];
      }
      fillSelect(byId("capital-code"), all);
      if (fresh || !byId("payroll-rows").firstChild) {
        drawPayroll();
      }
      drawCount();
    }, function (problem) {
      if (seq === money.seq) {
        handle(problem, byId("money-error"));
      }
    });
  }

  // The workers who can be paid: active, not blocked.
  function drawPayroll() {
    var rows = byId("payroll-rows");
    clear(rows);
    var payable = state.users.filter(function (user) {
      return user.role === "customer" && user.active && !user.blocked;
    });
    payable.forEach(function (user) {
      var row = make("tr");
      row.setAttribute("data-user", String(user.id));
      row.setAttribute("data-find", (user.name + " " + user.phone).toLowerCase());
      cell(row, user.name).dir = "auto";
      cell(row, phoneText(user.phone), "num");
      var input = make("input", "amount-input");
      input.type = "text";
      input.inputMode = "decimal";
      input.autocomplete = "off";
      input.maxLength = 20;
      input.setAttribute("aria-label", t("Salary for {name}", {name: user.name}));
      cell(row, null, "end").appendChild(input);
      rows.appendChild(row);
    });
    byId("payroll-empty").hidden = payable.length > 0;
    byId("payroll-search").value = "";
    payrollTotal();
  }

  function payrollLines() {
    var lines = [], wrong = false;
    Array.prototype.forEach.call(byId("payroll-rows").children, function (row) {
      var typed = amountText(row.querySelector("input").value);
      if (typed === "") {
        return;
      }
      if (!isLyd(typed)) {
        wrong = true;
        return;
      }
      lines.push({user_id: Number(row.getAttribute("data-user")), amount: typed});
    });
    return wrong ? null : lines;
  }

  function payrollTotal() {
    var lines = payrollLines(), total = byId("payroll-total");
    if (lines === null) {
      total.textContent = t("A salary isn't an amount in LYD.");
      return;
    }
    var sum = lines.reduce(function (all, line) {
      return all + dirhamsOf(line.amount);
    }, 0);
    total.textContent = !lines.length ? "" : lines.length === 1 ?
      t("1 worker, {amount} in all.", {amount: lyd(sum)}) :
      t("{count} workers, {amount} in all.", {count: lines.length, amount: lyd(sum)});
  }

  byId("payroll-rows").addEventListener("input", payrollTotal);

  byId("payroll-search").addEventListener("input", function () {
    var search = byId("payroll-search").value.trim().toLowerCase();
    Array.prototype.forEach.call(byId("payroll-rows").children, function (row) {
      row.hidden = search !== "" && row.getAttribute("data-find").indexOf(search) < 0;
    });
  });

  onSubmit("payroll-form", "payroll-error", function () {
    var lines = payrollLines(), ref = byId("payroll-ref").value.trim();
    var into = byId("payroll-into").value, note = byId("payroll-note").value.trim();
    if (lines === null) {
      showError(byId("payroll-error"), t("Type each salary in LYD, like 1200 or 950.5."));
      return null;
    }
    if (!lines.length) {
      showError(byId("payroll-error"), t("Type at least one worker's salary."));
      return null;
    }
    if (!ref) {
      showError(byId("payroll-error"), t("Type the payment's reference."));
      return null;
    }
    var total = lines.reduce(function (all, line) {
      return all + dirhamsOf(line.amount);
    }, 0);
    var key = newKey();
    ask({
      title: lines.length === 1 ? t("Pay 1 salary?") :
        t("Pay {count} salaries?", {count: lines.length}),
      facts: [[t("Reference"), ref], [t("Received into"), into, "num"],
              [t("Workers"), String(lines.length), "num"],
              [t("In all"), lyd(total), "num strong"]].concat(note ? [[t("Note"), note]] : []),
      text: t("Each worker's balance goes up by their salary. A reference can be paid " +
              "only once."),
      yes: t("Confirm salaries"), key: key,
      act: function () {
        var body = {items: lines, batch_ref: ref, into: into, idempotency_key: key};
        if (note) {
          body.note = note;
        }
        return post("/api/admin/payroll", body).then(function () {
          byId("payroll-ref").value = byId("payroll-note").value = "";
          notify(lines.length === 1 ?
                 t("Salaries {ref} paid: {amount} to 1 worker.", {ref: ref,
                                                                  amount: lyd(total)}) :
                 t("Salaries {ref} paid: {amount} to {count} workers.",
                   {ref: ref, amount: lyd(total), count: lines.length}));
          return loadMoney(true);
        });
      }
    });
    return null;
  });

  onSubmit("move-form", "move-error", function () {
    var from = byId("move-from").value, to = byId("move-to").value;
    var amount = amountText(byId("move-amount").value), note = byId("move-note").value.trim();
    if (!from || from === to) {
      showError(byId("move-error"), t("Pick two different places."));
      return null;
    }
    if (!isLyd(amount)) {
      showError(byId("move-error"), t("Type the amount in LYD, like 500."));
      return null;
    }
    var key = newKey();
    ask({
      title: t("Move {amount}?", {amount: typedLyd(amount)}),
      facts: [[t("From"), from, "num"], [t("To"), to, "num"],
              [t("Amount"), typedLyd(amount), "num strong"]]
        .concat(note ? [[t("Note"), note]] : []),
      yes: t("Confirm move"), key: key,
      act: function () {
        var body = {from: from, to: to, amount: amount, idempotency_key: key};
        if (note) {
          body.note = note;
        }
        return post("/api/admin/move", body).then(function () {
          byId("move-amount").value = byId("move-note").value = "";
          notify(t("{amount} moved from {from} to {to}.",
                   {amount: typedLyd(amount), from: from, to: to}));
          return loadMoney(false);
        });
      }
    });
    return null;
  });

  onSubmit("capital-form", "capital-error", function () {
    var code = byId("capital-code").value, putIn = byId("capital-direction").value === "in";
    var amount = amountText(byId("capital-amount").value);
    var note = byId("capital-note").value.trim();
    if (!isLyd(amount)) {
      showError(byId("capital-error"), t("Type the amount in LYD, like 5000."));
      return null;
    }
    var key = newKey();
    ask({
      title: putIn ? t("Put {amount} into {code}?", {amount: typedLyd(amount), code: code}) :
        t("Take {amount} out of {code}?", {amount: typedLyd(amount), code: code}),
      facts: [[t("What"), putIn ? t("The owner's money put in") : t("Taken out by the owner")],
              [t("Bank or till"), code, "num"], [t("Amount"), typedLyd(amount), "num strong"]]
        .concat(note ? [[t("Note"), note]] : []),
      yes: putIn ? t("Confirm: put in") : t("Confirm: take out"), key: key,
      act: function () {
        var body = {code: code, amount: amount, direction: putIn ? "in" : "out",
                    idempotency_key: key};
        if (note) {
          body.note = note;
        }
        return post("/api/admin/capital", body).then(function () {
          byId("capital-amount").value = byId("capital-note").value = "";
          notify(putIn ? t("{amount} put into {code}.", {amount: typedLyd(amount), code: code}) :
                 t("{amount} taken out of {code}.", {amount: typedLyd(amount), code: code}));
          return loadMoney(false);
        });
      }
    });
    return null;
  });

  // One row per place, with what was typed kept when the books are read again.
  function drawCount() {
    var rows = byId("count-rows"), typed = {};
    Array.prototype.forEach.call(rows.querySelectorAll("input"), function (input) {
      typed[input.getAttribute("data-code")] = input.value;
    });
    clear(rows);
    state.holdings.forEach(function (code) {
      var row = make("tr");
      row.setAttribute("data-code", code);
      cell(row, code, "num");
      cell(row, lyd(money.expected[code]), "num end");
      var input = make("input", "amount-input");
      input.type = "text";
      input.inputMode = "decimal";
      input.autocomplete = "off";
      input.maxLength = 20;
      input.value = typed[code] || "";
      input.setAttribute("data-code", code);
      input.setAttribute("aria-label", t("Really in {code}", {code: code}));
      cell(row, null, "end").appendChild(input);
      cell(row, "", "num end difference");
      rows.appendChild(row);
    });
  }

  onSubmit("count-form", "count-error", function () {
    var counts = {}, wrong = false, result = byId("count-result");
    result.hidden = true;
    Array.prototype.forEach.call(byId("count-rows").querySelectorAll("input"), function (input) {
      var typed = amountText(input.value);
      if (typed === "") {
        return;
      }
      if (!/^\d+(\.\d{1,3})?$/.test(typed)) {
        wrong = true;
      }
      counts[input.getAttribute("data-code")] = typed;
    });
    if (wrong || !Object.keys(counts).length) {
      showError(byId("count-error"), wrong ? t("Type each amount in LYD, like 1520.5.") :
                t("Type what is really there for at least one place."));
      return null;
    }
    return post("/api/admin/reconcile", {counts: counts}).then(function (answer) {
      var bad = 0;
      answer.rows.forEach(function (r) {
        var row = byId("count-rows").querySelector('tr[data-code="' + r.account + '"]');
        if (!row) {
          return;
        }
        var cellOf = row.querySelector(".difference");
        cellOf.textContent = r.ok ? t("Matches") : signed(r.difference);
        cellOf.className = "num end difference " + (r.ok ? "in" : "out");
        row.children[1].textContent = lyd(r.expected);
        if (!r.ok) {
          bad += 1;
        }
      });
      result.textContent = !bad ? t("Everything counted matches the books.") : bad === 1 ?
        t("1 place doesn't match the books. Count again, then look for what wasn't " +
          "recorded.") :
        t("{count} places don't match the books. Count again, then look for what wasn't " +
          "recorded.", {count: bad});
      result.className = "check-line " + (bad ? "bad" : "good");
      result.hidden = false;
    });
  });

  onClick("money-refresh", "money-error", function () {
    return loadMoney(false);
  });

  // ---------- orders ----------

  var orders = {limit: PAGE, seq: 0};

  var ORDER_STATUS = {placed: k("New"), assigned: k("With a supplier"), paid: k("Paid out"),
                      cancelled: k("Cancelled")};

  function supplierName(id) {
    for (var i = 0; i < state.suppliers.length; i += 1) {
      if (state.suppliers[i].id === id) {
        return state.suppliers[i].name;
      }
    }
    return t("supplier {id}", {id: id});
  }

  function supplierChoices(currency) {
    return state.suppliers.filter(function (s) {
      return s.active;
    }).map(function (s) {
      var rate = s.prices && s.prices[currency];
      return [String(s.id), rate ? t("{name} ({rate} LYD)", {name: s.name, rate: rate}) :
              t("{name} (no {currency} rate: give a one-off rate)",
                {name: s.name, currency: currency})];
    });
  }

  function loadSuppliers() {
    return get("/api/admin/suppliers").then(function (data) {
      state.suppliers = data.suppliers;
    });
  }

  function openOrders() {
    orders.limit = PAGE;
    return loadOrders();
  }

  function loadOrders() {
    var seq = (orders.seq += 1), status = byId("orders-status").value;
    silence(byId("orders-error"));
    byId("orders-list").setAttribute("aria-busy", "true");
    return Promise.all([
      get("/api/admin/orders?limit=" + orders.limit + (status ? "&status=" + status : "")),
      loadUsers(), loadSuppliers()
    ]).then(function (got) {
      if (seq !== orders.seq) {
        return;
      }
      byId("orders-list").setAttribute("aria-busy", "false");
      drawOrders(got[0].orders);
    }, function (problem) {
      if (seq === orders.seq) {
        byId("orders-list").setAttribute("aria-busy", "false");
        handle(problem, byId("orders-error"));
      }
    });
  }

  function drawOrders(list) {
    var ul = byId("orders-list");
    clear(ul);
    list.forEach(function (order) {
      var li = make("li", "order-card");
      li.setAttribute("data-status", order.status);
      li.setAttribute("data-order", String(order.id));
      var top = make("div", "order-top");
      top.appendChild(make("span", "order-code num", order.receipt_code));
      top.appendChild(make("span", "tag tag-" + order.status,
                           has(ORDER_STATUS, order.status) ? t(ORDER_STATUS[order.status]) :
                           order.status));
      li.appendChild(top);
      li.appendChild(make("p", "strong", t("{amount} {currency} for {cost} (rate {rate})", {
        amount: order.foreign_amount, currency: order.currency, cost: lyd(order.cost),
        rate: order.rate})));
      var customer = userById(order.customer_id);
      var who = make("p", null, t("Worker:") + " ");
      var link = make("a", null, customer ? customer.name : "#" + order.customer_id);
      link.href = "#/worker/" + order.customer_id;
      link.dir = "auto";
      who.appendChild(link);
      if (order.customer_blocked) {
        who.appendChild(document.createTextNode(" "));
        who.appendChild(make("span", "tag tag-bad", t("Blocked: held")));
      }
      li.appendChild(who);
      var to = make("p", null, t("Pay to:") + " ");
      to.appendChild(make("bdi", "strong", order.beneficiary_name));
      to.appendChild(document.createTextNode(t(", ID") + " "));
      to.appendChild(make("bdi", "num", order.beneficiary_id));
      li.appendChild(to);
      if (order.supplier_id) {
        li.appendChild(make("p", null, order.supplier_cost ?
          t("Supplier: {name}, costs us {cost} (rate {rate})", {
            name: supplierName(order.supplier_id), cost: lyd(order.supplier_cost),
            rate: order.supplier_rate}) :
          t("Supplier: {name}", {name: supplierName(order.supplier_id)})));
      }
      if (order.payout_ref) {
        li.appendChild(make("p", null, t("Payout reference: {ref}", {ref: order.payout_ref})));
      }
      li.appendChild(make("p", "muted small", [
        t("Placed {time}", {time: when(order.created_at)}),
        order.paid_at ? t("paid {time}", {time: when(order.paid_at)}) : "",
        order.cancelled_at ? t("cancelled {time}", {time: when(order.cancelled_at)}) : ""
      ].filter(Boolean).join(" · ")));
      var actions = make("div", "button-row"), code = {code: order.receipt_code};
      if (order.status === "placed") {
        actions.appendChild(button(t("Give to a supplier"), "primary", function () {
          askAssign(order);
        }));
        actions.appendChild(button(t("Mark paid"), "secondary", function () {
          askPaid(order);
        }));
        actions.appendChild(button(t("Cancel and refund"), "danger-outline", function () {
          askOrderReason(order, "cancel", t("Cancel order {code}?", code),
                         t("{amount} goes back to the worker's balance.",
                           {amount: lyd(order.cost)}),
                         t("Cancel and refund"));
        }));
      } else if (order.status === "assigned") {
        actions.appendChild(button(t("Mark paid"), "primary", function () {
          askPaid(order);
        }));
        actions.appendChild(button(t("Take back from supplier"), "secondary", function () {
          askOrderReason(order, "unassign", t("Take order {code} back?", code),
                         t("It becomes new again, for another supplier or a refund."),
                         t("Take back"));
        }));
      }
      if (actions.firstChild) {
        li.appendChild(actions);
      }
      ul.appendChild(li);
    });
    byId("orders-empty").hidden = list.length > 0;
    byId("orders-more").hidden = list.length < orders.limit || orders.limit >= PAGE_MORE;
  }

  function orderFacts(order) {
    return [[t("Receipt"), order.receipt_code, "num"],
            [t("Amount"), order.foreign_amount + " " + order.currency, "num strong"],
            [t("Pay to"), t("{name}, ID {id}", {name: order.beneficiary_name,
                                                id: order.beneficiary_id})]];
  }

  function askAssign(order) {
    var choices = supplierChoices(order.currency);
    if (!choices.length) {
      notify(t("Add a supplier first, on the Suppliers screen."));
      return;
    }
    ask({
      title: t("Give order {code} to a supplier?", {code: order.receipt_code}),
      facts: orderFacts(order), supplier: choices, rate: true, yes: t("Give to supplier"),
      act: function (values) {
        var body = {supplier_id: values.supplier_id};
        if (values.rate) {
          body.rate = values.rate;
        }
        return post("/api/admin/orders/" + order.id + "/assign", body).then(function () {
          notify(t("Order {code} is with {name}.", {code: order.receipt_code,
                                                     name: supplierName(values.supplier_id)}));
          return loadOrders();
        });
      }
    });
  }

  function askPaid(order) {
    var needsSupplier = !order.supplier_id;
    var choices = needsSupplier ? supplierChoices(order.currency) : null;
    if (needsSupplier && !choices.length) {
      notify(t("Add a supplier first, on the Suppliers screen."));
      return;
    }
    ask({
      title: t("Order {code} paid out?", {code: order.receipt_code}),
      facts: orderFacts(order).concat(needsSupplier ? [] :
                                      [[t("Supplier"), supplierName(order.supplier_id)]]),
      text: t("Only when the supplier confirmed the beneficiary got the money. A paid " +
              "order is final."),
      supplier: choices, rate: needsSupplier, ref: true, yes: t("Mark paid"),
      act: function (values) {
        var body = {receipt_code: order.receipt_code, payout_ref: values.payout_ref};
        if (needsSupplier) {
          body.supplier_id = values.supplier_id;
          if (values.rate) {
            body.rate = values.rate;
          }
        }
        return post("/api/admin/orders/paid", body).then(function () {
          notify(t("Order {code} is paid.", {code: order.receipt_code}));
          return loadOrders();
        });
      }
    });
  }

  function askOrderReason(order, path, title, text, yes) {
    ask({
      title: title, facts: orderFacts(order), text: text, reason: true, yes: yes,
      danger: path === "cancel",
      act: function (values) {
        return post("/api/admin/orders/" + order.id + "/" + path, {reason: values.reason})
          .then(function () {
            notify(path === "cancel" ?
                   t("Order {code} is cancelled and refunded.", {code: order.receipt_code}) :
                   t("Order {code} is new again.", {code: order.receipt_code}));
            return loadOrders();
          });
      }
    });
  }

  byId("orders-status").addEventListener("change", openOrders);
  onClick("orders-refresh", "orders-error", loadOrders);
  byId("orders-more").addEventListener("click", function () {
    orders.limit = PAGE_MORE;
    loadOrders();
  });

  onSubmit("paid-form", "paid-error", function () {
    var code = byId("paid-code").value.trim().toUpperCase(), ref = byId("paid-ref").value;
    silence(byId("paid-done"));
    if (!code || !filled(ref)) {
      showError(byId("paid-error"), t("Type the receipt code and the payout reference."));
      return null;
    }
    return post("/api/admin/orders/paid", {receipt_code: code, payout_ref: ref.trim()})
      .then(function () {
        byId("paid-form").reset();
        done("paid-done", t("Order {code} is paid.", {code: code}));
        return loadOrders();
      });
  });

  // ---------- currencies and prices ----------

  function loadCurrencies() {
    return get("/api/admin/currencies").then(function (data) {
      state.currencies = data.currencies;
      return data.currencies;
    });
  }

  function currencyChoices() {
    return state.currencies.map(function (c) {
      return [c.code, c.code];
    });
  }

  function openPrices() {
    silence(byId("prices-error"));
    return loadCurrencies().then(drawPrices, function (problem) {
      handle(problem, byId("prices-error"));
    });
  }

  function drawPrices() {
    var rows = byId("prices-rows");
    clear(rows);
    state.currencies.forEach(function (c) {
      var row = make("tr");
      cell(row, c.code, "num strong");
      cell(row, null).appendChild(make("span", c.active ? "tag tag-good" : "tag tag-off",
                                       c.active ? (c.price ? t("Yes") : t("No price yet")) :
                                       t("No, switched off")));
      cell(row, c.price ? c.price.rate : "—", "num end");
      cell(row, c.price ? when(c.price.set_at) : "", "nowrap");
      var actions = cell(row, null, "end");
      var label = c.active ? t("Switch off") : t("Switch on");
      actions.appendChild(button(label, c.active ? "danger-outline" : "secondary", function () {
        ask({
          title: c.active ? t("Switch off {code}?", {code: c.code}) :
            t("Switch on {code}?", {code: c.code}),
          text: c.active ? t("Workers stop seeing its price and can't order it, not even " +
            "with a quote they already have. Orders already placed aren't touched.") :
            t("Workers see its price and can order it again."),
          reason: true, yes: label, danger: c.active,
          act: function (values) {
            return post("/api/admin/currencies/active", {code: c.code, active: !c.active,
                                                         reason: values.reason})
              .then(function () {
                notify(c.active ? t("{code} is switched off.", {code: c.code}) :
                       t("{code} is switched on.", {code: c.code}));
                return loadCurrencies().then(drawPrices);
              });
          }
        });
      }));
      rows.appendChild(row);
    });
    byId("prices-empty").hidden = state.currencies.length > 0;
    fillSelect(byId("price-currency"), currencyChoices());
    byId("price-submit").disabled = !state.currencies.length;
  }

  onClick("prices-refresh", "prices-error", function () {
    return loadCurrencies().then(drawPrices);
  });

  onSubmit("price-form", "price-error", function () {
    var currency = byId("price-currency").value, rate = amountText(byId("price-rate").value);
    var wrong = byId("price-void").checked;
    silence(byId("price-done"));
    if (!currency || !/^\d+(\.\d{1,6})?$/.test(rate) || !/[1-9]/.test(rate)) {
      showError(byId("price-error"), t("Type the price in LYD for 1 {currency}, like 7.85.",
                                       {currency: currency || "?"}));
      return null;
    }
    return post("/api/admin/prices", {currency: currency, rate: rate, void_quotes: wrong})
      .then(function () {
        byId("price-rate").value = "";
        byId("price-void").checked = false;
        done("price-done", t("{currency} is now {rate} LYD.", {currency: currency, rate: rate}) +
             (wrong ? " " + t("Quotes given before can't be used.") : ""));
        return loadCurrencies().then(drawPrices);
      });
  });

  onSubmit("currency-form", "currency-error", function () {
    var code = byId("currency-code").value.trim().toUpperCase();
    silence(byId("currency-done"));
    if (!/^[A-Z]{3}$/.test(code)) {
      showError(byId("currency-error"), t("Type the 3 letters of the currency, like USD."));
      return null;
    }
    return post("/api/admin/currencies", {code: code}).then(function () {
      byId("currency-code").value = "";
      done("currency-done",
           t("{code} is added. Set its price so workers can order it.", {code: code}));
      return loadCurrencies().then(function () {
        drawPrices();
        byId("price-currency").value = code;
      });
    });
  });

  // ---------- suppliers ----------

  function ratesText(prices) {
    var codes = Object.keys(prices || {});
    return codes.length ? codes.map(function (c) {
      return c + " " + prices[c];
    }).join(", ") : t("None yet");
  }

  function openSuppliers() {
    silence(byId("suppliers-error"));
    return loadSuppliers().then(drawSuppliers, function (problem) {
      handle(problem, byId("suppliers-error"));
    });
  }

  function drawSuppliers() {
    var rows = byId("suppliers-rows");
    clear(rows);
    state.suppliers.forEach(function (s) {
      var row = make("tr");
      var link = make("a", null, s.name);
      link.href = "#/supplier/" + s.id;
      link.dir = "auto";
      cell(row, null).appendChild(link);
      cell(row, s.country || "").dir = "auto";
      cell(row, null).appendChild(make("span", s.active ? "tag tag-good" : "tag tag-off",
                                       s.active ? t("In use") : t("Not in use")));
      cell(row, ratesText(s.prices), "num");
      cell(row, lyd(s.balance.dirhams), "num end");
      rows.appendChild(row);
    });
    byId("suppliers-empty").hidden = state.suppliers.length > 0;
  }

  onClick("suppliers-refresh", "suppliers-error", function () {
    return loadSuppliers().then(drawSuppliers);
  });

  onSubmit("supplier-add-form", "supplier-add-error", function () {
    var name = byId("supplier-add-name").value, country = byId("supplier-add-country").value;
    silence(byId("supplier-add-done"));
    if (!filled(name)) {
      showError(byId("supplier-add-error"), t("Type the supplier's name."));
      return null;
    }
    var body = {name: name.trim()};
    if (filled(country)) {
      body.country = country.trim();
    }
    return post("/api/admin/suppliers", body).then(function () {
      byId("supplier-add-form").reset();
      done("supplier-add-done",
           t("{name} is added. Open it to set their rates.", {name: body.name}));
      return loadSuppliers().then(drawSuppliers);
    });
  });

  var supplier = {id: null, limit: PAGE, seq: 0};

  function openSupplier(id) {
    supplier.id = id;
    supplier.limit = PAGE;
    ["supplier-error", "supplier-price-error", "supplier-price-done", "pay-error"]
      .forEach(function (line) { silence(byId(line)); });
    byId("pay-form").reset();
    return loadSupplier();
  }

  function loadSupplier() {
    var id = supplier.id, seq = (supplier.seq += 1);
    return Promise.all([loadSuppliers(), loadCurrencies(), loadHoldings(),
                        get("/api/admin/suppliers/" + id + "/statement?limit=" +
                            supplier.limit)])
      .then(function (got) {
        if (seq !== supplier.seq || state.param !== id) {
          return;
        }
        var s = state.suppliers.filter(function (one) { return one.id === id; })[0];
        if (!s) {
          byId("supplier-name").textContent = t("No such supplier");
          return;
        }
        drawSupplier(s, got[3]);
      }, function (problem) {
        if (seq === supplier.seq) {
          handle(problem, byId("supplier-error"));
        }
      });
  }

  function drawSupplier(s, statement) {
    byId("supplier-name").textContent = s.name;
    byId("supplier-country").textContent = s.country || "—";
    var status = byId("supplier-status");
    clear(status);
    status.appendChild(make("span", s.active ? "tag tag-good" : "tag tag-off",
                            s.active ? t("In use") : t("Not in use")));
    var owed = statement.balance.dirhams;
    byId("supplier-balance").textContent = owed < 0 ?
      t("{amount} paid in advance", {amount: lyd(-owed)}) : lyd(owed);
    byId("supplier-rates").textContent = ratesText(s.prices);
    fillSelect(byId("supplier-price-currency"), currencyChoices());
    byId("supplier-price-submit").disabled = !state.currencies.length;
    fillSelect(byId("pay-from"), holdingChoices(false));

    var box = byId("supplier-actions");
    clear(box);
    box.appendChild(button(s.active ? t("Stop using them") : t("Use them again"),
                           s.active ? "danger-outline" : "secondary", function () {
      ask({
        title: s.active ? t("Stop using {name}?", {name: s.name}) :
          t("Use {name} again?", {name: s.name}),
        text: s.active ? t("New orders can't be given to them. Orders they have stay " +
                           "theirs.") : t("Orders can be given to them again."),
        reason: true, yes: s.active ? t("Stop using") : t("Use again"), danger: s.active,
        act: function (values) {
          return post("/api/admin/suppliers/" + s.id + "/active",
                      {active: !s.active, reason: values.reason}).then(function () {
            notify(s.active ? t("{name} is no longer used.", {name: s.name}) :
                   t("{name} is in use again.", {name: s.name}));
            return loadSupplier();
          });
        }
      });
    }));

    var rows = byId("supplier-rows");
    clear(rows);
    statement.entries.forEach(function (entry) {
      var row = make("tr");
      cell(row, when(entry.created_at), "nowrap");
      var what = cell(row, kindText(entry.kind));
      var detail = [entry.reference, entry.note].filter(filled).join(" · ");
      if (detail) {
        what.appendChild(make("span", "muted small detail", detail)).dir = "auto";
      }
      cell(row, signed(entry.amount), "num end");
      cell(row, lyd(entry.balance_after), "num end");
      rows.appendChild(row);
    });
    byId("supplier-empty").hidden = statement.entries.length > 0;
    byId("supplier-more").hidden = statement.entries.length < supplier.limit ||
      supplier.limit >= PAGE_MORE;
  }

  byId("supplier-more").addEventListener("click", function () {
    supplier.limit = PAGE_MORE;
    loadSupplier();
  });

  onSubmit("supplier-price-form", "supplier-price-error", function () {
    var currency = byId("supplier-price-currency").value;
    var rate = amountText(byId("supplier-price-rate").value);
    silence(byId("supplier-price-done"));
    if (!currency || !/^\d+(\.\d{1,6})?$/.test(rate) || !/[1-9]/.test(rate)) {
      showError(byId("supplier-price-error"),
                t("Type their rate in LYD for 1 unit, like 7.80."));
      return null;
    }
    return post("/api/admin/suppliers/" + supplier.id + "/price",
                {currency: currency, rate: rate}).then(function () {
      byId("supplier-price-rate").value = "";
      done("supplier-price-done", t("Their {currency} rate is now {rate} LYD.",
                                    {currency: currency, rate: rate}));
      return loadSupplier();
    });
  });

  onSubmit("pay-form", "pay-error", function () {
    var amount = amountText(byId("pay-amount").value), from = byId("pay-from").value;
    var reference = byId("pay-reference").value.trim(), note = byId("pay-note").value.trim();
    var s = state.suppliers.filter(function (one) { return one.id === supplier.id; })[0];
    if (!isLyd(amount)) {
      showError(byId("pay-error"), t("Type the amount in LYD, like 5000."));
      return null;
    }
    if (!reference) {
      showError(byId("pay-error"), t("Type the payment's reference."));
      return null;
    }
    var key = newKey();
    ask({
      title: t("Pay {name} {amount}?", {name: s.name, amount: typedLyd(amount)}),
      facts: [[t("To"), s.name], [t("Amount"), typedLyd(amount), "num strong"],
              [t("Paid from"), from, "num"], [t("Reference"), reference]]
        .concat(note ? [[t("Note"), note]] : []),
      yes: t("Confirm payment"), key: key,
      act: function () {
        var body = {amount: amount, from: from, reference: reference, idempotency_key: key};
        if (note) {
          body.note = note;
        }
        return post("/api/admin/suppliers/" + supplier.id + "/pay", body).then(function () {
          byId("pay-form").reset();
          notify(t("{amount} paid to {name}.", {amount: typedLyd(amount), name: s.name}));
          return loadSupplier();
        });
      }
    });
    return null;
  });

  // ---------- log ----------

  var log = {limit: PAGE, seq: 0};

  var ACTION = {
    create_user: k("Made an account"), set_whatsapp: k("Changed WhatsApp"),
    set_language: k("Changed the language"), block_user: k("Blocked"),
    unblock_user: k("Unblocked"), deactivate_user: k("Closed an account"),
    reactivate_user: k("Reopened an account"), unlock_login: k("Lifted a login lock"),
    issue_setup_code: k("Gave a setup code"), setup_code_used: k("Setup code used"),
    assign_supplier: k("Gave an order to a supplier"),
    unassign_supplier: k("Took an order back"), cancel_order: k("Cancelled an order"),
    mark_paid: k("Marked an order paid"), deposit: k("Deposit"), payroll: k("Salaries"),
    capital: k("Owner's money"), move_money: k("Moved money"),
    withdraw_cash: k("Cash withdrawal"), reverse: k("Reversed"),
    pay_supplier: k("Paid a supplier"), add_supplier: k("Added a supplier"),
    set_supplier_price: k("Set a supplier's rate"), set_supplier_active: k("Supplier on/off"),
    add_till: k("Added a till"), add_currency: k("Added a currency"),
    set_currency_active: k("Currency on/off"), set_price: k("Set a price")
  };

  function targetText(entry) {
    if (entry.target === null || entry.target === undefined) {
      return "";
    }
    var id = Number(entry.target);
    if (entry.target_kind === "user") {
      var user = userById(id);
      return user ? user.name : t("account {id}", {id: entry.target});
    }
    if (entry.target_kind === "order") {
      return t("order {id}", {id: entry.target});
    }
    if (entry.target_kind === "transaction") {
      return t("transaction #{id}", {id: entry.target});
    }
    if (entry.target_kind === "supplier") {
      return supplierName(id);
    }
    return String(entry.target);
  }

  var MONEY_DETAILS = {amount: true, gross: true, fee: true, margin: true,
                       supplier_cost: true, cost: true, total: true};
  var DETAIL_NAME = {
    user_id: k("worker"), supplier_id: k("supplier"), customer_blocked: k("worker blocked"),
    void_quotes: k("old quotes voided"), payout_ref: k("payout reference"),
    amount: k("amount"), gross: k("amount"), fee: k("fee"), margin: k("margin"),
    supplier_cost: k("supplier cost"), total: k("total"), reason: k("reason"),
    role: k("role"), phone: k("phone"), whatsapp: k("WhatsApp"), language: k("language"),
    rate: k("rate"), currency: k("currency"), name: k("name"), country: k("country"),
    into: k("into"), till: k("till"), source: k("from"), destination: k("to"),
    direction: k("direction"), batch_ref: k("reference"), reference: k("reference"),
    workers: k("workers"), reverses: k("reverses"), active: k("in use"),
    receipt: k("receipt"), code: k("code")
  };

  function detailText(details) {
    return Object.keys(details || {}).filter(function (key) {
      return details[key] !== null && details[key] !== "";
    }).map(function (key) {
      var value = details[key];
      if (MONEY_DETAILS[key] && typeof value === "number") {
        value = lyd(value);
      } else if (key === "user_id") {
        value = (userById(value) || {name: "#" + value}).name;
      } else if (key === "supplier_id") {
        value = supplierName(value);
      } else if (value === true || value === false) {
        value = value ? t("yes") : t("no");
      } else if (typeof value === "object") {
        value = JSON.stringify(value);
      }
      return (has(DETAIL_NAME, key) ? t(DETAIL_NAME[key]) : key.replace(/_/g, " ")) + ": " +
        value;
    }).join(" · ");
  }

  function openLog() {
    log.limit = PAGE;
    return loadLog();
  }

  function loadLog() {
    var seq = (log.seq += 1), kind = byId("log-kind").value;
    silence(byId("log-error"));
    return Promise.all([
      get("/api/admin/log?limit=" + log.limit + (kind ? "&kind=" + kind : "")),
      loadUsers(), loadSuppliers()
    ]).then(function (got) {
      if (seq !== log.seq) {
        return;
      }
      var rows = byId("log-rows");
      clear(rows);
      got[0].entries.forEach(function (entry) {
        var row = make("tr");
        cell(row, when(entry.at), "nowrap");
        var admin = userById(entry.admin_id);
        cell(row, admin ? admin.name : entry.admin_id === null ? "—" : "#" + entry.admin_id)
          .dir = "auto";
        var what = cell(row, has(ACTION, entry.action) ? t(ACTION[entry.action]) :
                        entry.action);
        var target = targetText(entry);
        if (target) {
          what.appendChild(make("span", "muted small detail", target)).dir = "auto";
        }
        cell(row, detailText(entry.details), "small").dir = "auto";
        rows.appendChild(row);
      });
      byId("log-empty").hidden = got[0].entries.length > 0;
      byId("log-more").hidden = got[0].entries.length < log.limit || log.limit >= PAGE_MORE;
    }, function (problem) {
      if (seq === log.seq) {
        handle(problem, byId("log-error"));
      }
    });
  }

  byId("log-kind").addEventListener("change", openLog);
  onClick("log-refresh", "log-error", loadLog);
  byId("log-more").addEventListener("click", function () {
    log.limit = PAGE_MORE;
    loadLog();
  });

  // ---------- start ----------

  function openOverview() {
    silence(byId("overview-error"));
    return loadHealth().catch(function (problem) {
      handle(problem, byId("overview-error"));
    });
  }

  var OPEN = {
    overview: openOverview,
    workers: openWorkers,
    worker: openWorker,
    money: openMoney,
    orders: openOrders,
    prices: openPrices,
    suppliers: openSuppliers,
    supplier: openSupplier,
    log: openLog,
    account: function () {
      byId("my-language").value = state.me.language;
      silence(byId("my-language-error"));
      silence(byId("password-error"));
      silence(byId("password-done"));
      silence(byId("account-error"));
      return Promise.resolve();
    }
  };

  window.addEventListener("hashchange", route);
  wireLanguageSwitch();
  start();
}());
