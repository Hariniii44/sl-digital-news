"""Step 4 — for relevant items: resolve Google News links, read the article page for
author, publish time and text (the text is only used to write the summary)."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import requests
import trafilatura

from .collect import Item
from .util import USER_AGENT, clean_text, domain_of, log, parse_date

# Pages that need a login or block bots — don't try to read them.
_NO_FETCH_DOMAINS = ("linkedin.com", "facebook.com", "instagram.com", "x.com",
                     "twitter.com", "tiktok.com", "news.google.com")

_BAD_AUTHORS = re.compile(r"^(admin|administrator|editor|web ?desk|online desk|news desk|"
                          r"staff|author|guest|unknown|none|null)$", re.I)


def decode_google_news_links(items: list[Item]) -> None:
    """Replace news.google.com redirect links with the real article URL where possible."""
    targets = [it for it in items if "news.google.com" in it.url]
    if not targets:
        return
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        log.warning("googlenewsdecoder not installed; keeping Google News links")
        return

    ok = failed_in_a_row = 0
    for it in targets:
        if failed_in_a_row >= 5:
            log.warning("Google News link decoding keeps failing; keeping remaining links as-is")
            break
        try:
            res = gnewsdecoder(it.url, interval=1)
            if res.get("status") and res.get("decoded_url"):
                it.also_in = [x for x in it.also_in if x.get("url") != res["decoded_url"]]
                it.url = res["decoded_url"]
                ok += 1
                failed_in_a_row = 0
                continue
        except Exception as exc:  # noqa: BLE001
            log.debug("decode failed for %s: %s", it.url, exc)
        failed_in_a_row += 1
    log.info("Google News links decoded: %d of %d", ok, len(targets))


def _clean_author(author: str | None, outlet: str) -> str:
    a = clean_text(author or "")
    a = re.sub(r"^by\s+", "", a, flags=re.I).strip(" ;,|-")
    if not a or len(a) > 80 or "http" in a or _BAD_AUTHORS.match(a):
        return ""
    if a.lower().replace(" ", "") in outlet.lower().replace(" ", ""):
        return ""
    # trafilatura joins multiple authors with ';'
    return ", ".join(p.strip() for p in a.split(";") if p.strip())


def fetch_article(item: Item, timeout: int = 15) -> None:
    if any(d in domain_of(item.url) for d in _NO_FETCH_DOMAINS):
        return
    try:
        r = requests.get(item.url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
        doc = trafilatura.bare_extraction(r.text, url=item.url, with_metadata=True,
                                          favor_precision=True)
        if doc is not None and not isinstance(doc, dict):
            doc = doc.as_dict()
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not read article %s: %s", item.url, exc)
        return
    if not doc:
        return
    if not item.author:
        item.author = doc.get("author") or ""
    if doc.get("text") and len(doc["text"]) > len(item.text):
        item.text = doc["text"]
    if item.published is None and doc.get("date"):
        item.published = parse_date(doc["date"])
    if item.via == "submission" and (item.title == item.url or not item.title) and doc.get("title"):
        item.title = clean_text(doc["title"])


def enrich(items: list[Item], workers: int = 8, timeout: int = 15) -> None:
    decode_google_news_links(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda it: fetch_article(it, timeout), items))
    for it in items:
        it.author = _clean_author(it.author, it.outlet)
    got_text = sum(1 for it in items if len(it.text) > 200)
    log.info("Article pages read: %d of %d have full text", got_text, len(items))
