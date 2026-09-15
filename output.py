"""Writes records.json to every configured output path with the exact
schema requested."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import List

import config
from utils import get_logger

log = get_logger("output")


def write_records(records: List[dict], paths: List[str] = None) -> dict:
    paths = paths or config.OUTPUT_PATHS
    with_address = sum(1 for r in records if r.get("prop_address"))

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": config.NEIGHBORHOOD_NAME,
        "date_range": {
            "lookback_days": config.LOOKBACK_DAYS,
            "as_of": datetime.now(timezone.utc).date().isoformat(),
        },
        "total": len(records),
        "with_address": with_address,
        "records": records,
    }

    for path in paths:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            os.replace(tmp_path, path)
            log.info("Wrote %d records to %s", len(records), path)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed writing output to %s: %s", path, exc)

    return payload
