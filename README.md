# Seller Ledger

A private, local-first dashboard for four eBay seller accounts. It uses Flask, SQLite, Python `Decimal`, a responsive vanilla JavaScript interface, and the official eBay Sell APIs. There are no paid services or third-party analytics services. The app binds to `127.0.0.1` by default and does not configure public deployment. Per the owner's request, the password screen is removed; the local loopback listener is the access boundary.

## What works

- Direct dashboard access on localhost, four separately namespaced accounts, account comparison, date/account filters, trends and CSV exports.
- Synthetic demo mode, isolated in browser memory and never inserted into the live database.
- Effective-dated profit-share changes, configurable loss-sharing, account name/marketplace edits.
- Manual order/SKU costs and CSV cost imports; raw order response is retained for audit.
- Per-store, per-SKU low-stock thresholds (default 3 units) with visible warnings; threshold edits are observational and do not change eBay inventory.
- Per-account OAuth authorization-code connection, server-side encrypted access/refresh tokens, refresh, reconnect, disconnect, paginated/idempotent order sync, and retry handling for transient errors.
- Sync attempts for Inventory API inventory/offer reads and Analytics traffic reports. Unsupported marketplaces/errors are shown as per-account coverage errors.
- Exact decimal handling in server-side calculation/export; original currencies are retained and never aggregated across currencies.

## Important live-data coverage limits

1. **Finances are retained separately from revenue.** The app stores raw Finances transactions in an account-scoped ledger and never adds transfers/payouts to order sales. It currently does not reconcile Finances fee/refund/fee-credit rows into order profit. The order-level fee/refund fields therefore remain `Pending`; profit/share remain incomplete until those entries can be reconciled. For UK/EU-domiciled sellers, all Finances methods require eBay digital-signature headers. This app does not yet generate RFC 9421 signatures or manage Key Management signing keys, so it skips Finances sync for the UK/EU marketplaces by default. Do not treat the live profit values as complete.
2. Fulfillment order records can be delayed/changed. The sync queries a rolling `lastmodifieddate` cursor and upserts by `(seller account, order ID)`. A full backfill can be started by clearing that account's cursor locally; eBay permits up to two years on `getOrders`.
3. Inventory API records/offers cover the Inventory API model. Traditional/legacy listings may not appear; eBay's active inventory report is a separate Feed workflow and is not implemented here.
   Low-stock checks only use quantities eBay returns for those items; they do not query a dropshipping supplier's live stock or automatically restock/revise a listing.
4. Analytics traffic and seller metrics depend on marketplace support and eBay-granted scopes. Traffic reporting is date-bounded (up to 90 days per request) and eBay says it is intended for a few thousand listings per day. Conversion/traffic data stays separate from financial calculations.
5. Marketplace-collected tax is stored separately and excluded from proceeds. This is not tax advice or a tax-liability calculation. Costs in a currency different from the order currency are ignored by calculation and should be corrected rather than converted without an explicit dated FX source.
6. Sale proceeds come from Fulfillment order pricing summary. Fees, refunds, shipping-label charges paid outside eBay, and fee credits need financial reconciliation before they can be considered complete. Manual costs do not claim to replace those API records.

## Run locally

Requires Python 3.10+ and an internet connection only for installing the two Python packages and calling eBay. For a purely synthetic demo, no eBay account is needed.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Generate two independent secrets:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(48))' # APP_SECRET
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' # TOKEN_ENCRYPTION_KEY
```

Set both values in `.env`. Set `COOKIE_SECURE=1` when served behind HTTPS. Keep `.env` and `dashboard.sqlite3` private and backed up. Then run:

```sh
set -a; source .env; set +a
python app.py
```

Open http://127.0.0.1:5000. The app is deliberately bound to loopback. Do not expose it to a network without putting it behind HTTPS, setting secure cookies, and arranging secure secret/database storage. No public deployment has been performed.

## eBay Developer Portal configuration

1. Sign in at [developer.ebay.com](https://developer.ebay.com/) and create an application keyset. Start with Sandbox keys and separate sandbox seller test users; switch `EBAY_ENV=production` only after production access is approved.
2. In the keyset's **User Tokens** / OAuth settings, create an RuName (redirect URL name). Set its accepted redirect URL to `http://localhost:5000/oauth/callback` for local development. Copy the resulting RuName exactly into `EBAY_RUNAME`; `EBAY_REDIRECT_URI` records the associated local callback URL. OAuth requests send the RuName in eBay's `redirect_uri` field, and it must match the app keyset.
3. Copy the keyset's Client ID and Client Secret into `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`; do not put them in frontend code or source control.
4. Grant/request only these read scopes for the current implementation: `sell.fulfillment.readonly`, `sell.finances`, `sell.inventory.readonly`, and `sell.analytics.readonly`. eBay may require application checks/production access before production authorization or API calls. The consent URL requests these scopes, but the keyset and seller grant must actually permit them.
5. Log in to this private app and connect each seller account independently. The seller completes eBay OAuth in eBay's own consent page; never enter an eBay password into this app. Repeat per account. Click **Sync** on each account.
6. For UK/EU-domiciled sellers, separately complete eBay's [Digital Signatures for APIs](https://developer.ebay.com/develop/guides/sell/digital-signatures-for-apis) and [Key Management API](https://developer.ebay.com/api-docs/developer/key-management/static/overview.html) production prerequisites. Finances calls currently remain disabled for those sellers until signing is implemented and tested. No eBay credentials were supplied, so production API access and granted scopes are not verified here.

## Data and safety

SQLite is created beside `app.py` by default (`DATABASE_PATH` can move it). Tokens are encrypted with Fernet; losing the encryption key means reconnecting all sellers. All writes to seller accounts are excluded: this app only calls OAuth token endpoints and GET Sell API methods. It does not edit listings, issue refunds, send messages, or change orders.

Manual cost CSV format:

```csv
account_id,order_id,sku,kind,amount,currency,note
1,ORDER-123,SKU-8,product,12.50,GBP,invoice 510
1,,SKU-8,fulfilment,3.20,GBP,packing and postage
```

Allowed `kind` values: `product`, `fulfilment`, `other`. Use order ID for an order override; SKU can be used to associate an entry with matching order line(s). SKU defaults are entered in the UI. Keep source records/adjustments for audit.

## Checks

Run `python -m unittest discover -s tests`. The test suite covers decimal profit/refund arithmetic, missing costs, effective-dated shares, loss-sharing, account isolation, and duplicate upserts. The UI can be opened in demo mode to review filters, charts, tables and responsive layout without touching live records.
