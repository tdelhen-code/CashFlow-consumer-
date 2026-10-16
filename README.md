# LYD wallet (Al Naseem workers)

Backend core for a web app where workers hold LYD balances, send LYD to each
other, and order foreign currency paid from their balance.

- `app/ledger.py`: balances, payroll, deposits, transfers, cash withdrawal with
  commission, bank and till accounts, reversals, admin log, hourly checks and
  reconciliation.
- `app/orders.py`: prices, 10-minute quotes, orders with beneficiary name and ID,
  random receipt codes, supplier assignment, payout marking, cancellation.

Needs Python 3.10+ only (standard library). Run the tests from this folder:

    python3 -m unittest

Not built yet: supplier settlement and margin, approvals, login, screens
(Arabic, English, French), hosting and backups.
