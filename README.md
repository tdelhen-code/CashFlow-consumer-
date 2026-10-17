# LYD wallet (Al Naseem workers)

Backend core for a web app where workers hold LYD balances, send LYD to each
other, and order foreign currency paid from their balance.

- `app/ledger.py`: balances, payroll, deposits, transfers, cash withdrawal with
  commission, bank and till accounts, reversals, admin log, hourly checks and
  reconciliation:
  - an idempotency key, when given, must be non-empty text (at most 200
    characters, spaces around it ignored); a blank key is refused, never taken
    as "no key";
  - a payroll `batch_ref` can be posted only once, by any admin (again only after
    that batch was reversed); a second one is refused as a conflict;
  - adding a till that already exists is refused as a conflict.
- `app/orders.py`: prices, 10-minute quotes, orders with beneficiary name and ID,
  random receipt codes, supplier assignment, payout marking, cancellation, and
  supplier settlement:
  - each supplier has an agreed rate per currency (`set_supplier_price`), locked
    onto an order when it is assigned (or a one-off `rate=` for that order);
  - marking an order paid moves its cost out of `SYS_ORDERS`: the supplier's cost
    to the supplier's account `SUP_<id>` (what we owe them) and the rest to
    `SYS_MARGIN`;
  - `pay_supplier` records LYD paid to a supplier from the bank or a till
    (overpaying leaves a prepayment, shown as a negative balance);
  - `supplier_balance`, `supplier_statement` and `margin_report` (per currency and
    supplier, for a time window) for the admin side;
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
  checked: no sending, receiving, orders or cash, but salary and deposits still
  arrive; a blocked admin loses admin rights;
- `deactivate_user` / `reactivate_user` for workers who leave: only at a zero
  balance with no unfinished orders. A reversal that would touch a deactivated
  worker's account is refused (reactivate them first), and the checks report
  any money left on a deactivated account.

An order with a supplier can't be cancelled directly: `unassign_supplier` first
(after checking with the supplier that it wasn't paid), then `cancel_order`.

Login (`app/auth.py`) and what the web app calls (`app/api.py`):
- log in with phone (any usual form) and password; passwords stored with scrypt;
- an admin issues a one-time setup code (`issue_setup_code`, valid 72 hours); the
  worker uses it to choose their own password (`set_password_with_code`); the
  same is used for a forgotten password. The admin never knows the password;
- 5 wrong tries lock the account for 15 minutes; an unknown phone and a wrong
  password give the same message;
- sessions: a week for workers; 8 hours for admins, ending after 30 minutes
  idle. Only hashes of tokens and codes are stored. Logout, logout everywhere,
  change password (ends other sessions). Blocking or deactivating someone logs
  them out at once;
- `app/api.py` acts as the logged-in user: balance, statement, send money by
  phone, prices, quotes, orders (without supplier or margin details). Admin
  screens get the admin's id with `api.admin_id(conn, token)`.

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
cookie, or `Authorization: Bearer <token>`. Login and setup-password are limited
to 20 tries per address per 10 minutes. Errors: `{"error", "type"}` with 400
(bad input), 401 (log in), 403 (not allowed / blocked), 404, 409 (balance, cash,
quote expired or price corrected, already exists, payroll batch already posted),
415, 429 (too many tries), 500 (nothing internal shown).

`idempotency_key` (text, at most 200 characters) is required on `POST /api/send`
and on the admin money routes `/api/admin/deposit`, `/payroll`, `/withdraw`,
`/move` and `/suppliers/<id>/pay`: the screen makes one per confirmation, so a
double click or a retry posts once. It is optional on `/api/admin/reverse` (a
transaction can be reversed only once anyway). A missing or blank key is a 400.

| | |
|---|---|
| Everyone | `POST /api/login`, `/api/setup-password`, `/api/logout`, `/api/logout-everywhere`, `/api/change-password` |
| Workers | `GET /api/me`, `/api/statement`, `/api/recipient?phone=`, `/api/prices`, `/api/orders`; `POST /api/send`, `/api/quote`, `/api/orders` |
| Admin: users | `GET /api/admin/users`, `/api/admin/users/<id>/statement`; `POST /api/admin/users` (returns the setup code), `/api/admin/users/<id>/setup-code`, `.../whatsapp`, `.../block`, `.../unblock`, `.../deactivate`, `.../reactivate` |
| Admin: money | `POST /api/admin/deposit`, `/payroll`, `/withdraw`, `/move`, `/reverse`, `/tills`, `/reconcile`; `GET /api/admin/health` |
| Admin: orders | `POST /api/admin/currencies` (`{code}`), `/currencies/active` (`{code, active: true/false, reason}`), `/prices` (`{currency, rate, void_quotes?}`), `/suppliers`, `/suppliers/<id>/price`, `/suppliers/<id>/pay`, `/orders/<id>/assign`, `/orders/<id>/unassign`, `/orders/<id>/cancel`, `/orders/paid`; `GET /api/admin/suppliers`, `/suppliers/<id>/statement`, `/orders?status=`, `/margin?since=&until=` |

Not built yet: suppliers paid in foreign currency instead of LYD, approvals,
screens (Arabic, English, French), hosting and backups.
