"""
Pushes the same rows as ghl_export.py's CSV into a Google Sheet, using a
Google Cloud service account (no interactive OAuth needed -- works fine
in a headless GitHub Actions runner).

ONE-TIME SETUP
--------------
1. Go to console.cloud.google.com -> create/select a project.
2. Enable the "Google Sheets API" for that project.
3. Create a Service Account (IAM & Admin -> Service Accounts), then
   create a JSON key for it and download it.
4. Create (or open) the target Google Sheet, and "Share" it with the
   service account's email address (looks like
   `something@your-project.iam.gserviceaccount.com`) as an Editor.
5. Copy the Sheet ID out of its URL:
   https://docs.google.com/spreadsheets/d/<THIS_PART>/edit
6. Set these as GitHub Actions repo secrets (Settings -> Secrets and
   variables -> Actions):
     - GOOGLE_SERVICE_ACCOUNT_JSON  -> paste the ENTIRE downloaded JSON
       key file content (it's just text)
     - GOOGLE_SHEET_ID              -> the ID from step 5
7. Set GOOGLE_SHEETS_ENABLED=true (workflow env or repo variable).

Locally, you can instead point GOOGLE_SERVICE_ACCOUNT_JSON at a file
path if you prefer -- see `_load_credentials_dict` below, it accepts
either raw JSON text or a path to a .json file.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import List

import config
from ghl_export import GHL_COLUMNS, _record_to_row
from utils import get_logger, retry

log = get_logger("sheets_export")


def _load_credentials_dict() -> dict:
    raw = config.GOOGLE_SERVICE_ACCOUNT_JSON
    if not raw:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set -- see sheets_export.py "
            "docstring for setup steps."
        )
    # Accept either the raw JSON text (typical for a GitHub secret) or a
    # path to a .json file (convenient for local runs).
    if os.path.isfile(raw):
        with open(raw, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(raw)


@retry(max_attempts=config.MAX_RETRIES, backoff_seconds=config.RETRY_BACKOFF_SECONDS)
def _get_worksheet():
    import gspread  # imported lazily so the module doesn't hard-require
    # gspread/google-auth unless Sheets export is actually used.

    creds_dict = _load_credentials_dict()
    client = gspread.service_account_from_dict(creds_dict)

    if not config.GOOGLE_SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is not set.")

    spreadsheet = client.open_by_key(config.GOOGLE_SHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(config.GOOGLE_SHEET_WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=config.GOOGLE_SHEET_WORKSHEET_NAME, rows=1000, cols=len(GHL_COLUMNS)
        )
    return worksheet


def export_to_google_sheet(records: List[dict]) -> int:
    """Overwrites the target worksheet with the current header + rows.
    Returns the number of data rows written. Never raises past this
    function -- a Sheets outage shouldn't fail the whole scraper run;
    the caller in fetch.py already wraps this in a try/except, but this
    function also degrades internally (logs + returns 0) on bad rows."""
    if not config.GOOGLE_SHEETS_ENABLED:
        return 0

    rows = [GHL_COLUMNS]
    for rec in records:
        try:
            row_dict = _record_to_row(rec)
            rows.append([row_dict.get(col, "") for col in GHL_COLUMNS])
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping record %s in Sheets export: %s",
                        rec.get("doc_num", "?"), exc)
            continue

    worksheet = _get_worksheet()
    # Full overwrite each run: simplest way to keep the sheet in sync
    # with records.json without reconciling row-by-row diffs. If you'd
    # rather append/preserve manual edits made in the sheet between
    # runs, switch this to worksheet.append_rows(rows[1:]) instead.
    worksheet.clear()
    worksheet.update(values=rows, range_name="A1")

    log.info("Wrote %d rows to Google Sheet (worksheet '%s')",
              len(rows) - 1, config.GOOGLE_SHEET_WORKSHEET_NAME)
    return len(rows) - 1
