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

Needs Python 3.10+ only (standard library). Run the tests from this folder:

    python3 -m unittest

The schema assumes a fresh database: there are no migrations yet, so a database
file made by an earlier version must be recreated.

Not built yet: suppliers paid in foreign currency instead of LYD, approvals,
login, screens (Arabic, English, French), hosting and backups.
