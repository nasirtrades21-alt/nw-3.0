"""Small shared helpers used across the pipeline.

Design principle: NOTHING in here should ever raise out of a per-record
loop. Anything that can fail on messy real-world data (missing fields,
weird encodings, malformed addresses) degrades to None/"" instead of
crashing the whole run.
"""
from __future__ import annotations

import functools
import logging
import re
import time
from typing import Callable, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


log = get_logger("pipeline")


def retry(max_attempts: int = 3, backoff_seconds: float = 4.0, exceptions=(Exception,)):
    """Retry decorator with linear backoff. Logs and re-raises after the
    final attempt so the caller can decide whether to skip the record."""

    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001
                    last_exc = exc
                    log.warning(
                        "%s failed (attempt %d/%d): %s",
                        fn.__name__, attempt, max_attempts, exc,
                    )
                    if attempt < max_attempts:
                        time.sleep(backoff_seconds * attempt)
            log.error("%s giving up after %d attempts", fn.__name__, max_attempts)
            raise last_exc

        return wrapper

    return decorator


async def async_retry(fn, *args, max_attempts=3, backoff_seconds=4.0, **kwargs):
    """Async equivalent of `retry`, used for Playwright calls."""
    import asyncio

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "%s failed (attempt %d/%d): %s",
                getattr(fn, "__name__", "async_fn"), attempt, max_attempts, exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(backoff_seconds * attempt)
    log.error("async op giving up after %d attempts", max_attempts)
    raise last_exc


_ADDR_JUNK_RE = re.compile(r"[^\w\s]")
_UNIT_RE = re.compile(r"\b(apt|unit|ste|suite|#)\b.*$", re.IGNORECASE)


def normalize_address(addr: Optional[str]) -> str:
    """Normalize a street address for fuzzy matching between the listing
    portal and the parcel database. Strips unit/apt suffixes, punctuation,
    and common abbreviations."""
    if not addr:
        return ""
    a = addr.strip().upper()
    a = _UNIT_RE.sub("", a)
    a = _ADDR_JUNK_RE.sub(" ", a)
    replacements = {
        r"\bSTREET\b": "ST", r"\bAVENUE\b": "AVE", r"\bBOULEVARD\b": "BLVD",
        r"\bDRIVE\b": "DR", r"\bPLACE\b": "PL", r"\bCOURT\b": "CT",
        r"\bLANE\b": "LN", r"\bROAD\b": "RD", r"\bTERRACE\b": "TER",
        r"\bNORTHWEST\b": "NW", r"\bNORTHEAST\b": "NE",
        r"\bSOUTHWEST\b": "SW", r"\bSOUTHEAST\b": "SE",
        r"\bSQUARE\b": "SQ",
    }
    for pattern, repl in replacements.items():
        a = re.sub(pattern, repl, a)
    a = re.sub(r"\s+", " ", a).strip()
    return a


def split_owner_name(owner: str) -> tuple[str, str]:
    """Best-effort split of an assessor 'owner' field into (first, last).
    Handles 'LAST FIRST', 'LAST, FIRST', 'FIRST LAST', and multi-owner
    strings like 'SMITH JOHN & JANE' (takes the first named individual).
    Falls back to putting everything in `last` if it can't confidently
    split, since that's safer for mail-merge than guessing wrong."""
    if not owner:
        return "", ""
    o = owner.strip()
    o = re.split(r"\s*&\s*|\bAND\b", o, maxsplit=1)[0].strip()

    if "," in o:
        last, _, first = o.partition(",")
        return first.strip().title(), last.strip().title()

    parts = o.split()
    if len(parts) == 1:
        return "", parts[0].title()
    if len(parts) == 2:
        # Ambiguous "LAST FIRST" vs "FIRST LAST" -- assessor rolls are
        # almost always "LAST FIRST", which is what we assume here.
        return parts[1].title(), parts[0].title()
    # 3+ tokens: assume "LAST FIRST MIDDLE"
    return parts[1].title(), parts[0].title()


def name_variants(first: str, last: str) -> list[str]:
    """Generate the lookup variants requested: 'FIRST LAST', 'LAST FIRST',
    'LAST, FIRST'."""
    first, last = (first or "").strip().upper(), (last or "").strip().upper()
    if not first and not last:
        return []
    variants = {
        f"{first} {last}".strip(),
        f"{last} {first}".strip(),
        f"{last}, {first}".strip(", "),
    }
    return [v for v in variants if v]


def safe_float(value, default=None):
    try:
        if value in (None, "", "N/A"):
            return default
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return default


def safe_int(value, default=None):
    f = safe_float(value, None)
    return int(f) if f is not None else default
