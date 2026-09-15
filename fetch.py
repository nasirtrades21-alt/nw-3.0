#!/usr/bin/env python3
"""
Northwest Washington DC 'Listing Removed' seller-lead pipeline.

Run:
    python scraper/fetch.py

Pipeline:
    1. Scrape all currently-active Redfin listings in the neighborhood
       filter (Playwright).
    2. Diff against the previous run's saved snapshot -> listings that
       disappeared = candidate "Expired Listing" leads.
    3. Enrich/classify each removed listing with DC OTR bulk parcel data
       (owner, mailing address, legal description, tax debt owed, and
       sale-date-based expired-vs-sold classification) via address match.
    4. For any record the bulk file couldn't match, optionally fall back
       to a live per-address MyTax.DC.gov search (mytax_lookup.py) --
       enable with MYTAX_LIVE_ENRICHMENT=true once you've verified its
       selectors against the live site.
    5. Write dashboard/records.json and data/records.json.
    6. Persist the new snapshot for next run's diff.
    7. Build the GoHighLevel CSV export.
    8. Push the same rows to a Google Sheet (if configured).

Designed to never crash on a single bad record -- failures are logged
and skipped so one malformed listing or parcel row doesn't take down
the whole scheduled run.
"""
from __future__ import annotations

import asyncio
import sys

import config
from ghl_export import export_ghl_csv
from lead_builder import build_removed_listing_leads
from mytax_lookup import enrich_records_via_mytax
from output import write_records
from parcel_data import ParcelDatabase
from redfin import RedfinScraper
from sheets_export import export_to_google_sheet
from state import load_previous_snapshot, merge_snapshots, save_snapshot
from utils import get_logger

log = get_logger("fetch")


async def run() -> int:
    log.info("=== Starting run: %s (lookback=%d days) ===",
              config.NEIGHBORHOOD_NAME, config.LOOKBACK_DAYS)

    # 1. Previous snapshot (may be empty on first run)
    previous = load_previous_snapshot(config.STATE_PATH)
    log.info("Loaded previous snapshot: %d listings", len(previous))

    # 2. Scrape current active listings
    scraper = RedfinScraper(config.PORTAL_URL)
    try:
        current = await scraper.scrape_active_listings()
    except Exception as exc:  # noqa: BLE001
        log.error("Redfin scrape failed entirely, aborting this run so we "
                   "don't mistake a scraper outage for mass delistings: %s", exc)
        return 1
    log.info("Scraped current active listings: %d", len(current))

    if not current and not previous:
        log.warning("No listings found on first run -- check selectors / "
                     "bot-detection before trusting future diffs.")

    # 3. Load parcel data (best-effort; enrichment degrades gracefully)
    parcels = ParcelDatabase().load()

    # 4. Build removed-listing leads
    records = build_removed_listing_leads(previous, current, parcels)
    log.info("Built %d 'Listing Removed' lead records", len(records))

    # 4b. Fill in owner/mailing/legal/debt for any record the bulk file
    # couldn't match, via a live per-address MyTax.DC.gov search.
    if config.MYTAX_LIVE_ENRICHMENT:
        try:
            await enrich_records_via_mytax(records)
        except Exception as exc:  # noqa: BLE001
            log.error("MyTax live enrichment pass failed (non-fatal): %s", exc)

    # 5. Write outputs
    write_records(records, config.OUTPUT_PATHS)

    # 6. Persist merged snapshot for next run
    merged = merge_snapshots(previous, current)
    save_snapshot(config.STATE_PATH, merged)

    # 7. GHL export
    try:
        export_ghl_csv(records)
    except Exception as exc:  # noqa: BLE001
        log.error("GHL export failed (non-fatal): %s", exc)

    # 8. Google Sheets export (same rows as the GHL CSV)
    try:
        export_to_google_sheet(records)
    except Exception as exc:  # noqa: BLE001
        log.error("Google Sheets export failed (non-fatal): %s", exc)

    log.info("=== Run complete: %d leads ===", len(records))
    return 0


def main() -> None:
    exit_code = asyncio.run(run())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
