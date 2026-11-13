"""Daily run: collect → filter → screen → enrich → summarise → publish JSON for the website.

Usage:
    python -m pipeline.run                  # normal daily run
    python -m pipeline.run --check-sources  # only test the sources and print their status
    python -m pipeline.run --no-ai          # run without Gemini (keyword rules only)
    python -m pipeline.run --hours 48       # look back a custom number of hours
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import ai
from .collect import Item, collect_all
from .dedupe import dedupe, filter_window, item_key, prefilter_general
from .enrich import enrich
from .util import DATA_DIR, STATE_FILE, iso, load_yaml, log, parse_date, utcnow

SEEN_RETENTION_DAYS = 10


# ── State (what we've already shown) ─────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_run": None, "seen": {}, "submitted_seen": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


# ── Output ───────────────────────────────────────────────────────────────────

def item_to_json(it: Item) -> dict:
    return {
        "id": item_key(it),
        "title": it.title,
        "url": it.url,
        "outlet": it.outlet,
        "author": it.author,
        "published": iso(it.published),
        "time_estimated": it.time_estimated,
        "category": it.category,
        "summary": it.summary,
        "score": it.score,
        "via": it.via,
        "ai_summary": it.ai,
        "also_in": it.also_in[:6],
    }


def write_outputs(payload: dict, local_date: str) -> None:
    archive_dir = DATA_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    day_file = archive_dir / f"{local_date}.json"

    # A second run on the same day (e.g. triggered manually) adds to that day's file.
    if day_file.exists():
        old = json.loads(day_file.read_text(encoding="utf-8"))
        new_ids = {i["id"] for i in payload["items"]}
        payload["items"] += [i for i in old.get("items", []) if i["id"] not in new_ids]
        payload["items"].sort(key=lambda i: i.get("published") or "", reverse=True)
        payload["window_start"] = min(filter(None, [old.get("window_start"), payload["window_start"]]))
        payload["stats"]["published"] = len(payload["items"])

    text = json.dumps(payload, ensure_ascii=False, indent=1)
    day_file.write_text(text, encoding="utf-8")
    (DATA_DIR / "latest.json").write_text(text, encoding="utf-8")

    dates = sorted((p.stem for p in archive_dir.glob("????-??-??.json")), reverse=True)
    (archive_dir / "index.json").write_text(json.dumps({"dates": dates}, indent=1), encoding="utf-8")
    log.info("Wrote %d items to latest.json and archive/%s.json", len(payload["items"]), local_date)


# ── Main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-sources", action="store_true", help="only test sources and print status")
    ap.add_argument("--no-ai", action="store_true", help="don't call Gemini")
    ap.add_argument("--hours", type=float, help="look back this many hours (ignores last run time)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "trafilatura", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = load_yaml("settings.yaml")
    sources = load_yaml("sources.yaml")
    tz = ZoneInfo(settings.get("timezone", "Asia/Colombo"))
    now = utcnow()
    state = load_state()
    submissions_url = os.environ.get("SUBMISSIONS_CSV_URL", "").strip()

    # 1. Collect
    raw, health = collect_all(sources, now, submissions_url)

    if args.check_sources:
        print(f"\n{'SOURCE':58} {'TYPE':12} {'STATUS':6} ITEMS")
        for h in health:
            status = "OK" if h["ok"] else "FAIL"
            print(f"{h['name'][:58]:58} {h['type']:12} {status:6} {h['count']}"
                  + (f"   ← {h.get('error', '')[:80]}" if not h["ok"] else ""))
        return 0

    if not raw:
        # Probably a network problem. Keep yesterday's edition and don't move the window,
        # so the next run picks up everything. The failed workflow run alerts the maintainer.
        log.error("No source returned any items — not publishing an empty edition.")
        return 1

    # 2. Window + filters + dedupe
    if args.hours:
        since = now - timedelta(hours=args.hours)
    else:
        last = parse_date(state.get("last_run"))
        max_window = timedelta(hours=settings.get("max_window_hours", 36))
        since = last if last and now - last <= max_window else now - timedelta(hours=settings.get("window_hours", 24))
    log.info("Window: %s → %s", since.astimezone(tz), now.astimezone(tz))

    # A 2-hour overlap catches stories that appear in feeds late; the 'seen' list prevents repeats.
    in_window = filter_window(raw, since - timedelta(hours=2), set(state.get("seen", {})),
                              set(state.get("submitted_seen", [])))
    candidates = dedupe(prefilter_general(in_window, settings.get("general_prefilter_keywords", [])))

    # 3. Screen for relevance
    api_key = "" if args.no_ai else os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip() or settings.get("gemini_model", "gemini-3.5-flash-lite")
    gemini = ai.Gemini(api_key, model, settings.get("gemini_min_interval_seconds", 6))
    ai.triage(candidates, gemini, settings)
    min_rel = settings.get("min_relevance", 3)
    relevant = sorted((c for c in candidates if c.score >= min_rel), key=lambda c: -c.score)
    max_items = settings.get("max_items_per_day", 300)
    if len(relevant) > max_items:
        log.warning("Capping %d relevant items to the top %d", len(relevant), max_items)
        relevant = relevant[:max_items]
    log.info("Relevant after screening: %d of %d", len(relevant), len(candidates))

    # 4. Read article pages (author, time, text)
    enrich(relevant, settings.get("fetch_workers", 8), settings.get("fetch_timeout_seconds", 15))
    relevant = dedupe(relevant)  # decoded Google News links may reveal more duplicates
    stale_before = since - timedelta(days=1)
    relevant = [it for it in relevant if it.published is None or it.published >= stale_before]

    # 5. Category + summary
    ai.categorize_and_summarize(relevant, gemini, settings)
    final = [it for it in relevant if it.score >= min_rel]
    for it in final:
        # Form timestamps can be ambiguous (day/month order); trust them only if recent.
        if it.via == "submission" and it.published and abs(now - it.published) > timedelta(days=3):
            it.published = None
        if it.published is None:
            it.published, it.time_estimated = now, True
    final.sort(key=lambda it: it.published, reverse=True)

    # 6. Publish
    payload = {
        "site_title": settings.get("site_title", "News"),
        "generated_at": iso(now),
        "window_start": iso(since),
        "window_end": iso(now),
        "timezone": str(tz),
        "submit_form_url": os.environ.get("SUBMIT_FORM_URL", "").strip() or settings.get("submit_form_url", ""),
        "categories": [c["name"] for c in settings["categories"]],
        "stats": {
            "collected": len(raw),
            "in_window": len(in_window),
            "after_dedupe": len(candidates),
            "published": len(final),
            "gemini_model": model,
            "gemini_calls": gemini.calls,
            "gemini_status": "ok" if gemini.available else gemini.disabled_reason,
        },
        "sources": health,
        "items": [item_to_json(it) for it in final],
    }
    write_outputs(payload, now.astimezone(tz).strftime("%Y-%m-%d"))

    # 7. Remember what we've processed
    today = now.astimezone(tz).strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=SEEN_RETENTION_DAYS)).astimezone(tz).strftime("%Y-%m-%d")
    seen = {k: d for k, d in state.get("seen", {}).items() if d >= cutoff}
    submitted = set(state.get("submitted_seen", []))
    for it in in_window:
        if it.via == "submission":
            submitted.add(item_key(it))
        else:
            seen[item_key(it)] = today
    for it in final:  # decoded URLs too
        seen[item_key(it)] = today
    save_state({"last_run": iso(now), "seen": seen, "submitted_seen": sorted(submitted)})

    log.info("Done: %d stories published (Gemini: %d calls, status: %s)",
             len(final), gemini.calls, payload["stats"]["gemini_status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
