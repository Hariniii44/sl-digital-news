"""Step 2 — keep only new items from the time window, pre-filter general news, remove duplicates."""

from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher

from .collect import Item
from .util import log, normalize_title, normalize_url, short_hash

# Preferred version when the same story arrives through several routes.
_VIA_RANK = {"submission": 0, "rss": 1, "google_news": 2}


def item_key(item: Item) -> str:
    return short_hash(normalize_url(item.url))


def filter_window(items: list[Item], since: datetime, seen: set[str],
                  submitted_seen: set[str]) -> list[Item]:
    kept = []
    for it in items:
        key = item_key(it)
        if it.via == "submission":
            if key not in submitted_seen:
                kept.append(it)
            continue
        if key in seen:
            continue
        if it.published is not None and it.published < since:
            continue
        # Items without a publish date are kept once (the 'seen' list stops repeats).
        kept.append(it)
    log.info("Window filter: %d → %d items", len(items), len(kept))
    return kept


def prefilter_general(items: list[Item], keywords: list[str]) -> list[Item]:
    kws = [k.lower() for k in keywords]
    kept = []
    for it in items:
        if not it.general:
            kept.append(it)
            continue
        hay = f" {it.title} {it.description} ".lower()
        if any(k in hay for k in kws):
            kept.append(it)
    log.info("General-news pre-filter: %d → %d items", len(items), len(kept))
    return kept


def _same_outlet(a: str, b: str) -> bool:
    na, nb = normalize_title(a).replace(" ", ""), normalize_title(b).replace(" ", "")
    return bool(na and nb) and (na in nb or nb in na)


def _merge(keep: Item, drop: Item) -> None:
    if not _same_outlet(keep.outlet, drop.outlet):
        if all(not _same_outlet(x["outlet"], drop.outlet) for x in keep.also_in):
            keep.also_in.append({"outlet": drop.outlet, "url": drop.url})
    for extra in drop.also_in:
        if not _same_outlet(keep.outlet, extra["outlet"]) and extra not in keep.also_in:
            keep.also_in.append(extra)
    if not keep.published and drop.published:
        keep.published = drop.published
    if len(drop.description) > len(keep.description):
        keep.description = drop.description
    if not keep.author and drop.author:
        keep.author = drop.author


def dedupe(items: list[Item], title_threshold: float = 0.88) -> list[Item]:
    """Merge items that share a URL or have (nearly) the same headline."""
    ordered = sorted(items, key=lambda i: _VIA_RANK.get(i.via, 9))

    by_url: dict[str, Item] = {}
    for it in ordered:
        key = normalize_url(it.url)
        if key in by_url:
            _merge(by_url[key], it)
        else:
            by_url[key] = it

    unique: list[Item] = []
    norm_titles: list[str] = []
    for it in by_url.values():
        nt = normalize_title(it.title)
        match = None
        for idx, other in enumerate(norm_titles):
            if not nt or not other:
                continue
            if nt == other:
                match = idx
                break
            sm = SequenceMatcher(None, nt, other)
            if sm.real_quick_ratio() >= title_threshold and sm.quick_ratio() >= title_threshold \
                    and sm.ratio() >= title_threshold:
                match = idx
                break
        if match is None:
            unique.append(it)
            norm_titles.append(nt)
        else:
            _merge(unique[match], it)
    log.info("Dedupe: %d → %d items", len(items), len(unique))
    return unique
