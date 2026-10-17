# LYD wallet (Al Naseem workers)

Backend core for a web app where workers hold LYD balances, send LYD to each
other, and order foreign currency paid from their balance.

- `app/ledger.py`: balances, payroll, deposits, transfers, cash withdrawal with
  commission, bank and till accounts, reversals, admin log, hourly checks and
  reconciliation:
  - the checks (`verify_ledger`, and `orders.verify` which adds the order
    checks) and `reconcile` read one snapshot of the database, so they can run
    while the app is in use without reporting differences that aren't there
    (such as a move between the bank and a till counted in neither or both);
  - `admin_log` reads the admin log back, newest first, optionally only one
    action, one kind of target (`user`, `order`, `transaction`, `supplier`,
    or `code` for a till or currency) and one target (`GET /api/admin/log`).
    User 2, order 2 and supplier 2 are all target 2, so give the kind with a
    target; each row says its `target_kind`;
  - an idempotency key, when given, must be non-empty text (at most 200
    characters, spaces around it ignored); a blank key is refused, never taken
    as "no key";
  - a payroll `batch_ref` can be posted only once, by any admin (again only after
    that batch was reversed); a second one is refused as a conflict;
  - adding a till that already exists is refused as a conflict.
- `app/orders.py`: prices, 10-minute quotes, orders with beneficiary name and ID,
  random receipt codes, supplier assignment, payout marking, cancellation, and
  supplier settlement:
  - foreign amounts and rates are stored as plain decimal text, the same for
    the same value however it was typed (`1E+1` is `10.00`, `7.850` is `7.85`);
  - placing the same quote again returns the first order (a double tap charges
    once); the same quote with a different beneficiary name or ID is refused as
    a conflict (get a new quote);
  - each supplier has an agreed rate per currency (`set_supplier_price`), locked
    onto an order when it is assigned (or a one-off `rate=` for that order);
  - `mark_paid` can be given the `supplier_id` whose confirmation it is: it is
    refused as a conflict if the order is with another supplier by then. An
    order without a supplier (unassigned on the supplier's word that it had not
    paid, then it sends proof that it did) is marked paid only with that
    `supplier_id`, at a one-off `rate=` if given, otherwise at the rate it
    had when it last held the order (it paid at that rate; the unassign is
    logged with it), otherwise its agreed rate; it is locked on in the same
    step;
  - `set_supplier_active` switches a supplier off or on, with a logged reason.
    While off, no order can be assigned to it; its orders already assigned can
    still be marked paid or unassigned, and it can still be paid;
  - marking an order paid moves its cost out of `SYS_ORDERS`: the supplier's cost
    to the supplier's account `SUP_<id>` (what we owe them) and the rest to
    `SYS_MARGIN`;
  - `pay_supplier` records LYD paid to a supplier from the bank or a till
    (overpaying leaves a prepayment, shown as a negative balance);
  - `supplier_balance`, `supplier_statement` and `margin_report` (per currency and
    supplier, for a time window `[since, until)`; a time without a zone is UTC,
    never the server's own zone) for the admin side;
  - currency codes are 3 letters (`usd` is stored as `USD`); adding a currency
    or a supplier name (ignoring case) that already exists is refused as a
    conflict;
  - a quote keeps its price for 10 minutes, unless the admin corrects a wrong
    price with `set_price(..., void_quotes=True)`: then no quote taken before
    that price can be used (the worker is asked to get a new one);
  - `set_currency_active` switches a currency off or on, with a logged reason.
    While off, workers don't see it and can't get quotes or place orders for it,
    even on quotes taken before; orders already placed are not touched. Its price
    and the suppliers' agreed rates for it can still be set while it is off, so
    the orders already placed are assigned at what the supplier charges now.

Users (in `app/ledger.py`):
- phone numbers must be Libyan mobiles and are stored as `+2189XXXXXXXX` however
  they are typed (`find_user_by_phone`): spaces, dashes, dots, brackets and the
  invisible direction marks of a number copied from Arabic text (the Arabic
  letter mark among them) are ignored, and Arabic-Indic or Persian digits
  (`٠٩١ ١٢٣ ٤٥٦٧`) are read as 0-9; any other
  character (digits of other scripts, `²`) is refused. A number already
  registered, however it is typed, is refused as a conflict. An optional
  WhatsApp number can be from any country (`set_whatsapp`), read the same way
  and stored as `+<country code><number>`;
- `block_user` / `unblock_user` freeze a worker while a problem or suspicion is
  checked: no sending, receiving, quotes, orders or cash, but salary and deposits
  still arrive; a blocked admin loses admin rights, and the setup codes they
  issued that nobody has used yet stop working, for good (also after an
  unblock; the admin log lists whose they were, who then need a new code from
  another admin). Their open orders are held: no supplier can be assigned
  until they are unblocked (the admin's order list shows
  `customer_blocked`). An admin can still unassign (with a reason) and
  cancel, which refunds to the frozen balance. A payout the supplier already
  made is still recorded with `mark_paid` (the worker's balance doesn't change;
  the admin log notes the block), also on an order unassigned meanwhile, with
  that `supplier_id`: never cancel an order the supplier has paid;
- `deactivate_user` / `reactivate_user` for workers who leave: only at a zero
  balance with no unfinished orders. Deactivating an admin also stops the
  setup codes they issued, like blocking. A reversal that would touch a
  deactivated worker's account is refused (reactivate them first), and the
  checks report any money left on a deactivated account.

An order with a supplier can't be cancelled or given to another supplier
directly (refused as a conflict): `unassign_supplier` first, with a reason
(after checking with the supplier that it wasn't paid), then `cancel_order` or
`assign_supplier` again. A paid or cancelled order can't be changed.
Cancel and unassign reasons are for admins (admin log, `GET /api/admin/log`,
admin statements): the worker's statement shows a reversal only as "Order
cancelled" or, for an `/api/admin/reverse`, "Correction by admin".

Login (`app/auth.py`) and what the web app calls (`app/api.py`):
- log in with phone (any usual form) and password. A password has at least 10
  characters, is not a much-used one (`1234567890`, `qwertyuiop`, `password123`,
  ...), nor a much-used word with only digits or symbols around it
  (`Password1!`, `Qwerty123!`), counting (`12345678910`) or a short piece
  repeated (`123123123123`), and doesn't contain the user's phone number.
  Passwords are stored with scrypt (N=2^15, r=8, p=3: one of OWASP's minimums,
  about 0.3 s); the cost is kept with each hash, and a hash made with an older
  cost is redone at the next login. Until then checking it is padded with the
  work it falls short of the current cost, so it takes about as long as for an
  unknown phone. At most 4 hashes run at once, so many logins at the same time
  can't use up the memory;
- an admin issues a one-time setup code (`issue_setup_code`, valid 72 hours,
  12 characters shown as `XXXX-XXXX-XXXX`, typed in any case, with or without
  the dashes); the worker uses it to choose their own password
  (`set_password_with_code`), which ends every other session of theirs; the
  same is used for a forgotten password. Codes are stored with salted scrypt,
  like passwords;
- the admin sees the code, so until the worker uses it, that admin could use
  it and act as the worker (or as another admin). This can't be ruled out while
  admins hand out codes. Instead, issuing a code and using it are both in the
  admin log (who issued it and when; its use is logged under the issuing
  admin; `GET /api/admin/log?action=issue_setup_code&kind=user&target=<user
  id>`), and a code for an admin's account needs a reason. A worker whose code was already
  used, or who is logged out unexpectedly, must report it. Blocking or
  deactivating an admin stops every code they issued that nobody has used yet,
  so a suspected or departed admin can't use one. Later: a second admin
  approves resetting an admin's account;
- 5 wrong tries in a row (password, setup code, or the current password when
  changing it) from one address (IPv6: one /64) lock that phone number for 15
  minutes from that address, registered or not; the owner can still log in
  from anywhere else, so a stranger can't lock someone out with a few tries.
  50 wrong tries within 15 minutes from all addresses together lock it for
  every address that never gave its right password, so guessing spread over
  many addresses stays slow while the owner can still log in from where they
  did before (with their password or a setup code). That is the trade-off:
  someone with many addresses can still lock a phone for every address its
  owner hasn't logged in from, and an admin then lifts it. An unknown phone, a
  wrong password and a locked account get the same answer and the same slow
  check, so login can't be used to find out who has an account. A right setup code still works while locked and lifts
  every lock; an admin can lift them too (`unlock_login`, with a reason,
  logged). `login`, `set_password_with_code` and `change_password` take the
  address as `client=`;
- sessions: a week for workers; 8 hours for admins, ending after 30 minutes
  idle. Activity is written at most once a minute, so reading pages doesn't wait
  for other writes; the idle limit can end a session up to a minute early,
  never late. Only hashes of tokens are stored. Logout, logout everywhere,
  change password (ends other sessions; refused if meanwhile the password was
  reset or changed, or the user was blocked or logged out). Blocking or
  deactivating someone logs them out at once;
- `app/api.py` acts as the logged-in user: balance, statement (without admins'
  reversal reasons), send money by phone (a blocked or closed colleague gets
  the same answer as an unknown number), prices, quotes, orders (without
  supplier or margin details). Admin screens get the admin's id with
  `api.admin_id(conn, token)`.

Set up a new database with the first admin:

    from app import auth
    conn = auth.connect("wallet.db")
    auth.create_first_admin(conn, "Name", "091 000 0000", "a strong password")

Needs Python 3.10+ only (standard library). Run the tests from this folder:

    python3 -m unittest

The schema assumes a fresh database: there are no migrations yet, so a database
file made by an earlier version must be recreated.

## Web API (`app/web.py`)

Run it locally (plain http, so the cookie is allowed without HTTPS):

    python3 -m app.web --db wallet.db --port 8000 --insecure-cookies

The bundled server handles requests on 8 threads, each keeping its database
connection from one request to the next, and lets go of a client that sends
nothing for 30 seconds or whose request hasn't fully arrived within 30
seconds. It is for local testing only: don't make it reachable by others.

In production, serve `make_app("/path/wallet.db", trust_proxy=True)` with a WSGI
server (waitress, gunicorn) behind HTTPS (nginx, Caddy) that sets
`X-Forwarded-For`; without `trust_proxy` every client looks like the proxy, so
they all share one login limit. Waitress drops that header unless it is told to
trust the proxy: `waitress.serve(application, trusted_proxy="127.0.0.1",
trusted_proxy_headers="x-forwarded-for")`, or `waitress-serve
--trusted-proxy=127.0.0.1 --trusted-proxy-headers=x-forwarded-for
wsgi:application` (with the proxy's address); gunicorn passes it on as it is.
With `trust_proxy`, a request without the header counts for the address it
came from, and the first one is reported on stderr. JSON in and out; POSTs
must be `Content-Type: application/json` (else 415) with a plain
`Content-Length` of at most 64 KiB (a negative or non-numeric one is a 400, a larger one a 413; none
means an empty body). The session is the HttpOnly, Secure, SameSite=Strict
cookie `__Host-session` (plain `session` with `--insecure-cookies`), found among
any other cookies the browser sends, or `Authorization: Bearer <token>`. A
browser takes a `__Host-` cookie only from this very host, so another subdomain
can't plant one; a request with two different session cookies counts as not
logged in.

Limits (429 when reached). They are kept in memory, per server process: run
one process (waitress, or `gunicorn -w 1 --threads 8`), or also limit at the
proxy; with several processes each one allows as much again:
- failed login, setup-password and change-password tries: 20 per client
  address per 10 minutes, an IPv6 address counting per /64. Successful tries
  don't count, so an office full of workers behind one address can log in;
- successful ones (the right password or code, also of a blocked user): 10
  per account per 10 minutes, from any address, so one account can't log in
  over and over. Wrong tries don't count here, so nobody else can use them up;
- each worker's `POST /api/send`, `/api/quote`, `/api/orders` and
  `GET /api/recipient`: 30 a minute.

Fields: text must be a JSON string (a number, list or object is a 400) of at
most 100 characters for `name`, `beneficiary_name`, `beneficiary_id`,
`country` and references (`reference`, `payout_ref`, `batch_ref`,
`receipt_code`), 500 for a `reason`, and 200 for a `note`, `idempotency_key`
or any other text. Amounts and rates are text such as `"12.5"` (a whole JSON
number is taken too, a fraction such as `12.5` is not: it may not be exactly
what was typed); ids (`user_id`, `txn_id`, `supplier_id`) are whole numbers,
and a `quote_id` is the text `/api/quote` returned. Currency codes are read in
any case (`usd` is `USD`). Text that is only spaces or invisible marks
(direction marks, the Arabic letter mark, zero-width spaces) is blank: a
required one is refused. An optional field (marked `?` below) sent as `""` or
only spaces counts as left out: no WhatsApp number, the agreed rate on assign,
no fee (but not `idempotency_key`, see below). Lists (`/api/statement`,
`/api/orders`, the admin statements, `/api/admin/orders` and
`/api/admin/log`) take `?limit=` (default 50, at most 200). `.../whatsapp` needs its field: `null` or `""`
removes the number, and a body without it is a 400 that changes nothing.

Errors: `{"error", "type"}` with 400 (bad input: a field missing, of the wrong
type or too long, or a rule not met, such as a currency that isn't offered, a
supplier switched off, an order that is already paid or cancelled, or an
idempotency key reused with other details), 401 (log in; also a wrong or unknown login, a locked one, or a
session ended by blocking, deactivating or a new setup code), 403 (not allowed /
blocked: a worker on an admin route or an admin on a worker's one, a blocked
user logging in, a blocked worker's order, an admin blocked or deactivated
while their request was under way; `WrongPassword` for a wrong current password
on change-password, which doesn't log out), 404 (no such user, worker account (also a closed worker's),
bank or till, supplier, order, quote (also another worker's), receipt code,
transaction or path; also a colleague to send to who is blocked or closed, like
an unknown number), 405 (a known path with another method, such as
`GET /api/login`), 409 (balance, cash, quote expired or price corrected,
already exists (a till, currency, supplier, or a phone number already
registered), payroll batch already posted, quote already used for another
beneficiary, order already with a supplier (to assign or cancel it, unassign
it first) or with another one than given, password changed meanwhile), 413 (a
body over 64 KiB), 415 (a POST that isn't `application/json`), 429 (see the
limits; `AccountLocked` when a logged-in user's current-password tries are
locked), 500 (nothing internal shown).

`idempotency_key` (text, at most 200 characters) is required on `POST /api/send`
and on the admin money routes `/api/admin/deposit`, `/payroll`, `/withdraw`,
`/move` and `/suppliers/<id>/pay`: the screen makes one per confirmation, so a
double click or a retry posts once. The same key again with the same details
answers with the first `txn_id` (on `/api/send` also when the colleague was
blocked or closed since); with other details it is a 400 (a 404 on `/api/send`
to a colleague who is blocked or closed by then, like any new send). Keys are kept
apart per sender, so two workers can't collide. It is optional on
`/api/admin/reverse` (a transaction can be reversed only once anyway). A
missing key (where required) or a blank one is a 400.

| | |
|---|---|
| Everyone | `POST /api/login` (`{phone, password}`), `/api/setup-password` (`{phone, code, password}`), `/api/logout` (this session), `/api/logout-everywhere` (every session of this user), `/api/change-password` (`{old_password, new_password}`; ends the other sessions) |
| Workers | `GET /api/me`, `/api/statement`, `/api/recipient?phone=`, `/api/prices`, `/api/orders`; `POST /api/send` (`{to_phone, amount, idempotency_key, note?}`), `/api/quote` (`{currency, amount}`), `/api/orders` (`{quote_id, beneficiary_name, beneficiary_id}`) |
| Admin: users | `GET /api/admin/users`, `/api/admin/users/<id>/statement`, `/api/admin/log?action=&kind=&target=` (the admin log, newest first: who issued and used setup codes, block, unassign and cancel reasons...; all filters optional; `kind` is `user`, `order`, `transaction`, `supplier` or `code`); `POST /api/admin/users` (`{name, phone, role?, language?, whatsapp?, reason?}`: `role` is `customer` (default) or `admin`, `language` `ar` (default), `en` or `fr`; returns the setup code; `reason` "new account" by default), `/api/admin/users/<id>/setup-code` (`{reason?}`, required for an admin's account), `.../whatsapp` (`{whatsapp}`, `null` or `""` removes it), `.../block`, `.../unblock`, `.../deactivate`, `.../reactivate`, `.../unlock` (each `{reason}`, required; unlock lifts a lock from wrong tries) |
| Admin: money | `POST /api/admin/deposit` (`{user_id, amount, into, idempotency_key, note?}`; `into` is `BANK` or a till such as `TILL_MAIN`), `/payroll` (`{items: [{user_id, amount}, ...], batch_ref, into?, idempotency_key, note?}`; `into` is `BANK` by default), `/withdraw` (`{user_id, amount, fee?, from_till, idempotency_key, note?}`; the worker gets `amount - fee` in cash), `/move` (`{from, to, amount, idempotency_key, note?}`, between the bank and tills), `/reverse` (`{txn_id, reason, idempotency_key?}`), `/tills` (`{name}`, becomes `TILL_<NAME>`), `/reconcile` (`{counts: {account: amount}}`, what is really in each); `GET /api/admin/health` |
| Admin: orders | `POST /api/admin/currencies` (`{code}`), `/currencies/active` (`{code, active: true/false, reason}`), `/prices` (`{currency, rate, void_quotes?}`, `void_quotes` true/false), `/suppliers` (`{name, country?}`), `/suppliers/<id>/active` (`{active: true/false, reason}`), `/suppliers/<id>/price` (`{currency, rate}`), `/suppliers/<id>/pay` (`{amount, from, reference, idempotency_key, note?}`; `from` is `BANK` or a till), `/orders/<id>/assign` (`{supplier_id, rate?}`, only a placed order), `/orders/<id>/unassign` (`{reason}`), `/orders/<id>/cancel` (`{reason}`, only an order without a supplier), `/orders/paid` (`{receipt_code, payout_ref, supplier_id?, rate?}`; an order without a supplier needs `supplier_id`, and `rate` is a one-off rate for it); `GET /api/admin/suppliers`, `/suppliers/<id>/statement`, `/orders?status=` (with `customer_blocked`), `/margin?since=2026-10-01&until=2026-11-01` (orders paid from `since` up to but not including `until`; dates, or times like `2026-10-01T00:00:00Z` or with an offset; no zone = UTC) |

Not built yet: suppliers paid in foreign currency instead of LYD, approvals
(such as a second admin approving a reset of an admin's account), screens
(Arabic, English, French), hosting and backups.
