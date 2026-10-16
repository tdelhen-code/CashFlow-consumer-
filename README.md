# LYD wallet (Al Naseem workers)

Backend core for a web app where workers hold LYD balances, send LYD to each
other, and order foreign currency paid from their balance.

- `app/ledger.py`: balances, payroll, deposits, transfers, cash withdrawal with
  commission, bank and till accounts, reversals, admin log, hourly checks and
  reconciliation.
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
    supplier, for a time window) for the admin side.

Users (in `app/ledger.py`):
- phone numbers must be Libyan mobiles and are stored as `+2189XXXXXXXX` however
  they are typed (`find_user_by_phone`); an optional WhatsApp number can be from
  any country (`set_whatsapp`);
- `block_user` / `unblock_user` freeze a worker while a problem or suspicion is
  checked: no sending, receiving, orders or cash, but salary and deposits still
  arrive; a blocked admin loses admin rights;
- `deactivate_user` / `reactivate_user` for workers who leave: only at a zero
  balance with no unfinished orders.

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

Not built yet: suppliers paid in foreign currency instead of LYD, approvals,
the web server (with per-IP rate limits on login), screens (Arabic, English, French), hosting and backups.
