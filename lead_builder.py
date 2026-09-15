"""Turns a (previous_snapshot, current_snapshot) diff into scored,
parcel-enriched 'Listing Removed' lead records matching the required
output schema."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import config
from parcel_data import ParcelDatabase, ParcelRecord
from redfin import Listing
from utils import get_logger

log = get_logger("lead_builder")


def _days_since(iso_str: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(iso_str)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:  # noqa: BLE001
        return None


def _parse_sale_date(raw) -> Optional[date]:
    """Assessor exports store sale dates in all sorts of formats
    (datetime.date objects from dbfread, 'YYYYMMDD' strings, 'MM/DD/YYYY'
    strings...). Try the common ones and give up quietly on anything else."""
    if raw in (None, "", 0):
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    text = str(raw).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _classify_removal(parcel: Optional[ParcelRecord]) -> tuple[str, str]:
    """Distinguish 'expired/withdrawn' from 'sold' for a listing that
    disappeared from the portal. Redfin (and most consumer IDX sites)
    doesn't expose the underlying MLS status (Active/Expired/Withdrawn/
    Canceled/Sold) -- it just stops showing the listing either way. The
    proxy signal used here: if the parcel roll shows a sale recorded
    close to "now" (within SALE_CONFIRMATION_WINDOW_DAYS), the property
    almost certainly sold rather than expired. No matching recent sale
    (or no parcel match at all) is treated as an expired/withdrawn lead.
    """
    if parcel is None:
        return "expired", "Expired Listing (unconfirmed - no parcel match)"

    sale_dt = _parse_sale_date(parcel.sale_date)
    if sale_dt is None:
        return "expired", "Expired Listing"

    days_since_sale = (datetime.now(timezone.utc).date() - sale_dt).days
    if 0 <= days_since_sale <= config.SALE_CONFIRMATION_WINDOW_DAYS:
        return "sold_not_expired", "Sold (not expired)"

    return "expired", "Expired Listing"


def _clerk_url(listing: Listing) -> str:
    # The original listing URL is the most durable public reference we
    # have for a removed listing (Redfin still resolves old URLs to a
    # "delisted" page for a period). Kept as its own field so it can be
    # swapped for a DC ROD/clerk document URL if/when a corresponding
    # recorded instrument is matched.
    return listing.url


def _score(listing: Listing, has_parcel_match: bool, debt: Optional[float]) -> int:
    """Simple, transparent lead score (0-100). Tune freely -- this is a
    starting heuristic, not a validated model."""
    score = 40  # base score for any listing classified as expired
    if has_parcel_match:
        score += 20  # confirmed via parcel roll that no sale followed
    if debt and debt > 0:
        score += min(20, int(debt // 1000))
    if listing.listing_history:
        # multiple price cuts / relistings often correlate with motivation
        score += min(10, len(listing.listing_history) * 2)
    if listing.description:
        score += 5
    return max(0, min(100, score))


def _flags(listing: Listing, has_parcel_match: bool, debt: Optional[float], cat: str) -> List[str]:
    flags = ["listing_removed", cat]
    if not has_parcel_match:
        flags.append("no_parcel_match")
        flags.append("expiration_unconfirmed")
    if debt and debt > 0:
        flags.append("tax_debt_owed")
    if not listing.description:
        flags.append("missing_description")
    return flags


def build_removed_listing_leads(
    previous: Dict[str, Listing],
    current: Dict[str, Listing],
    parcels: ParcelDatabase,
    lookback_days: int = config.LOOKBACK_DAYS,
) -> List[dict]:
    records: List[dict] = []
    removed_ids = set(previous.keys()) - set(current.keys())
    log.info("%d listings disappeared since last run", len(removed_ids))

    for lid in removed_ids:
        try:
            listing = previous[lid]
            age_days = _days_since(listing.first_seen)
            if age_days is not None and age_days > lookback_days:
                continue  # outside the requested lookback window

            record = _build_single_record(listing, parcels)
            if config.EXPIRED_ONLY and record["cat"] != "expired":
                log.info("Excluding %s (%s) -- classified as %s, not expired",
                          listing.address, lid, record["cat_label"])
                continue
            records.append(record)
        except Exception as exc:  # noqa: BLE001
            # Never let one bad record kill the whole run.
            log.error("Skipping malformed removed-listing record for %s: %s", lid, exc)
            continue

    return records


def _build_single_record(listing: Listing, parcels: ParcelDatabase) -> dict:
    parcel = parcels.match_by_address(listing.address)
    has_match = parcel is not None
    cat, cat_label = _classify_removal(parcel)

    owner = parcel.owner if parcel else ""
    mail_addr = parcel.mail_addr if parcel else ""
    mail_city = parcel.mail_city if parcel else ""
    mail_state = parcel.mail_state if parcel else ""
    mail_zip = parcel.mail_zip if parcel else ""
    legal = parcel.legal if parcel else ""
    debt = parcel.debt if parcel else None

    return {
        "doc_num": listing.listing_id,
        "doc_type": "MLS Listing",
        "filed": listing.first_seen,
        "cat": cat,
        "cat_label": cat_label,
        "owner": owner,
        "grantee": "",
        "amount": debt,
        "legal": legal or "",
        "prop_address": listing.address,
        "prop_city": listing.city,
        "prop_state": listing.state,
        "prop_zip": listing.zip_code or (parcel.site_zip if parcel else ""),
        "prop_living_sqft": listing.living_sqft,
        "prop_lot_sqft": listing.lot_sqft,
        "prop_beds": listing.beds,
        "prop_baths": listing.baths,
        "listing_price": listing.price,
        "listing_description": listing.description,
        "listing_history": listing.listing_history,
        "mail_address": mail_addr or "",
        "mail_city": mail_city or "",
        "mail_state": mail_state or "",
        "mail_zip": mail_zip or "",
        "clerk_url": _clerk_url(listing),
        "flags": _flags(listing, has_match, debt, cat),
        "score": _score(listing, has_match, debt),
    }
