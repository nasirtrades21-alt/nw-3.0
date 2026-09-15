# NW Washington DC — "Listing Removed" Seller Lead Pipeline

Automated pipeline that watches the Redfin Northwest Washington, DC
neighborhood filter, detects listings that disappear (withdrawn/expired/
delisted), and enriches each one with DC OTR parcel data (owner, mailing
address, legal description, tax debt owed) for seller-lead generation.

## Before you run this in production

**1. Redfin scraping and ToS.** Redfin's Terms of Use restrict automated
data collection. This code renders the public search/listing pages with
Playwright the way a browser would, but it is *your* responsibility to
confirm this use is compliant, or to swap `redfin.py` for a licensed
IDX/MLS feed or a paid listing-data API (e.g. MLS Grid, RentCast, ATTOM).
Redfin's markup also changes often and can serve bot checks to headless
browsers — selectors in `redfin.py` are best-effort and should be
re-verified against the live page before you trust the output.

**2. DC OTR bulk parcel file.** The page you provided
(`otr.cfo.dc.gov/page/real-property-tax-database-search`) is a landing
page that links out to the interactive **MyTax.DC.gov** parcel search —
it does not itself host a bulk DBF download. The District's bulk
assessment-roll extract (owner name, mailing address, etc.) is published
separately on DC's Open Data portal. You must locate the current file
there (or through OTR's Public Extract program) and set:

```bash
export PARCEL_DBF_URL="https://.../parcels.dbf"   # or a .zip containing one
```

or add it as the `PARCEL_DBF_URL` GitHub Actions secret. Until this is
set, the pipeline still runs and produces "Listing Removed" leads — they
just won't have owner/mailing/debt enrichment (each such record gets a
`no_parcel_match` flag).

## Setup

```bash
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
export PARCEL_DBF_URL="..."          # see above
python scraper/fetch.py
```

## How "Expired Listing" is detected

Redfin has no public MLS-status field (Active/Expired/Withdrawn/Canceled/
Sold) — a listing just disappears from the site either way. The pipeline
detects removal with a snapshot diff (every run scrapes all currently-
active listings into `data/listings_seen.json`; anything present last
run but missing now, and first seen within `LOOKBACK_DAYS`, is a
candidate), then classifies *why* it disappeared using the parcel roll's
sale date as a proxy signal:

- If the parcel roll shows a sale recorded within
  `SALE_CONFIRMATION_WINDOW_DAYS` (default 60) of the removal → classified
  `sold_not_expired` and excluded from `records.json` by default.
- If no matching recent sale shows up (or there's no parcel match at
  all) → classified `expired` and included, with a `no_parcel_match` /
  `expiration_unconfirmed` flag when the sale check couldn't be run.

Set `EXPIRED_ONLY=false` if you'd rather keep the sold ones in the
output (tagged accordingly) instead of dropping them. This is a proxy,
not ground truth — if you have or can license real MLS RETS/RESO access
(e.g. via MLS Grid, or your local Bright MLS account), pulling the
actual `StandardStatus=Expired` field there will be far more reliable
than inferring it from a public IDX site plus tax-roll sale dates.

## Getting owner info via the DC property tax database search itself

The OTR page you started from links out to **MyTax.DC.gov**, which is a
*live, per-address (or SSL) search tool* — not a bulk file. No login is
required for the public search. `scraper/mytax_lookup.py` automates that
exact workflow with Playwright: search by address → open the result row
→ open the SSL detail page → read owner name, mailing address, legal
description, and amount due.

This runs automatically as a **fallback**, not the primary path: the
bulk parcel file (`parcel_data.py`) is tried first since it's instant
for every address, and `mytax_lookup.py` only fires for records still
flagged `no_parcel_match` afterward. Enable it with:

```bash
export MYTAX_LIVE_ENRICHMENT=true
```

It's off by default because it hasn't been verified against MyTax's live
DOM (a dynamic tax-platform app whose markup wasn't inspectable while
writing this) — run it once with `MyTaxOwnerLookup(headless=False)` in
`mytax_lookup.py` and confirm the selectors actually find the address
field, results table, and detail-page labels before trusting it, and
expect to adjust the label-matching regexes (`_extract_after_label`) to
whatever MyTax's detail page actually renders. It also self-throttles
(`MYTAX_REQUEST_DELAY_SECONDS`, default 3s) since it's driving a live
government portal — fine for enriching a handful of daily leads, not
meant for bulk harvesting.

## Sending leads to a Google Sheet

`scraper/sheets_export.py` pushes the exact same rows as the GHL CSV
into a Google Sheet, using a service account (no interactive login, so
it works fine in a headless GitHub Actions runner). One-time setup:

1. In [Google Cloud Console](https://console.cloud.google.com), create/
   select a project and enable the **Google Sheets API**.
2. Create a **Service Account** (IAM & Admin → Service Accounts), then
   create and download a **JSON key** for it.
3. Open your target Google Sheet and **Share** it with the service
   account's email (looks like `xyz@your-project.iam.gserviceaccount.com`)
   as an **Editor**.
4. Copy the **Sheet ID** out of its URL:
   `https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`
5. Add these as **GitHub repo secrets** (Settings → Secrets and
   variables → Actions):
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the *entire* downloaded JSON
     key file content (it's plain text)
   - `GOOGLE_SHEET_ID` — the ID from step 4
6. Set `GOOGLE_SHEETS_ENABLED=true` (already set in the included
   workflow).

Each run fully overwrites the worksheet (default tab name `Leads`,
change via `GOOGLE_SHEET_WORKSHEET_NAME`) with the current header +
rows, so it always matches `records.json`. If you'd rather append new
rows and preserve manual edits made directly in the sheet between runs,
swap the `worksheet.clear()` / `worksheet.update()` call in
`sheets_export.py` for `worksheet.append_rows(...)`.

Locally, `GOOGLE_SERVICE_ACCOUNT_JSON` can also point at a `.json` file
path instead of raw text, if that's easier for testing:

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account-key.json
export GOOGLE_SHEET_ID="your-sheet-id"
export GOOGLE_SHEETS_ENABLED=true
python scraper/fetch.py
```

## Files

| Path | Purpose |
|---|---|
| `scraper/fetch.py` | Main entrypoint / orchestrator |
| `scraper/config.py` | URLs, column aliases, paths, retry settings |
| `scraper/redfin.py` | Playwright scraper for search + detail pages |
| `scraper/parcel_data.py` | DBF download + owner/address indexes |
| `scraper/state.py` | Snapshot persistence for the removal diff |
| `scraper/lead_builder.py` | Diff → scored, enriched lead records |
| `scraper/output.py` | Writes `records.json` to both destinations |
| `scraper/ghl_export.py` | GoHighLevel CSV export |
| `dashboard/records.json` / `data/records.json` | Output (schema below) |
| `data/listings_seen.json` | Internal state, not a deliverable |
| `.github/workflows/scrape.yml` | Daily cron + GitHub Pages deploy |

## Output schema (`records.json`)

```json
{
  "fetched_at": "...",
  "source": "Northwest Washington DC",
  "date_range": {"lookback_days": 700, "as_of": "..."},
  "total": 0,
  "with_address": 0,
  "records": [
    {
      "doc_num": "...", "doc_type": "MLS Listing", "filed": "...",
      "cat": "listing_removed", "cat_label": "Listing Removed",
      "owner": "...", "grantee": "", "amount": null, "legal": "...",
      "prop_address": "...", "prop_city": "...", "prop_state": "DC",
      "prop_zip": "...", "mail_address": "...", "mail_city": "...",
      "mail_state": "...", "mail_zip": "...", "clerk_url": "...",
      "flags": ["listing_removed"], "score": 60
    }
  ]
}
```

(A few extra fields — `prop_living_sqft`, `prop_lot_sqft`, `prop_beds`,
`prop_baths`, `listing_price`, `listing_description`, `listing_history` —
are included alongside the required schema since they were requested as
data to capture.)
