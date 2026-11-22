# Seller Ledger (local role-based demo)

A private Flask dashboard prototype with two server-enforced roles. The current phase uses deterministic **synthetic demo data only**. It makes no eBay API calls, requires no eBay keys, and keeps demo stores/orders/costs in separate `demo_*` tables from the existing future live-integration tables. Live eBay OAuth and sync endpoints intentionally return `501` during this phase.

## Requirements

- Python 3.10 or newer
- No paid service, external database, or internet connection needed after dependencies are installed

## Run locally

```sh
cd /path/to/Seller-Ledger
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Before first use, replace the placeholder values in `.env` with a unique `APP_SECRET` and separate `DEMO_MANAGER_PASSWORD` / `DEMO_CLIENT_PASSWORD` values (both passwords must be at least 12 characters). Pick usernames with the `DEMO_*_USERNAME` settings if desired; the app uses the configured values verbatim. The app does not load `.env` automatically; export its values before starting:

```sh
set -a
source .env
set +a
python app.py
```

Open <http://127.0.0.1:5000>. To run on another port, set `PORT=5002` before `python app.py`. For a clean reset of only local demo and app data, stop the server and remove the configured SQLite database file; the next start recreates the schema and synthetic sample stores/orders. Do not remove a database that contains data you need.

### Demo accounts

The credentials are the values you set in `.env`:

- Manager: `DEMO_MANAGER_USERNAME` (default `manager@demo.local`) and `DEMO_MANAGER_PASSWORD`
- Client: `DEMO_CLIENT_USERNAME` (default `client@demo.local`) and `DEMO_CLIENT_PASSWORD`

The manager is assigned four sample stores. The client is assigned exactly one sample store (`North Star Goods`). Login is for this app and is separate from any eBay login. Passwords are stored as Werkzeug scrypt hashes. Sessions are HTTP-only, SameSite=Lax, CSRF-protected, and expire after 12 hours. Set `COOKIE_SECURE=1` when using HTTPS.

## Roles and data boundaries

- The manager sees all four assigned demo stores, can compare them, rename a demo store, set each store’s profit share with an effective date and configurable loss sharing, set dropshipping or low-stock inventory, configure a low-stock threshold, add order costs, and export assigned data. Synthetic stock rules are configurable, but no stock counts or alerts are provided until inventory data is connected.
- The client sees only their assigned store. There is no client store selector or manager form. `/api/state` derives permitted stores from the authenticated user record. Exports reject a requested unassigned `store_id`/legacy `account_id`; manager actions also check assignments server-side.
- No browser-supplied role is accepted. Every write requires a session CSRF token.
- Synthetic data is labelled throughout and never enters the existing live `accounts`, `orders`, or `ledger` tables.

## Demo calculations and limitations

The sample ledger is GBP. Estimated profit for an order is proceeds minus refunds, fees, and known product, fulfilment, and other costs. A cost row tied to an order is applied only to that order; future SKU defaults are distinct rows without an order ID. If product cost or a required financial value is missing, profit and share are shown as unknown and excluded from totals rather than treating the missing value as zero. The sample includes missing product costs for this purpose. Marketplace tax is a separate field and is not counted as seller proceeds or treated as a tax-liability calculation. Payouts are never revenue. Store shares in the synthetic seed data are illustrative only.

CSV exports include sales, marketplace tax, refunds, fees, known costs, estimated profit, effective share percentage, estimated share and missing-cost flags. This demo does not yet provide eBay OAuth, API sync, role assignment UI, cost CSV imports, SKU defaults, inventory/traffic metrics, or a production secret-management/authentication deployment. Live API coverage and credentials must be verified separately before live integration is enabled. Do not expose this development server publicly.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile app.py
node --check static/app.js
```

Tests cover decimal calculations, refund/fee/cost handling, missing-cost behavior, loss-sharing, account namespace/idempotency, SKU cost isolation, manager stock-rule upserts, authentication, client store isolation, and export ID tampering.
