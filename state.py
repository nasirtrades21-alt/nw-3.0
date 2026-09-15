"""Persists the last-seen snapshot of active listings so we can detect
'listing removed' by diffing across runs."""
from __future__ import annotations

import json
import os
from typing import Dict

from redfin import Listing
from utils import get_logger

log = get_logger("state")


def load_previous_snapshot(path: str) -> Dict[str, Listing]:
    if not os.path.exists(path):
        log.info("No previous snapshot found at %s (first run)", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {lid: Listing.from_dict(d) for lid, d in raw.items()}
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read previous snapshot (%s), treating as empty: %s", path, exc)
        return {}


def save_snapshot(path: str, listings: Dict[str, Listing]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {lid: l.to_dict() for lid, l in listings.items()}
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp_path, path)
    log.info("Saved snapshot of %d listings to %s", len(listings), path)


def merge_snapshots(previous: Dict[str, Listing], current: Dict[str, Listing]) -> Dict[str, Listing]:
    """New snapshot to persist: current listings win (fresh data), but we
    keep each listing's original `first_seen` so lookback-window checks
    stay accurate across runs."""
    merged: Dict[str, Listing] = {}
    for lid, listing in current.items():
        prior = previous.get(lid)
        if prior:
            listing.first_seen = prior.first_seen
        merged[lid] = listing
    return merged
