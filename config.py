"""
Central configuration for the Northwest Washington DC 'listing removed'
seller-lead pipeline.

IMPORTANT / READ BEFORE RUNNING
--------------------------------
1. Redfin scraping: Redfin's Terms of Use prohibit automated scraping of
   its site. This module uses Playwright to render the public search page
   the same way a browser would, but you are responsible for confirming
   this is compliant with Redfin's ToS (or licensing an IDX/MLS/RETS feed,
   or a data vendor such as RentCast / ATTOM / Estated instead) before
   running this in production. Selectors below are best-effort as of the
   time this was written and WILL need to be re-checked against the live
   DOM (Redfin changes markup often and may show a CAPTCHA/interstitial to
   bots) -- see `redfin.py` for the fallback/verification hooks.

2. DC OTR bulk parcel file: the page given
   (https://otr.cfo.dc.gov/page/real-property-tax-database-search) is a
   *marketing/landing* page. It links out to the interactive
   MyTax.DC.gov parcel search tool -- it does NOT itself expose a bulk
   DBF download. The District's actual bulk parcel extract (assessment
   roll, incl. owner name & mailing address) is published on the DC
   Open Data portal (opendata.dc.gov, "Computer Assisted Mass Appraisal"
   / "Integrated Tax System Public Extract" layers), typically as a
   Shapefile/DBF or CSV. You MUST set PARCEL_DBF_URL below to the actual
   file location you're licensed/authorized to use. The loader in
   `parcel_data.py` is written generically against the DBF column names
   you specified so it will work once pointed at a real file.
"""
import os

# ---------------------------------------------------------------------------
# Lead source
# ---------------------------------------------------------------------------
PORTAL_URL = (
    "https://www.redfin.com/neighborhood/125413/DC/Washington-DC/"
    "Northwest-Washington/filter/property-type=house+townhouse+land"
)
NEIGHBORHOOD_NAME = "Northwest Washington DC"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 700))
LEAD_TYPE = "Listing Removed"

# How many days after a listing disappears from Redfin we allow for a
# matching deed/sale to show up in the parcel roll before concluding it
# wasn't a sale (i.e. it expired/was withdrawn rather than closing).
# Redfin/most MLSs don't expose a raw "Expired" status publicly, so this
# window is the proxy signal: no recorded sale near the removal date =
# treated as expired. Widen if your county's recording lag is longer.
SALE_CONFIRMATION_WINDOW_DAYS = int(os.environ.get("SALE_CONFIRMATION_WINDOW_DAYS", 60))

# If true, records.json only contains listings classified as expired/
# withdrawn (sale ruled out). If false, sold-and-removed listings are
# still written but tagged cat="sold_not_expired" so you can inspect them.
EXPIRED_ONLY = os.environ.get("EXPIRED_ONLY", "true").lower() != "false"

# If true, any expired-listing record the bulk parcel file couldn't
# match gets a live, per-address MyTax.DC.gov lookup as a fallback (see
# mytax_lookup.py). Off by default so a missing/misconfigured bulk file
# doesn't silently turn every run into N live gov't-portal page loads --
# turn on once you've verified mytax_lookup.py's selectors against the
# live site.
MYTAX_LIVE_ENRICHMENT = os.environ.get("MYTAX_LIVE_ENRICHMENT", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Parcel / property appraiser bulk data
# ---------------------------------------------------------------------------
# Set this to the direct URL of the bulk parcel DBF (or a .zip containing
# a .dbf) published by DC OTR / DC Open Data. Left blank by default so the
# pipeline fails loudly instead of silently using fake data.
PARCEL_DBF_URL = os.environ.get("PARCEL_DBF_URL", "")

# Candidate column names (bulk assessor exports vary by vintage/vendor).
# The loader checks each alias in order and uses whichever is present.
PARCEL_COLUMN_ALIASES = {
    "owner": ["OWNER", "OWN1", "OWNERNAME", "OWNER_NAME"],
    "site_addr": ["SITE_ADDR", "SITEADDR", "PREMISEADD", "SITUS_ADDR"],
    "site_city": ["SITE_CITY", "SITECITY", "SITUS_CITY"],
    "site_zip": ["SITE_ZIP", "SITEZIP", "SITUS_ZIP"],
    "mail_addr": ["ADDR_1", "MAILADR1", "MAIL_ADDR1", "MAILING_ADDRESS"],
    "mail_city": ["CITY", "MAILCITY", "MAIL_CITY"],
    "mail_state": ["STATE", "MAILSTATE", "MAIL_STATE"],
    "mail_zip": ["ZIP", "MAILZIP", "MAIL_ZIP"],
    "legal": ["LEGAL", "LEGAL_DESC", "LEGALDESC"],
    "debt": ["TAXDUE", "AMTDUE", "BALANCE", "AMOUNT_DUE", "TOTAL_DUE"],
    "parcel_id": ["SSL", "PARCEL_ID", "SQSUFLOT", "PARID"],
    # Needed to distinguish "expired" (no sale followed) from "sold"
    # (a deed was actually recorded around the same time it vanished).
    "sale_date": ["SALEDATE", "SALE_DATE", "DEEDDATE", "DEED_DATE", "LASTSALEDATE"],
    "sale_price": ["SALEPRICE", "SALE_PRICE", "PRICE", "LASTSALEPRICE"],
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATHS = [
    os.path.join(REPO_ROOT, "dashboard", "records.json"),
    os.path.join(REPO_ROOT, "data", "records.json"),
]
STATE_PATH = os.path.join(REPO_ROOT, "data", "listings_seen.json")
PARCEL_CACHE_PATH = os.path.join(REPO_ROOT, "data", "parcel_cache.dbf")
GHL_EXPORT_PATH = os.path.join(REPO_ROOT, "data", "ghl_export.csv")

# ---------------------------------------------------------------------------
# Google Sheets export
# ---------------------------------------------------------------------------
# Push the same rows as the GHL CSV into a Google Sheet. Requires a
# Google Cloud service account with Sheets API enabled, shared as an
# Editor on the target spreadsheet. See README for setup steps.
GOOGLE_SHEETS_ENABLED = os.environ.get("GOOGLE_SHEETS_ENABLED", "false").lower() == "true"
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")  # the long ID in the sheet's URL
GOOGLE_SHEET_WORKSHEET_NAME = os.environ.get("GOOGLE_SHEET_WORKSHEET_NAME", "Leads")
# Full JSON key content (not a file path) -- pass it as a GitHub secret
# and it'll be written to a temp file at runtime. Keep this out of the repo.
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# ---------------------------------------------------------------------------
# Networking / retry behavior
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 4
REQUEST_TIMEOUT = 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
