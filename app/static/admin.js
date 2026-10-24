// The office's screens: everything app/static/admin.html does. Plain
// JavaScript with no outside code, like the workers' app.js: one helper, api,
// makes every request; every text goes in with textContent, never as HTML;
// the API is only called because someone did something (opened a screen,
// pressed a button), never on a timer.
//
// The office reads the wallet's own error text (English): it says exactly
// what was refused ("order 4 is not placed"), which is what staff need.
// Only a lost connection, a timeout and a logged-out session get texts of
// their own.
(function () {
  "use strict";

  var TIMEOUT_MS = 20000;
  var PAGE = 50;            // rows loaded at first
  var PAGE_MORE = 200;      // "Show more": the most the API gives

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
    return (negative ? "−" : "") + whole + "." + fraction + " LYD";
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

  // "12.5" as "12.500 LYD", from the text (nothing rounded).
  function typedLyd(text) {
    var parts = text.split(".");
    var fraction = (parts[1] || "") + "000";
    return lyd(Number(parts[0]) * 1000 + Number(fraction.slice(0, 3)));
  }

  function parseTime(text) {
    return typeof text === "string" ? Date.parse(text.replace(/(\.\d{3})\d+/, "$1")) : NaN;
  }

  var timeFormat;
  try {
    timeFormat = new Intl.DateTimeFormat("en-GB", {dateStyle: "medium", timeStyle: "short"});
  } catch (e) {
    timeFormat = new Intl.DateTimeFormat("en-GB");
  }

  function when(text) {
    var ms = parseTime(text);
    return isNaN(ms) ? String(text || "") : timeFormat.format(new Date(ms));
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
    deposit: "Deposit", payroll: "Salary", transfer: "Transfer between workers",
    order: "Currency order", withdrawal: "Cash withdrawal", reversal: "Reversal",
    settlement: "Paid to supplier", payout: "Order paid out", capital: "Capital",
    move: "Moved between bank and tills"
  };

  function kindText(kind) {
    return KIND[kind] || kind;
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
      return "No connection to the wallet. Check the internet and try again.";
    }
    if (problem.kind === "timeout") {
      return "The wallet didn't answer in time. Try again.";
    }
    if (problem.status >= 500 || !problem.message) {
      return "Something went wrong on the wallet's side (" + problem.status + "). Try again.";
    }
    var text = problem.message;
    return text.charAt(0).toUpperCase() + text.slice(1) + (/[.!?]$/.test(text) ? "" : ".");
  }

  // A failed call: a session that ended goes to the login screen (true);
  // anything else is shown in the line (false).
  function handle(problem, line) {
    if (problem.kind === "http" && problem.status === 401 && problem.type === "NotLoggedIn" &&
        state.me) {
      loggedOut("Your session ended. Please log in again.");
      return true;
    }
    if (problem.kind === "http" && problem.status === 403 &&
        problem.type === "PermissionDenied" && state.me) {
      loggedOut("That account isn't an office account any more.");
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
                 "orders", "prices", "suppliers", "supplier", "log", "account"];
  var NAV_OF = {worker: "workers", supplier: "suppliers"};

  function show(name) {
    SCREENS.forEach(function (screen) {
      byId("screen-" + screen).hidden = screen !== name;
    });
    document.body.setAttribute("data-screen", name);
    state.screen = name;
    var loggedIn = state.me !== null && name !== "login" && name !== "setup";
    byId("nav").hidden = !loggedIn;
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
    var who = byId("bar-who");
    who.textContent = me.name;
    who.hidden = false;
    byId("account-name").textContent = me.name;
    byId("account-phone").textContent = phoneText(me.phone);
  }

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
      showError(error, "Fill in every field first.");
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
        showError(error, errorText(problem) + " It may have gone through: press " +
                  question.yes + " again to make sure. It won't be done twice.");
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
      showError(byId("login-error"), "Fill in your phone number and password.");
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
          showError(byId("login-error"), "This is a worker's account. Workers use the " +
                    "wallet at the main address, not the office screens.");
        });
      }
      throw problem;
    });
  }

  onSubmit("setup-form", "setup-error", function () {
    var phone = byId("setup-phone").value, code = byId("setup-code").value;
    var password = byId("setup-password").value;
    if (!filled(phone) || !filled(code) || !password) {
      showError(byId("setup-error"), "Fill in every field.");
      return null;
    }
    if (password !== byId("setup-password2").value) {
      showError(byId("setup-error"), "The two new passwords are different.");
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
      showError(byId("password-error"), "Fill in every field.");
      return null;
    }
    if (fresh !== byId("password-new2").value) {
      showError(byId("password-error"), "The two new passwords are different.");
      return null;
    }
    return post("/api/change-password", {old_password: old, new_password: fresh})
      .then(function () {
        byId("password-form").reset();
        done("password-done", "Your password is changed. You're logged out everywhere else.");
      });
  });

  onClick("logout", "account-error", function () {
    return post("/api/logout").then(function () {
      loggedOut("You're logged out.");
    });
  });

  onClick("logout-all", "account-error", function () {
    return post("/api/logout-everywhere").then(function () {
      loggedOut("You're logged out on every device.");
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
        checks.textContent = "The books' checks found " + health.problems.length +
          " problem" + (health.problems.length === 1 ? "" : "s") + ":";
        checks.className = "check-line bad";
        health.problems.forEach(function (problem) {
          problems.appendChild(make("li", null, problem));
        });
      } else {
        checks.textContent = "The books' checks are all fine.";
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
      return "This server doesn't run the hourly checks or the backups (the test " +
        "server). The production server (app.serve) does.";
    }
    var parts = [];
    parts.push(m.last_check ? "Last hourly check " + when(m.last_check.at) + ": " +
               (m.last_check.ok ? "fine." : "found problems.") : "No hourly check yet.");
    if (!m.backups_on) {
      parts.push("Backups are off.");
    } else {
      parts.push(m.last_good_backup_at ? "Last good backup " + when(m.last_good_backup_at) +
                 "." : "No good backup yet.");
      if (m.backup_overdue) {
        parts.push("A backup is overdue: check the server's log.");
      }
    }
    return parts.join(" ");
  }

  onClick("overview-refresh", "overview-error", loadHealth);

  onSubmit("till-form", "till-error", function () {
    var name = byId("till-name").value;
    silence(byId("till-done"));
    if (!filled(name)) {
      showError(byId("till-error"), "Type the till's name.");
      return null;
    }
    return post("/api/admin/tills", {name: name}).then(function (made) {
      byId("till-name").value = "";
      done("till-done", made.code + " is added.");
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
      parts.push("Office staff");
    }
    if (!user.active) {
      parts.push("Closed");
    } else if (user.blocked) {
      parts.push("Blocked");
    } else if (!user.has_password) {
      parts.push("Waiting for a password");
    } else {
      parts.push("Active");
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
      cell(row, user.role === "admin" ? "" : user.balance_lyd + " LYD", "num end");
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
    byId(box + "-text").textContent = "Give it to " + user + " in person or by WhatsApp. " +
      "They use it once, with their phone number, to choose their password, within 3 days. " +
      "It isn't shown again; a new code replaces it.";
    if (title) {
      byId(box + "-title").textContent = title;
    }
    byId(box === "code" ? "code-box" : "worker-code-box").hidden = false;
    byId(box + "-title").focus();
  }

  onSubmit("worker-add-form", "worker-add-error", function () {
    var name = byId("worker-add-name").value, phone = byId("worker-add-phone").value;
    if (!filled(name) || !filled(phone)) {
      showError(byId("worker-add-error"), "Fill in the name and the phone number.");
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
      showCode("code", body.name + "'s account is ready", made.setup_code, "", body.name);
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
      return [code, code === "BANK" ? "Bank (BANK)" : code];
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
    ["worker-error", "deposit-error", "withdraw-error", "whatsapp-error", "whatsapp-done"]
      .forEach(function (line) { silence(byId(line)); });
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
          byId("worker-name").textContent = "No such account";
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
    byId("worker-whatsapp").textContent = user.whatsapp || "None";
    var status = byId("worker-status");
    clear(status);
    status.appendChild(make("span", statusClass(user), statusText(user)));
    if (user.blocked && user.blocked_reason) {
      status.appendChild(make("span", "muted small block-reason",
                              " Blocked " + when(user.blocked_at) + ": " +
                              user.blocked_reason));
    }
    byId("worker-balance").textContent = isAdmin ? "Office accounts hold no money" :
      lyd(statement.balance.dirhams);
    byId("whatsapp-number").value = user.whatsapp || "";
    byId("worker-money").hidden = isAdmin || !user.active;
    fillSelect(byId("deposit-into"), holdingChoices(false));
    var tills = holdingChoices(true);
    fillSelect(byId("withdraw-till"), tills);
    byId("withdraw-submit").disabled = !tills.length;
    if (!tills.length) {
      byId("withdraw-till").title = "Add a till on the Overview first.";
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
        actions.appendChild(button("Reverse", "danger-outline", function () {
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
    function action(label, path, title, text, danger) {
      box.appendChild(button(label, danger ? "danger-outline" : "secondary", function () {
        ask({title: title, text: text, reason: true, yes: label, danger: danger,
             act: function (values) {
               return post("/api/admin/users/" + user.id + "/" + path,
                           {reason: values.reason}).then(function () {
                 notify(title.replace(/\?$/, "") + ": done.");
                 return loadWorker();
               });
             }});
      }));
    }
    if (user.active) {
      box.appendChild(button("New setup code", "secondary", function () {
        askSetupCode(user);
      }));
      if (user.blocked) {
        action("Unblock", "unblock", "Unblock " + user.name + "?",
               "They can log in and use their wallet again.");
      } else {
        action("Block", "block", "Block " + user.name + "?",
               "They're logged out at once and can't log in, send or order until " +
               "unblocked. Their money stays.", true);
      }
      action("Lift login lock", "unlock", "Lift the login lock for " + user.name + "?",
             "After too many wrong passwords an account is locked for a while. This " +
             "lets them try again now.");
      action("Close account", "deactivate", "Close " + user.name + "'s account?",
             "Only an account with nothing on it can be closed. They're logged out and " +
             "can't log in.", true);
    } else {
      action("Reopen account", "reactivate", "Reopen " + user.name + "'s account?",
             "They'll need a new setup code to choose a password.");
    }
  }

  function askSetupCode(user) {
    var isAdmin = user.role === "admin";
    ask({
      title: "A new setup code for " + user.name + "?",
      text: "Their password stops working and they're logged out everywhere until " +
        "they use the code." + (isAdmin ? " This is an office account: give a reason." : ""),
      reason: isAdmin,
      yes: "Make the code",
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
      title: "Reverse this " + kindText(entry.kind).toLowerCase() + "?",
      facts: [["When", when(entry.created_at)], ["Amount for " + user.name,
              signed(entry.amount), "num strong"], ["Transaction", "#" + entry.txn_id, "num"]],
      text: "Posts its exact opposite, for every account it touched. It can't be undone.",
      reason: true, yes: "Reverse", danger: true, key: key,
      act: function (values) {
        return post("/api/admin/reverse", {txn_id: entry.txn_id, reason: values.reason,
                                           idempotency_key: key}).then(function () {
          notify("Reversed.");
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
      showError(byId("deposit-error"), "Type the amount in LYD, like 50 or 12.5.");
      return null;
    }
    var key = newKey();
    ask({
      title: "Deposit " + typedLyd(amount) + "?",
      facts: [["For", user.name], ["Amount", typedLyd(amount), "num strong"],
              ["Received into", into, "num"]].concat(note ? [["Note", note]] : []),
      text: "Check you have the money before you confirm.",
      yes: "Confirm deposit", key: key,
      act: function () {
        var body = {user_id: worker.id, amount: amount, into: into, idempotency_key: key};
        if (note) {
          body.note = note;
        }
        return post("/api/admin/deposit", body).then(function () {
          byId("deposit-form").reset();
          notify(typedLyd(amount) + " deposited for " + user.name + ".");
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
      showError(byId("withdraw-error"), "Type the amount in LYD, like 50 or 12.5.");
      return null;
    }
    if (fee !== "" && !/^\d+(\.\d{1,3})?$/.test(fee)) {
      showError(byId("withdraw-error"), "Type the fee in LYD, like 1.5, or leave it empty.");
      return null;
    }
    if (!till) {
      showError(byId("withdraw-error"), "Add a till on the Overview first.");
      return null;
    }
    var key = newKey();
    var feeDirhams = fee === "" || !/[1-9]/.test(fee) ? 0 :
      Number(fee.split(".")[0]) * 1000 + Number(((fee.split(".")[1] || "") + "000").slice(0, 3));
    var gross = Number(amount.split(".")[0]) * 1000 +
      Number(((amount.split(".")[1] || "") + "000").slice(0, 3));
    ask({
      title: "Cash withdrawal of " + typedLyd(amount) + "?",
      facts: [["For", user.name], ["Taken from their balance", lyd(gross), "num strong"],
              ["Fee", lyd(feeDirhams), "num"],
              ["Cash to give them", lyd(gross - feeDirhams), "num strong"],
              ["From till", till, "num"]].concat(note ? [["Note", note]] : []),
      yes: "Confirm withdrawal", key: key,
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
          notify("Withdrawal done: give " + user.name + " " + lyd(gross - feeDirhams) +
                 " in cash.");
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
        done("whatsapp-done", number ? "WhatsApp number saved." : "WhatsApp number removed.");
        return loadWorker();
      });
  });

  // ---------- orders ----------

  var orders = {limit: PAGE, seq: 0};

  var ORDER_STATUS = {placed: "New", assigned: "With a supplier", paid: "Paid out",
                      cancelled: "Cancelled"};

  function supplierName(id) {
    for (var i = 0; i < state.suppliers.length; i += 1) {
      if (state.suppliers[i].id === id) {
        return state.suppliers[i].name;
      }
    }
    return "supplier " + id;
  }

  function supplierChoices(currency) {
    return state.suppliers.filter(function (s) {
      return s.active;
    }).map(function (s) {
      var rate = s.prices && s.prices[currency];
      return [String(s.id), s.name + (rate ? " (" + rate + " LYD)" : " (no " + currency +
              " rate: give a one-off rate)")];
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
      top.appendChild(make("span", "tag tag-" + order.status, ORDER_STATUS[order.status] ||
                           order.status));
      li.appendChild(top);
      li.appendChild(make("p", "strong num", order.foreign_amount + " " + order.currency +
                          " for " + lyd(order.cost) + " (rate " + order.rate + ")"));
      var customer = userById(order.customer_id);
      var who = make("p", null, "Worker: ");
      var link = make("a", null, customer ? customer.name : "#" + order.customer_id);
      link.href = "#/worker/" + order.customer_id;
      link.dir = "auto";
      who.appendChild(link);
      if (order.customer_blocked) {
        who.appendChild(make("span", "tag tag-bad", " Blocked: held"));
      }
      li.appendChild(who);
      var to = make("p", null, "Pay to: ");
      to.appendChild(make("bdi", "strong", order.beneficiary_name));
      to.appendChild(document.createTextNode(", ID "));
      to.appendChild(make("bdi", "num", order.beneficiary_id));
      li.appendChild(to);
      if (order.supplier_id) {
        li.appendChild(make("p", null, "Supplier: " + supplierName(order.supplier_id) +
                            (order.supplier_cost ? ", costs us " + lyd(order.supplier_cost) +
                             " (rate " + order.supplier_rate + ")" : "")));
      }
      if (order.payout_ref) {
        li.appendChild(make("p", "num", "Payout reference: " + order.payout_ref));
      }
      li.appendChild(make("p", "muted small", "Placed " + when(order.created_at) +
                          (order.paid_at ? " · paid " + when(order.paid_at) : "") +
                          (order.cancelled_at ? " · cancelled " + when(order.cancelled_at) :
                           "")));
      var actions = make("div", "button-row");
      if (order.status === "placed") {
        actions.appendChild(button("Give to a supplier", "primary", function () {
          askAssign(order);
        }));
        actions.appendChild(button("Mark paid", "secondary", function () {
          askPaid(order);
        }));
        actions.appendChild(button("Cancel and refund", "danger-outline", function () {
          askOrderReason(order, "cancel", "Cancel order " + order.receipt_code + "?",
                         lyd(order.cost) + " goes back to the worker's balance.",
                         "Cancel and refund");
        }));
      } else if (order.status === "assigned") {
        actions.appendChild(button("Mark paid", "primary", function () {
          askPaid(order);
        }));
        actions.appendChild(button("Take back from supplier", "secondary", function () {
          askOrderReason(order, "unassign", "Take order " + order.receipt_code + " back?",
                         "It becomes new again, for another supplier or a refund.",
                         "Take back");
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
    return [["Receipt", order.receipt_code, "num"],
            ["Amount", order.foreign_amount + " " + order.currency, "num strong"],
            ["Pay to", order.beneficiary_name + ", ID " + order.beneficiary_id]];
  }

  function askAssign(order) {
    var choices = supplierChoices(order.currency);
    if (!choices.length) {
      notify("Add a supplier first, on the Suppliers screen.");
      return;
    }
    ask({
      title: "Give order " + order.receipt_code + " to a supplier?",
      facts: orderFacts(order), supplier: choices, rate: true, yes: "Give to supplier",
      act: function (values) {
        var body = {supplier_id: values.supplier_id};
        if (values.rate) {
          body.rate = values.rate;
        }
        return post("/api/admin/orders/" + order.id + "/assign", body).then(function () {
          notify("Order " + order.receipt_code + " is with " +
                 supplierName(values.supplier_id) + ".");
          return loadOrders();
        });
      }
    });
  }

  function askPaid(order) {
    var needsSupplier = !order.supplier_id;
    var choices = needsSupplier ? supplierChoices(order.currency) : null;
    if (needsSupplier && !choices.length) {
      notify("Add a supplier first, on the Suppliers screen.");
      return;
    }
    ask({
      title: "Order " + order.receipt_code + " paid out?",
      facts: orderFacts(order).concat(needsSupplier ? [] :
                                      [["Supplier", supplierName(order.supplier_id)]]),
      text: "Only when the supplier confirmed the beneficiary got the money. A paid " +
        "order is final.",
      supplier: choices, rate: needsSupplier, ref: true, yes: "Mark paid",
      act: function (values) {
        var body = {receipt_code: order.receipt_code, payout_ref: values.payout_ref};
        if (needsSupplier) {
          body.supplier_id = values.supplier_id;
          if (values.rate) {
            body.rate = values.rate;
          }
        }
        return post("/api/admin/orders/paid", body).then(function () {
          notify("Order " + order.receipt_code + " is paid.");
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
            notify(path === "cancel" ? "Order " + order.receipt_code + " is cancelled and " +
                   "refunded." : "Order " + order.receipt_code + " is new again.");
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
      showError(byId("paid-error"), "Type the receipt code and the payout reference.");
      return null;
    }
    return post("/api/admin/orders/paid", {receipt_code: code, payout_ref: ref.trim()})
      .then(function () {
        byId("paid-form").reset();
        done("paid-done", "Order " + code + " is paid.");
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
                                       c.active ? (c.price ? "Yes" : "No price yet") :
                                       "No, switched off"));
      cell(row, c.price ? c.price.rate : "—", "num end");
      cell(row, c.price ? when(c.price.set_at) : "", "nowrap");
      var actions = cell(row, null, "end");
      actions.appendChild(button(c.active ? "Switch off" : "Switch on",
                                 c.active ? "danger-outline" : "secondary", function () {
        ask({
          title: (c.active ? "Switch off " : "Switch on ") + c.code + "?",
          text: c.active ? "Workers stop seeing its price and can't order it, not even " +
            "with a quote they already have. Orders already placed aren't touched." :
            "Workers see its price and can order it again.",
          reason: true, yes: c.active ? "Switch off" : "Switch on", danger: c.active,
          act: function (values) {
            return post("/api/admin/currencies/active", {code: c.code, active: !c.active,
                                                         reason: values.reason})
              .then(function () {
                notify(c.code + (c.active ? " is switched off." : " is switched on."));
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
      showError(byId("price-error"), "Type the price in LYD for 1 " + (currency || "unit") +
                ", like 7.85.");
      return null;
    }
    return post("/api/admin/prices", {currency: currency, rate: rate, void_quotes: wrong})
      .then(function () {
        byId("price-rate").value = "";
        byId("price-void").checked = false;
        done("price-done", currency + " is now " + rate + " LYD." +
             (wrong ? " Quotes given before can't be used." : ""));
        return loadCurrencies().then(drawPrices);
      });
  });

  onSubmit("currency-form", "currency-error", function () {
    var code = byId("currency-code").value.trim().toUpperCase();
    silence(byId("currency-done"));
    if (!/^[A-Z]{3}$/.test(code)) {
      showError(byId("currency-error"), "Type the 3 letters of the currency, like USD.");
      return null;
    }
    return post("/api/admin/currencies", {code: code}).then(function () {
      byId("currency-code").value = "";
      done("currency-done", code + " is added. Set its price so workers can order it.");
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
    }).join(", ") : "None yet";
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
                                       s.active ? "In use" : "Not in use"));
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
      showError(byId("supplier-add-error"), "Type the supplier's name.");
      return null;
    }
    var body = {name: name.trim()};
    if (filled(country)) {
      body.country = country.trim();
    }
    return post("/api/admin/suppliers", body).then(function () {
      byId("supplier-add-form").reset();
      done("supplier-add-done", body.name + " is added. Open it to set their rates.");
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
          byId("supplier-name").textContent = "No such supplier";
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
                            s.active ? "In use" : "Not in use"));
    var owed = statement.balance.dirhams;
    byId("supplier-balance").textContent = owed < 0 ? lyd(-owed) + " paid in advance" :
      lyd(owed);
    byId("supplier-rates").textContent = ratesText(s.prices);
    fillSelect(byId("supplier-price-currency"), currencyChoices());
    byId("supplier-price-submit").disabled = !state.currencies.length;
    fillSelect(byId("pay-from"), holdingChoices(false));

    var box = byId("supplier-actions");
    clear(box);
    box.appendChild(button(s.active ? "Stop using them" : "Use them again",
                           s.active ? "danger-outline" : "secondary", function () {
      ask({
        title: (s.active ? "Stop using " : "Use ") + s.name + (s.active ? "?" : " again?"),
        text: s.active ? "New orders can't be given to them. Orders they have stay theirs." :
          "Orders can be given to them again.",
        reason: true, yes: s.active ? "Stop using" : "Use again", danger: s.active,
        act: function (values) {
          return post("/api/admin/suppliers/" + s.id + "/active",
                      {active: !s.active, reason: values.reason}).then(function () {
            notify(s.name + (s.active ? " is no longer used." : " is in use again."));
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
      showError(byId("supplier-price-error"), "Type their rate in LYD for 1 unit, like 7.80.");
      return null;
    }
    return post("/api/admin/suppliers/" + supplier.id + "/price",
                {currency: currency, rate: rate}).then(function () {
      byId("supplier-price-rate").value = "";
      done("supplier-price-done", "Their " + currency + " rate is now " + rate + " LYD.");
      return loadSupplier();
    });
  });

  onSubmit("pay-form", "pay-error", function () {
    var amount = amountText(byId("pay-amount").value), from = byId("pay-from").value;
    var reference = byId("pay-reference").value.trim(), note = byId("pay-note").value.trim();
    var s = state.suppliers.filter(function (one) { return one.id === supplier.id; })[0];
    if (!isLyd(amount)) {
      showError(byId("pay-error"), "Type the amount in LYD, like 5000.");
      return null;
    }
    if (!reference) {
      showError(byId("pay-error"), "Type the payment's reference.");
      return null;
    }
    var key = newKey();
    ask({
      title: "Pay " + s.name + " " + typedLyd(amount) + "?",
      facts: [["To", s.name], ["Amount", typedLyd(amount), "num strong"],
              ["Paid from", from, "num"], ["Reference", reference]]
        .concat(note ? [["Note", note]] : []),
      yes: "Confirm payment", key: key,
      act: function () {
        var body = {amount: amount, from: from, reference: reference, idempotency_key: key};
        if (note) {
          body.note = note;
        }
        return post("/api/admin/suppliers/" + supplier.id + "/pay", body).then(function () {
          byId("pay-form").reset();
          notify(typedLyd(amount) + " paid to " + s.name + ".");
          return loadSupplier();
        });
      }
    });
    return null;
  });

  // ---------- log ----------

  var log = {limit: PAGE, seq: 0};

  var ACTION = {
    create_user: "Made an account", set_whatsapp: "Changed WhatsApp",
    block_user: "Blocked", unblock_user: "Unblocked", deactivate_user: "Closed an account",
    reactivate_user: "Reopened an account", unlock_login: "Lifted a login lock",
    issue_setup_code: "Gave a setup code", setup_code_used: "Setup code used",
    assign_supplier: "Gave an order to a supplier", unassign_supplier: "Took an order back",
    cancel_order: "Cancelled an order", mark_paid: "Marked an order paid",
    deposit: "Deposit", payroll: "Salaries", capital: "Capital", move_money: "Moved money",
    withdraw_cash: "Cash withdrawal", reverse: "Reversed", pay_supplier: "Paid a supplier",
    add_supplier: "Added a supplier", set_supplier_price: "Set a supplier's rate",
    set_supplier_active: "Supplier on/off", add_till: "Added a till",
    add_currency: "Added a currency", set_currency_active: "Currency on/off",
    set_price: "Set a price"
  };

  function targetText(entry) {
    if (entry.target === null || entry.target === undefined) {
      return "";
    }
    var id = Number(entry.target);
    if (entry.target_kind === "user") {
      var user = userById(id);
      return user ? user.name : "account " + entry.target;
    }
    if (entry.target_kind === "order") {
      return "order " + entry.target;
    }
    if (entry.target_kind === "transaction") {
      return "transaction #" + entry.target;
    }
    if (entry.target_kind === "supplier") {
      return supplierName(id);
    }
    return String(entry.target);
  }

  var MONEY_DETAILS = {amount: true, gross: true, fee: true, margin: true,
                       supplier_cost: true, cost: true};
  var DETAIL_NAME = {user_id: "worker", supplier_id: "supplier",
                     customer_blocked: "worker blocked", void_quotes: "old quotes voided",
                     payout_ref: "payout reference"};

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
        value = value ? "yes" : "no";
      } else if (typeof value === "object") {
        value = JSON.stringify(value);
      }
      return (DETAIL_NAME[key] || key.replace(/_/g, " ")) + ": " + value;
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
        var what = cell(row, ACTION[entry.action] || entry.action);
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
    orders: openOrders,
    prices: openPrices,
    suppliers: openSuppliers,
    supplier: openSupplier,
    log: openLog,
    account: function () {
      silence(byId("password-error"));
      silence(byId("password-done"));
      silence(byId("account-error"));
      return Promise.resolve();
    }
  };

  window.addEventListener("hashchange", route);
  start();
}());
