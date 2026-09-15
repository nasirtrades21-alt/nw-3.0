"""
Loads the DC OTR bulk parcel extract and builds lookup indexes.

See config.py for the important caveat: the DBF URL is NOT published on
the "Real Property Tax Database Search" landing page itself (that page
only links to the interactive MyTax.DC.gov search). You must set
PARCEL_DBF_URL (env var or config.py) to the actual bulk-extract file
you're authorized to use, e.g. a "Computer Assisted Mass Appraisal" or
"Integrated Tax System public extract" file from opendata.dc.gov.

Everything here is written generically against the column-name aliases
in config.PARCEL_COLUMN_ALIASES, and degrades gracefully (empty index)
if the file can't be fetched -- a bad appraiser download should reduce
enrichment quality, not crash the whole pipeline.
"""
from __future__ import annotations

import os
import zipfile
from typing import Dict, List, Optional

import requests
from dbfread import DBF

import config
from utils import get_logger, name_variants, normalize_address, retry, safe_float, split_owner_name

log = get_logger("parcel_data")


class ParcelRecord:
    __slots__ = (
        "owner", "first_name", "last_name", "site_addr", "site_city",
        "site_zip", "mail_addr", "mail_city", "mail_state", "mail_zip",
        "legal", "debt", "parcel_id", "sale_date", "sale_price",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}


class ParcelDatabase:
    """Owner-name and site-address indexes over the bulk parcel file."""

    def __init__(self):
        self.by_address: Dict[str, ParcelRecord] = {}
        self.by_owner_variant: Dict[str, List[ParcelRecord]] = {}
        self.loaded = False

    # -- download -----------------------------------------------------
    @retry(max_attempts=config.MAX_RETRIES, backoff_seconds=config.RETRY_BACKOFF_SECONDS,
           exceptions=(requests.RequestException, IOError))
    def _download(self, url: str, dest_path: str) -> str:
        log.info("Downloading parcel bulk file from %s", url)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        headers = {"User-Agent": config.USER_AGENT}
        with requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        return dest_path

    def _resolve_dbf_path(self, downloaded_path: str) -> Optional[str]:
        """If the download was a .zip, extract and return the first .dbf
        inside it. Otherwise assume the file itself is a .dbf."""
        if zipfile.is_zipfile(downloaded_path):
            extract_dir = downloaded_path + "_extracted"
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(downloaded_path) as zf:
                zf.extractall(extract_dir)
                dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
            if not dbf_names:
                log.error("Zip file downloaded but contained no .dbf")
                return None
            return os.path.join(extract_dir, dbf_names[0])
        return downloaded_path

    @staticmethod
    def _first_present(row: dict, aliases: List[str]):
        for alias in aliases:
            if alias in row and row[alias] not in (None, ""):
                return row[alias]
            # dbfread field names are sometimes lowercase/mixed case
            for key in row:
                if key.upper() == alias.upper() and row[key] not in (None, ""):
                    return row[key]
        return None

    def load(self, url: Optional[str] = None) -> "ParcelDatabase":
        url = url or config.PARCEL_DBF_URL
        if not url:
            log.error(
                "PARCEL_DBF_URL is not configured -- skipping parcel "
                "enrichment. See config.py for how to source the DC OTR "
                "bulk extract."
            )
            return self

        try:
            downloaded = self._download(url, config.PARCEL_CACHE_PATH)
            dbf_path = self._resolve_dbf_path(downloaded)
            if not dbf_path:
                return self
            self._index(dbf_path)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to load parcel database, continuing without it: %s", exc)
        return self

    # -- indexing -------------------------------------------------------
    def _index(self, dbf_path: str) -> None:
        table = DBF(dbf_path, load=False, encoding="latin1", ignore_missing_memofile=True)
        count = 0
        for row in table:
            try:
                rec = self._row_to_record(row)
            except Exception as exc:  # noqa: BLE001
                log.debug("Skipping malformed parcel row: %s", exc)
                continue
            if rec is None:
                continue

            if rec.site_addr:
                self.by_address[normalize_address(rec.site_addr)] = rec

            for variant in name_variants(rec.first_name, rec.last_name):
                self.by_owner_variant.setdefault(variant, []).append(rec)

            count += 1
            if count % 25000 == 0:
                log.info("Indexed %d parcel rows...", count)

        self.loaded = count > 0
        log.info("Parcel index built: %d records, %d unique addresses, %d owner-name variants",
                  count, len(self.by_address), len(self.by_owner_variant))

    def _row_to_record(self, row: dict) -> Optional[ParcelRecord]:
        aliases = config.PARCEL_COLUMN_ALIASES
        owner = self._first_present(row, aliases["owner"])
        if not owner:
            return None
        first, last = split_owner_name(str(owner))
        return ParcelRecord(
            owner=str(owner).strip(),
            first_name=first,
            last_name=last,
            site_addr=self._first_present(row, aliases["site_addr"]),
            site_city=self._first_present(row, aliases["site_city"]) or "Washington",
            site_zip=self._first_present(row, aliases["site_zip"]),
            mail_addr=self._first_present(row, aliases["mail_addr"]),
            mail_city=self._first_present(row, aliases["mail_city"]),
            mail_state=self._first_present(row, aliases["mail_state"]) or "DC",
            mail_zip=self._first_present(row, aliases["mail_zip"]),
            legal=self._first_present(row, aliases["legal"]),
            debt=safe_float(self._first_present(row, aliases["debt"])),
            parcel_id=self._first_present(row, aliases["parcel_id"]),
            sale_date=self._first_present(row, aliases["sale_date"]),
            sale_price=safe_float(self._first_present(row, aliases["sale_price"])),
        )

    # -- lookups ----------------------------------------------------------
    def match_by_address(self, address: str) -> Optional[ParcelRecord]:
        if not address:
            return None
        return self.by_address.get(normalize_address(address))

    def match_by_owner(self, first: str, last: str) -> Optional[ParcelRecord]:
        for variant in name_variants(first, last):
            hits = self.by_owner_variant.get(variant)
            if hits:
                return hits[0]
        return None
