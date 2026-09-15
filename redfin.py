"""
Playwright-based scraper for the Redfin neighborhood search results.

HOW 'LISTING REMOVED' DETECTION WORKS
--------------------------------------
Redfin does not expose a public "removed/withdrawn listings" feed. The
standard, portal-agnostic way to build this lead type is a snapshot diff:

  1. Each run, scrape every currently-active listing in the neighborhood
     filter (address, url, price, beds, baths, sqft, lot size, listing
     description, and a compact listing-history table if present).
  2. Persist that snapshot (state.py / data/listings_seen.json).
  3. Compare to the PREVIOUS run's snapshot. Any listing_id that was
     present before but is missing now -- and was first seen within
     LOOKBACK_DAYS -- is a "Listing Removed" lead. We use the *last
     known* scraped details for that listing (since the live page is
     gone by definition), which is why step 1 captures full detail data
     up front rather than only a thin summary row.

NOTE ON SELECTORS: Redfin's DOM/CSS classes change frequently and the
site may serve a bot-check interstitial to headless browsers. The
selectors below target the semantically stable pieces (JSON-LD, data-rf-*
test ids, and visible text landmarks) where possible, with CSS fallbacks.
Re-verify against the live page (Playwright's codegen / inspector is the
fastest way) before relying on this in production, and consider adding
`playwright-stealth` or a residential proxy pool if you hit persistent
CAPTCHAs -- neither is included here since that's an infra/ToS decision,
not a code one.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

import config
from utils import async_retry, get_logger, safe_float, safe_int

log = get_logger("redfin")

BASE = "https://www.redfin.com"


class Listing:
    """One scraped listing snapshot."""

    def __init__(self, **kwargs):
        self.listing_id: str = kwargs.get("listing_id", "")
        self.url: str = kwargs.get("url", "")
        self.address: str = kwargs.get("address", "")
        self.city: str = kwargs.get("city", "Washington")
        self.state: str = kwargs.get("state", "DC")
        self.zip_code: str = kwargs.get("zip_code", "")
        self.price: Optional[float] = kwargs.get("price")
        self.beds: Optional[float] = kwargs.get("beds")
        self.baths: Optional[float] = kwargs.get("baths")
        self.living_sqft: Optional[int] = kwargs.get("living_sqft")
        self.lot_sqft: Optional[int] = kwargs.get("lot_sqft")
        self.description: str = kwargs.get("description", "")
        self.listing_history: List[dict] = kwargs.get("listing_history", [])
        self.first_seen: str = kwargs.get("first_seen") or _now_iso()
        self.last_seen: str = kwargs.get("last_seen") or _now_iso()

    def to_dict(self) -> dict:
        return {
            "listing_id": self.listing_id, "url": self.url, "address": self.address,
            "city": self.city, "state": self.state, "zip_code": self.zip_code,
            "price": self.price, "beds": self.beds, "baths": self.baths,
            "living_sqft": self.living_sqft, "lot_sqft": self.lot_sqft,
            "description": self.description, "listing_history": self.listing_history,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        return cls(**d)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _listing_id_from_url(url: str) -> str:
    m = re.search(r"/home/(\d+)", url)
    if m:
        return m.group(1)
    return re.sub(r"\W+", "-", url).strip("-")[-64:]


class RedfinScraper:
    def __init__(self, portal_url: str = config.PORTAL_URL, headless: bool = True):
        self.portal_url = portal_url
        self.headless = headless

    async def scrape_active_listings(self) -> Dict[str, Listing]:
        """Returns {listing_id: Listing} for every home currently showing
        in the neighborhood filter."""
        results: Dict[str, Listing] = {}
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                user_agent=config.USER_AGENT,
                viewport={"width": 1400, "height": 1000},
            )
            page = await context.new_page()
            try:
                urls = await async_retry(self._collect_search_result_urls, page,
                                          max_attempts=config.MAX_RETRIES,
                                          backoff_seconds=config.RETRY_BACKOFF_SECONDS)
                log.info("Found %d listing URLs in search results", len(urls))

                for url in urls:
                    try:
                        listing = await async_retry(
                            self._scrape_listing_detail, context, url,
                            max_attempts=config.MAX_RETRIES,
                            backoff_seconds=config.RETRY_BACKOFF_SECONDS,
                        )
                        if listing:
                            results[listing.listing_id] = listing
                    except Exception as exc:  # noqa: BLE001
                        log.error("Giving up on listing %s: %s", url, exc)
                        continue
            finally:
                await context.close()
                await browser.close()
        return results

    # -- search results page ------------------------------------------------
    async def _collect_search_result_urls(self, page: Page) -> List[str]:
        urls: List[str] = []
        next_url = self.portal_url
        page_num = 1
        while next_url:
            log.info("Loading search results page %d: %s", page_num, next_url)
            await page.goto(next_url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector("[data-rf-test-id='property-card']",
                                              timeout=15000)
            except PWTimeout:
                # Fall back to a generic anchor-based selector; Redfin
                # sometimes renders cards under different test ids.
                log.warning("Primary card selector not found, trying fallback")
                await page.wait_for_selector("a[href*='/home/']", timeout=15000)

            hrefs = await page.eval_on_selector_all(
                "a[href*='/home/']",
                "els => els.map(e => e.getAttribute('href'))",
            )
            for href in hrefs:
                if not href:
                    continue
                full = urljoin(BASE, href.split("?")[0])
                if full not in urls:
                    urls.append(full)

            next_url = await self._find_next_page_url(page)
            page_num += 1
            if page_num > 60:  # sanity cap so a pagination bug can't loop forever
                log.warning("Hit pagination safety cap at page %d", page_num)
                break

        return urls

    async def _find_next_page_url(self, page: Page) -> Optional[str]:
        try:
            next_link = await page.query_selector(
                "a[data-rf-test-id='react-data-paginate-next-page']"
            )
            if not next_link:
                next_link = await page.query_selector("a.PageArrow.next")
            if not next_link:
                return None
            disabled = await next_link.get_attribute("aria-disabled")
            if disabled == "true":
                return None
            href = await next_link.get_attribute("href")
            return urljoin(BASE, href) if href else None
        except Exception:  # noqa: BLE001
            return None

    # -- listing detail page --------------------------------------------------
    async def _scrape_listing_detail(self, context, url: str) -> Optional[Listing]:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1000)

            data = await self._extract_json_ld(page)
            address, city, state, zip_code = self._parse_address(page_url=url, jsonld=data)
            price = await self._extract_price(page)
            beds, baths = await self._extract_beds_baths(page)
            living_sqft, lot_sqft = await self._extract_sqft(page)
            description = await self._extract_description(page)
            history = await self._extract_listing_history(page)

            listing_id = _listing_id_from_url(url)
            return Listing(
                listing_id=listing_id, url=url, address=address, city=city,
                state=state, zip_code=zip_code, price=price, beds=beds,
                baths=baths, living_sqft=living_sqft, lot_sqft=lot_sqft,
                description=description, listing_history=history,
            )
        finally:
            await page.close()

    async def _extract_json_ld(self, page: Page) -> dict:
        try:
            scripts = await page.eval_on_selector_all(
                "script[type='application/ld+json']",
                "els => els.map(e => e.textContent)",
            )
            for raw in scripts:
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                candidates = parsed if isinstance(parsed, list) else [parsed]
                for c in candidates:
                    if isinstance(c, dict) and c.get("@type") in ("SingleFamilyResidence", "House", "Product", "Residence"):
                        return c
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _parse_address(self, page_url: str, jsonld: dict):
        addr = jsonld.get("address") if isinstance(jsonld, dict) else None
        if isinstance(addr, dict):
            street = addr.get("streetAddress", "")
            city = addr.get("addressLocality", "Washington")
            state = addr.get("addressRegion", "DC")
            zip_code = addr.get("postalCode", "")
            if street:
                return street, city, state, zip_code
        # Fallback: parse from the URL slug, e.g. /DC/Washington/123-Main-St/home/12345
        m = re.search(r"/DC/([^/]+)/([^/]+)/home/", page_url)
        if m:
            city = m.group(1).replace("-", " ")
            street = m.group(2).replace("-", " ")
            return street, city, "DC", ""
        return "", "Washington", "DC", ""

    async def _extract_price(self, page: Page) -> Optional[float]:
        for selector in ("[data-rf-test-id='abp-price'] .statsValue",
                          ".price .statsValue", "[data-rf-test-id='abp-price']"):
            try:
                el = await page.query_selector(selector)
                if el:
                    text = await el.inner_text()
                    return safe_float(text)
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _extract_beds_baths(self, page: Page):
        beds = baths = None
        try:
            beds_el = await page.query_selector("[data-rf-test-id='abp-beds'] .statsValue")
            baths_el = await page.query_selector("[data-rf-test-id='abp-baths'] .statsValue")
            if beds_el:
                beds = safe_float(await beds_el.inner_text())
            if baths_el:
                baths = safe_float(await baths_el.inner_text())
        except Exception:  # noqa: BLE001
            pass
        return beds, baths

    async def _extract_sqft(self, page: Page):
        living_sqft = lot_sqft = None
        try:
            sqft_el = await page.query_selector("[data-rf-test-id='abp-sqFt'] .statsValue")
            if sqft_el:
                living_sqft = safe_int(await sqft_el.inner_text())
        except Exception:  # noqa: BLE001
            pass
        try:
            rows = await page.query_selector_all(".keyDetails-row, .keyDetail")
            for row in rows:
                text = (await row.inner_text()).lower()
                if "lot size" in text:
                    lot_sqft = safe_int(re.sub(r"[^\d]", "", text.split("lot size")[-1]))
        except Exception:  # noqa: BLE001
            pass
        return living_sqft, lot_sqft

    async def _extract_description(self, page: Page) -> str:
        for selector in ("[data-rf-test-id='marketing-remarks-scroll'] p",
                          "#marketing-remarks-scroll p", ".remarks"):
            try:
                el = await page.query_selector(selector)
                if el:
                    return (await el.inner_text()).strip()
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def _extract_listing_history(self, page: Page) -> List[dict]:
        history = []
        try:
            rows = await page.query_selector_all(
                "#property-history-scroll table tbody tr, .PropertyHistoryEventRow"
            )
            for row in rows:
                try:
                    cells = await row.query_selector_all("td, .col")
                    values = [(await c.inner_text()).strip() for c in cells]
                    if values:
                        history.append({
                            "date": values[0] if len(values) > 0 else "",
                            "event": values[1] if len(values) > 1 else "",
                            "price": values[2] if len(values) > 2 else "",
                        })
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return history
