"""
Live, per-address owner/parcel lookup against MyTax.DC.gov's public real
property search -- the actual interactive tool behind the OTR "Real
Property Tax Database Search" page. Use this INSTEAD OF or ALONGSIDE
parcel_data.py's bulk-DBF loader.

Trade-off vs. the bulk file:
  - Bulk DBF (parcel_data.py): one big download, instant lookups for
    every parcel in the District, but you have to locate/license the
    actual bulk extract (see parcel_data.py's docstring).
  - This module: no bulk file needed at all -- it drives the public
    search UI per address, which is exactly what "getting owner info
    via the DC property tax database search" means. Slower (one page
    load per lead) and more fragile to markup changes, but for a
    handful of expired-listing addresses a day it's simpler and doesn't
    depend on ever finding/licensing a bulk source.

MyTax.DC.gov is a live government tax portal used by real taxpayers.
Be a good citizen: this module deliberately rate-limits itself
(MYTAX_REQUEST_DELAY_SECONDS between lookups) and is meant for a small
number of per-day enrichment lookups, not bulk harvesting.

VERIFY BEFORE RELYING ON THIS: the selectors below are written against
the documented public workflow (OTR's own "How to Search for a Real
Property Account" guide: MyTax.DC.gov -> Real Property section ->
"Search Real Property by Address or SSL" -> results row -> SSL detail
page), using resilient text/label matching rather than guessed CSS
class names, since the live DOM wasn't available to inspect while
writing this. Run once with `headless=False` and confirm each selector
actually matches before trusting the output, and expect to adjust them
-- MyTax is an Angular-style dynamic app and its markup can change
without notice.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

import config
from utils import async_retry, get_logger, normalize_address, safe_float, split_owner_name

log = get_logger("mytax_lookup")

MYTAX_BASE = "https://mytax.dc.gov/_/"
MYTAX_REQUEST_DELAY_SECONDS = 3  # be polite to a live gov't tax portal


@dataclass
class OwnerLookupResult:
    address_queried: str
    matched: bool = False
    ssl: str = ""
    owner: str = ""
    first_name: str = ""
    last_name: str = ""
    mail_addr: str = ""
    mail_city: str = ""
    mail_state: str = ""
    mail_zip: str = ""
    legal: str = ""
    assessed_value: Optional[float] = None
    tax_due: Optional[float] = None
    detail_url: str = ""


class MyTaxOwnerLookup:
    """Drives the public MyTax.DC.gov real-property search, one address
    at a time. Use as an async context manager so the browser is reused
    across a batch of lookups instead of relaunching per address:

        async with MyTaxOwnerLookup() as lookup:
            result = await lookup.lookup_address("1234 16th St NW")
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None

    async def __aenter__(self) -> "MyTaxOwnerLookup":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(user_agent=config.USER_AGENT)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def lookup_address(self, address: str) -> OwnerLookupResult:
        result = OwnerLookupResult(address_queried=address)
        try:
            result = await async_retry(
                self._lookup, address,
                max_attempts=config.MAX_RETRIES,
                backoff_seconds=config.RETRY_BACKOFF_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("MyTax lookup failed for %r after retries: %s", address, exc)
        finally:
            await asyncio.sleep(MYTAX_REQUEST_DELAY_SECONDS)
        return result

    # -- internals ------------------------------------------------------
    async def _lookup(self, address: str) -> OwnerLookupResult:
        page = await self._context.new_page()
        try:
            await page.goto(MYTAX_BASE, wait_until="domcontentloaded", timeout=45000)

            await self._navigate_to_property_search(page)
            await self._submit_address_search(page, address)
            row = await self._read_first_result_row(page)

            result = OwnerLookupResult(address_queried=address)
            if row is None:
                log.info("No MyTax match for address: %s", address)
                return result

            result.matched = True
            result.ssl = row.get("ssl", "")
            result.owner = row.get("owner", "")
            result.first_name, result.last_name = split_owner_name(result.owner)

            detail = await self._open_detail_page(page, row)
            if detail:
                result.mail_addr = detail.get("mail_addr", "")
                result.mail_city = detail.get("mail_city", "")
                result.mail_state = detail.get("mail_state", "DC")
                result.mail_zip = detail.get("mail_zip", "")
                result.legal = detail.get("legal", "")
                result.assessed_value = safe_float(detail.get("assessed_value"))
                result.tax_due = safe_float(detail.get("tax_due"))
                result.detail_url = page.url

            return result
        finally:
            await page.close()

    async def _navigate_to_property_search(self, page: Page) -> None:
        """Find and click through to the 'Search Real Property by Address
        or SSL' link/panel. Uses text matching since it's the one thing
        OTR's own documentation confirms won't change wording."""
        try:
            link = page.get_by_text(re.compile("Search Real Property", re.I)).first
            await link.wait_for(timeout=15000)
            await link.click()
        except PWTimeout:
            # Some MyTax skins nest this under a "Real Property" panel first.
            panel = page.get_by_text(re.compile(r"^Real Property$", re.I)).first
            await panel.click(timeout=15000)
            link = page.get_by_text(re.compile("Search.*Address.*SSL", re.I)).first
            await link.click(timeout=15000)
        await page.wait_for_load_state("domcontentloaded")

    async def _submit_address_search(self, page: Page, address: str) -> None:
        # Prefer an explicit "Address" labeled field; fall back to the
        # first visible text input on the search form.
        field = None
        for label_pattern in ("Address", "Street Address", "Premise Address"):
            try:
                field = page.get_by_label(re.compile(label_pattern, re.I))
                await field.wait_for(timeout=5000)
                break
            except PWTimeout:
                field = None
        if field is None:
            field = page.locator("input[type='text']").first
            await field.wait_for(timeout=15000)

        await field.fill(address)

        try:
            search_btn = page.get_by_role("button", name=re.compile("search", re.I))
            await search_btn.click(timeout=10000)
        except PWTimeout:
            await field.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=20000)

    async def _read_first_result_row(self, page: Page) -> Optional[dict]:
        try:
            row = page.locator("table tbody tr").first
            await row.wait_for(timeout=15000)
        except PWTimeout:
            return None

        cells = await row.locator("td").all_inner_texts()
        if not cells:
            return None

        # Column order isn't guaranteed across MyTax skins, so this scans
        # cell text for shape (SSL looks like "0123 0456", owner is the
        # longest alphabetic cell) rather than assuming a fixed index.
        ssl = next((c.strip() for c in cells if re.match(r"^\d{4}\s*\d{4}$", c.strip())), "")
        owner = max(cells, key=lambda c: sum(ch.isalpha() for ch in c)).strip()
        ssl_link = row.locator("a").first

        return {"ssl": ssl, "owner": owner, "_link_locator": ssl_link}

    async def _open_detail_page(self, page: Page, row: dict) -> Optional[dict]:
        link = row.get("_link_locator")
        if link is None:
            return None
        try:
            await link.click(timeout=10000)
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except PWTimeout:
            return None

        text = await page.locator("body").inner_text()
        return {
            "mail_addr": _extract_after_label(text, r"Mailing Address"),
            "mail_city": _extract_after_label(text, r"Mailing City"),
            "mail_state": _extract_after_label(text, r"Mailing State") or "DC",
            "mail_zip": _extract_after_label(text, r"Mailing Zip"),
            "legal": _extract_after_label(text, r"Legal Description"),
            "assessed_value": _extract_after_label(text, r"(Total )?Assessed Value"),
            "tax_due": _extract_after_label(text, r"(Total )?(Tax )?(Amount )?Due|Balance Due"),
        }


def _extract_after_label(page_text: str, label_pattern: str) -> str:
    """MyTax detail pages typically render as label/value pairs stacked
    in the DOM (e.g. 'Legal Description\\nLOT 45 SQUARE 123'). This grabs
    the text on the line(s) immediately following a matching label."""
    m = re.search(label_pattern + r"\s*[:\n]\s*(.+)", page_text, re.IGNORECASE)
    if not m:
        return ""
    return m.group(m.lastindex).strip().splitlines()[0].strip()


# ---------------------------------------------------------------------------
# Batch enrichment entry point -- called from fetch.py for any expired-
# listing record that the bulk parcel file (parcel_data.py) couldn't
# match, so owner/mailing/legal/debt info still gets filled in via the
# live MyTax.DC.gov search instead of being left blank.
# ---------------------------------------------------------------------------
async def enrich_records_via_mytax(records: list[dict], headless: bool = True) -> None:
    """Mutates `records` in place. Only touches records already flagged
    `no_parcel_match` -- records the bulk file already enriched are left
    alone to avoid an unnecessary live lookup per run."""
    targets = [r for r in records if "no_parcel_match" in r.get("flags", [])]
    if not targets:
        return

    log.info("Enriching %d expired-listing record(s) via live MyTax.DC.gov search", len(targets))
    async with MyTaxOwnerLookup(headless=headless) as lookup:
        for rec in targets:
            address = rec.get("prop_address", "")
            if not address:
                continue
            try:
                result = await lookup.lookup_address(address)
            except Exception as exc:  # noqa: BLE001
                log.error("MyTax enrichment failed for %s, leaving record as-is: %s",
                          address, exc)
                continue

            if not result.matched:
                continue

            rec["owner"] = result.owner or rec.get("owner", "")
            rec["mail_address"] = result.mail_addr or rec.get("mail_address", "")
            rec["mail_city"] = result.mail_city or rec.get("mail_city", "")
            rec["mail_state"] = result.mail_state or rec.get("mail_state", "")
            rec["mail_zip"] = result.mail_zip or rec.get("mail_zip", "")
            rec["legal"] = result.legal or rec.get("legal", "")
            if result.tax_due is not None:
                rec["amount"] = result.tax_due
            rec["clerk_url"] = result.detail_url or rec.get("clerk_url", "")

            flags = [f for f in rec.get("flags", []) if f not in ("no_parcel_match", "expiration_unconfirmed")]
            flags.append("confirmed_via_mytax")
            rec["flags"] = flags
            rec["score"] = min(100, rec.get("score", 40) + 15)
