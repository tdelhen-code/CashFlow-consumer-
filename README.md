# LYD wallet (Al Naseem workers)

Backend core for a web app where workers hold LYD balances, send LYD to each
other, and order foreign currency paid from their balance.

- `app/ledger.py`: balances, payroll, deposits, transfers, cash withdrawal with
  commission, bank and till accounts, reversals, admin log, hourly checks and
  reconciliation:
  - the checks (`verify_ledger`, and `orders.verify` which adds the order
    checks) read one snapshot of the database, so they can run while the app is
    in use without reporting differences that aren't there;
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
    refused as a conflict if the order is with another supplier by then;
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
    can still be set while it is off.

Users (in `app/ledger.py`):
- phone numbers must be Libyan mobiles and are stored as `+2189XXXXXXXX` however
  they are typed (`find_user_by_phone`); an optional WhatsApp number can be from
  any country (`set_whatsapp`);
- `block_user` / `unblock_user` freeze a worker while a problem or suspicion is
  checked: no sending, receiving, quotes, orders or cash, but salary and deposits
  still arrive; a blocked admin loses admin rights. Their open orders are held:
  no supplier can be assigned and no payout recorded until they are unblocked
  (the admin's order list shows `customer_blocked`). An admin can still unassign
  (with a reason) and cancel, which refunds to the frozen balance;
- `deactivate_user` / `reactivate_user` for workers who leave: only at a zero
  balance with no unfinished orders. A reversal that would touch a deactivated
  worker's account is refused (reactivate them first), and the checks report
  any money left on a deactivated account.

An order with a supplier can't be cancelled or given to another supplier
directly: `unassign_supplier` first, with a reason (after checking with the
supplier that it wasn't paid), then `cancel_order` or `assign_supplier` again.
Cancel and unassign reasons are for admins (admin log, admin statements): the
worker's statement shows a reversal only as "Order cancelled" or, for an
`/api/admin/reverse`, "Correction by admin".

Login (`app/auth.py`) and what the web app calls (`app/api.py`):
- log in with phone (any usual form) and password. A password has at least 10
  characters, is not a much-used one (`1234567890`, `qwertyuiop`, `password123`,
  ...) and doesn't contain the user's phone number. Passwords are stored with
  scrypt (N=2^15, r=8, p=1); the cost is kept with each hash, and a hash made
  with an older cost is redone at the next login. At most 4 hashes run at once,
  so many logins at the same time can't use up the memory;
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
  admin), and a code for an admin's account needs a reason. A worker whose code
  was already used, or who is logged out unexpectedly, must report it. Later:
  a second admin approves resetting an admin's account;
- 5 wrong tries in a row (password, setup code, or the current password when
  changing it) lock that phone number for 15 minutes, registered or not. An
  unknown phone, a wrong password and a locked account get the same answer and
  the same slow check, so login can't be used to find out who has an account. A
  right setup code still works while locked and lifts the lock; an admin can
  lift it too (`unlock_login`, with a reason, logged);
- sessions: a week for workers; 8 hours for admins, ending after 30 minutes
  idle. Activity is written at most once a minute, so reading pages doesn't wait
  for other writes; the idle limit can end a session up to a minute early,
  never late. Only hashes of tokens are stored. Logout, logout everywhere,
  change password (ends other sessions; refused if meanwhile the password was
  reset or changed, or the user was blocked or logged out). Blocking or
  deactivating someone logs them out at once;
- `app/api.py` acts as the logged-in user: balance, statement (without admins'
  reversal reasons), send money by phone, prices, quotes, orders (without
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

In production, serve `make_app("/path/wallet.db", trust_proxy=True)` with a WSGI
server (waitress, gunicorn) behind HTTPS. JSON in and out; POSTs must be
`Content-Type: application/json`. The session is an HttpOnly, SameSite=Strict
cookie, or `Authorization: Bearer <token>`. Login, setup-password and
change-password are limited to 20 tries per address per 10 minutes. Errors:
`{"error", "type"}` with 400 (bad input), 401 (log in; also a wrong or unknown
login, or a locked one), 403 (not allowed / blocked, including a blocked
worker's order; `WrongPassword` for a wrong current password on
change-password, which doesn't log out), 404, 409 (balance, cash, quote expired
or price corrected, already exists, payroll batch already posted, quote already
used for another beneficiary, order already with a supplier or with another one
than given, password changed meanwhile), 415, 429 (too many tries from an
address; `AccountLocked` when a logged-in user's current-password tries are
locked), 500 (nothing internal shown).

`idempotency_key` (text, at most 200 characters) is required on `POST /api/send`
and on the admin money routes `/api/admin/deposit`, `/payroll`, `/withdraw`,
`/move` and `/suppliers/<id>/pay`: the screen makes one per confirmation, so a
double click or a retry posts once. It is optional on `/api/admin/reverse` (a
transaction can be reversed only once anyway). A missing or blank key is a 400.

| | |
|---|---|
| Everyone | `POST /api/login`, `/api/setup-password`, `/api/logout`, `/api/logout-everywhere`, `/api/change-password` |
| Workers | `GET /api/me`, `/api/statement`, `/api/recipient?phone=`, `/api/prices`, `/api/orders`; `POST /api/send`, `/api/quote`, `/api/orders` |
| Admin: users | `GET /api/admin/users`, `/api/admin/users/<id>/statement`; `POST /api/admin/users` (returns the setup code; `reason` optional, "new account" by default), `/api/admin/users/<id>/setup-code` (`{reason}`, required for an admin's account), `.../whatsapp`, `.../block`, `.../unblock`, `.../deactivate`, `.../reactivate`, `.../unlock` (`{reason}`: lifts a lock from wrong tries) |
| Admin: money | `POST /api/admin/deposit`, `/payroll`, `/withdraw`, `/move`, `/reverse`, `/tills`, `/reconcile`; `GET /api/admin/health` |
| Admin: orders | `POST /api/admin/currencies` (`{code}`), `/currencies/active` (`{code, active: true/false, reason}`), `/prices` (`{currency, rate, void_quotes?}`), `/suppliers`, `/suppliers/<id>/price`, `/suppliers/<id>/pay`, `/orders/<id>/assign` (`{supplier_id, rate?}`, only a placed order), `/orders/<id>/unassign` (`{reason}`), `/orders/<id>/cancel` (`{reason}`), `/orders/paid` (`{receipt_code, payout_ref, supplier_id?}`); `GET /api/admin/suppliers`, `/suppliers/<id>/statement`, `/orders?status=` (with `customer_blocked`), `/margin?since=2026-10-01&until=2026-11-01` (dates, or times like `2026-10-01T00:00:00Z` or with an offset; no zone = UTC) |

Not built yet: suppliers paid in foreign currency instead of LYD, approvals
(such as a second admin approving a reset of an admin's account), screens
(Arabic, English, French), hosting and backups.
