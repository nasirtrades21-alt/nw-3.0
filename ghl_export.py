"""Builds the GoHighLevel (GHL) import CSV from records.json-shaped
lead dicts."""
from __future__ import annotations

import csv
from typing import List

import config
from utils import get_logger, split_owner_name

log = get_logger("ghl_export")

GHL_COLUMNS = [
    "First Name", "Last Name", "Mailing Address", "Mailing City",
    "Mailing State", "Mailing Zip", "Property Address", "Property City",
    "Property State", "Property SqFt", "Lot Size Sqft", "Property Zip",
    "Lead Type", "Amount/Debt Owed", "Source", "Public Records URL",
]


def export_ghl_csv(records: List[dict], out_path: str = config.GHL_EXPORT_PATH) -> str:
    """Writes a GHL-ready CSV and returns the path. Skips (rather than
    crashes on) any record missing enough data to be useful, but logs
    every skip so nothing silently disappears."""
    rows_written = 0
    try:
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=GHL_COLUMNS)
            writer.writeheader()
            for rec in records:
                try:
                    row = _record_to_row(rec)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Skipping record %s in GHL export: %s",
                                rec.get("doc_num", "?"), exc)
                    continue
                writer.writerow(row)
                rows_written += 1
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to write GHL export to %s: %s", out_path, exc)
        raise

    log.info("Wrote %d rows to GHL export at %s", rows_written, out_path)
    return out_path


def _record_to_row(rec: dict) -> dict:
    first, last = split_owner_name(rec.get("owner", ""))
    return {
        "First Name": first,
        "Last Name": last,
        "Mailing Address": rec.get("mail_address", ""),
        "Mailing City": rec.get("mail_city", ""),
        "Mailing State": rec.get("mail_state", ""),
        "Mailing Zip": rec.get("mail_zip", ""),
        "Property Address": rec.get("prop_address", ""),
        "Property City": rec.get("prop_city", ""),
        "Property State": rec.get("prop_state", ""),
        "Property SqFt": rec.get("prop_living_sqft", ""),
        "Lot Size Sqft": rec.get("prop_lot_sqft", ""),
        "Property Zip": rec.get("prop_zip", ""),
        "Lead Type": rec.get("cat_label", config.LEAD_TYPE),
        "Amount/Debt Owed": rec.get("amount", ""),
        "Source": config.NEIGHBORHOOD_NAME,
        "Public Records URL": rec.get("clerk_url", ""),
    }
