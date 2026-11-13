"""Small shared helpers: time, text cleaning, URL normalisation, config loading."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import yaml
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SITE_DIR = ROOT / "site"
DATA_DIR = SITE_DIR / "data"
STATE_FILE = ROOT / "state" / "state.json"

log = logging.getLogger("news")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime | None, assume_tz: str = "Asia/Colombo") -> datetime | None:
    """Make a datetime timezone-aware (UTC). Naive datetimes are assumed local (Sri Lanka)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(assume_tz))
    return dt.astimezone(timezone.utc)


def parse_date(value, assume_tz: str = "Asia/Colombo") -> datetime | None:
    """Parse almost any date string feeds and pages throw at us."""
    if not value:
        return None
    if isinstance(value, datetime):
        return to_utc(value, assume_tz)
    s = str(value).strip()
    # Sunday Times style: "14 December 2019 @ 7:23 pm"
    s = s.replace(" @ ", " ")
    try:
        return to_utc(dateparser.parse(s, fuzzy=True), assume_tz)
    except (ValueError, OverflowError, TypeError):
        return None


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value: str | None, limit: int | None = None) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    if not value:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = _WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut.rstrip(",;:.") + "…"
    return text


_TRACKING_PARAMS = {"fbclid", "gclid", "ref", "mc_cid", "mc_eid", "ocid"}


def normalize_url(url: str) -> str:
    """Canonical form for duplicate detection (drops tracking params, fragments, trailing slash)."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    netloc = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    t = clean_text(title).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return _WS_RE.sub(" ", t).strip()


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")
