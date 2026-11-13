"""Step 1 — collect candidate news items from RSS feeds, Google News and team submissions."""

from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests

from .util import USER_AGENT, clean_text, log, parse_date, utcnow

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-LK&gl=LK&ceid=LK:en"


@dataclass
class Item:
    title: str
    url: str
    outlet: str
    via: str                       # "rss" | "google_news" | "submission"
    source: str                    # name of the configured source that found it
    published: datetime | None = None
    description: str = ""
    general: bool = False          # came from a general-news outlet (pre-filter applies)
    author: str = ""
    text: str = ""                 # article text (filled in by enrich step)
    also_in: list = field(default_factory=list)   # [{"outlet":..., "url":...}]
    score: int = 0
    category: str = ""
    summary: str = ""
    ai: bool = False               # True if Gemini produced the category/summary
    time_estimated: bool = False   # True if we couldn't find a real publish time


# ── Low-level fetching ───────────────────────────────────────────────────────

def _get(url: str, timeout: int = 20) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout=timeout)
    r.raise_for_status()
    return r


def _parse_feed(url: str):
    resp = _get(url)
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"not a valid feed ({type(feed.bozo_exception).__name__})")
    if not feed.entries and not feed.feed.get("title"):
        raise ValueError("response is not a feed (blocked or empty page?)")
    return feed


def _entry_published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=ZoneInfo("UTC"))
    for key in ("published", "updated", "pubDate"):
        if entry.get(key):
            dt = parse_date(entry.get(key))
            if dt:
                return dt
    return None


def _entry_author(entry) -> str:
    author = entry.get("author") or ""
    if not author and entry.get("authors"):
        author = ", ".join(a.get("name", "") for a in entry["authors"] if a.get("name"))
    return clean_text(author)


# ── Source types ─────────────────────────────────────────────────────────────

def fetch_rss(url: str, outlet: str, source: str, general: bool = False) -> list[Item]:
    feed = _parse_feed(url)
    items = []
    for e in feed.entries:
        link = (e.get("link") or "").strip()
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        desc = e.get("summary") or e.get("description") or ""
        if not desc and e.get("content"):
            desc = e["content"][0].get("value", "")
        items.append(Item(
            title=title, url=link, outlet=outlet, via="rss", source=source,
            published=_entry_published(e), description=clean_text(desc, 600),
            general=general, author=_entry_author(e),
        ))
    return items


def fetch_google_news(query: str, source: str, outlet: str | None = None) -> list[Item]:
    """Google News search, Sri Lanka edition, limited to the last day."""
    url = GOOGLE_NEWS_RSS.format(q=quote_plus(f"{query} when:1d"))
    feed = _parse_feed(url)
    items = []
    for e in feed.entries:
        src = e.get("source") or {}
        src_title = clean_text(src.get("title")) if hasattr(src, "get") else ""
        title = clean_text(e.get("title"))
        # Google News titles look like "Headline - Outlet"; strip the outlet suffix.
        if src_title and title.endswith(f" - {src_title}"):
            title = title[: -len(f" - {src_title}")].strip()
        if not title or not e.get("link"):
            continue
        items.append(Item(
            title=title, url=e["link"], outlet=outlet or src_title or "Google News",
            via="google_news", source=source, published=_entry_published(e),
        ))
    return items


def sunday_times_feed_url(now: datetime) -> str:
    """The Sunday Times Business Times feed lives under a dated folder for each Sunday edition."""
    today = now.astimezone(ZoneInfo("Asia/Colombo")).date()
    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    return f"https://www.sundaytimes.lk/{last_sunday:%y%m%d}/business-times/feed/business_times.xml"


def fetch_submissions(csv_url: str) -> list[Item]:
    """Links submitted by the team through a Google Form (responses sheet published as CSV).

    Recognised columns (case-insensitive, partial match): link/url, title/headline,
    source/page/outlet, text/post/content/notes, timestamp.
    """
    resp = _get(csv_url)
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))

    headers = list(rows[0].keys()) if rows else []

    def find(names: tuple[str, ...], exclude: set[str]) -> str | None:
        for h in headers:
            if h and h not in exclude and any(n in h.lower() for n in names):
                return h
        return None

    link_col = find(("link", "url"), set())
    used = {link_col} if link_col else set()
    title_col = find(("title", "headline"), used)
    used |= {title_col}
    source_col = find(("source", "page", "outlet", "platform"), used)
    used |= {source_col}
    text_col = find(("text", "content", "note", "description", "post"), used)
    time_col = find(("timestamp", "date"), used)

    def col(row: dict, name: str | None) -> str:
        return (row.get(name) or "").strip() if name else ""

    items = []
    for row in rows:
        link = col(row, link_col)
        if not link.startswith("http"):
            continue
        title = col(row, title_col)
        text = col(row, text_col)
        outlet = col(row, source_col) or "Submitted by team"
        items.append(Item(
            title=title or clean_text(text, 120) or link,
            url=link, outlet=outlet, via="submission", source="Team submissions",
            published=parse_date(col(row, time_col)),
            description=clean_text(text, 600), text=clean_text(text, 4000),
        ))
    return items


# ── Orchestration ────────────────────────────────────────────────────────────

def collect_all(sources_cfg: dict, now: datetime, submissions_csv_url: str = "") -> tuple[list[Item], list[dict]]:
    """Fetch every configured source. Returns (items, source_health)."""
    items: list[Item] = []
    health: list[dict] = []

    def run(name: str, kind: str, fn, *args, **kwargs) -> list[Item] | None:
        started = time.time()
        try:
            got = fn(*args, **kwargs)
            health.append({"name": name, "type": kind, "ok": True, "count": len(got),
                           "seconds": round(time.time() - started, 1)})
            return got
        except Exception as exc:  # noqa: BLE001 — one bad source must never stop the run
            log.warning("Source failed: %s (%s): %s", name, kind, exc)
            health.append({"name": name, "type": kind, "ok": False, "count": 0,
                           "error": str(exc)[:200]})
            return None

    for src in sources_cfg.get("outlets", []):
        if src.get("enabled", True) is False:
            continue
        name, kind = src["name"], src.get("type", "rss")
        general = bool(src.get("general"))
        got = None
        if kind == "rss":
            got = run(name, "rss", fetch_rss, src["url"], name, name, general)
        elif kind == "sundaytimes":
            got = run(name, "rss", fetch_rss, sunday_times_feed_url(now), name, name, general)
        elif kind == "google_news":
            got = run(name, "google_news", fetch_google_news, src["query"], name, name)
        else:
            log.warning("Unknown source type %r for %s", kind, name)
            continue

        if not got and src.get("fallback_query"):
            log.info("Using Google News fallback for %s", name)
            got = run(f"{name} (Google News fallback)", "google_news",
                      fetch_google_news, src["fallback_query"], name, name)
        items.extend(got or [])
        time.sleep(0.5)

    for q in sources_cfg.get("topics", []):
        got = run(f"Topic: {q}", "google_news", fetch_google_news, q, f"Topic: {q}")
        items.extend(got or [])
        time.sleep(1.0)  # be gentle with Google News

    if submissions_csv_url:
        got = run("Team submissions", "form", fetch_submissions, submissions_csv_url)
        items.extend(got or [])

    log.info("Collected %d raw items from %d sources", len(items), len(health))
    return items, health
