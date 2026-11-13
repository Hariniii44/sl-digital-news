"""Step 3 & 5 — Gemini: relevance screening, then category + summary.

Everything degrades gracefully: if there is no API key or the free-tier quota runs
out, keyword rules take over so the site still updates.
"""

from __future__ import annotations

import json
import re
import time

import requests

from .collect import Item
from .util import clean_text, log

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

TEAM_CONTEXT = (
    "You work for the Digital Industry Development team in Sri Lanka. The team supports "
    "the growth of Sri Lanka's digital economy: startups and the startup ecosystem "
    "(accelerators, incubators, competitions, investors), tech and IT/BPM companies, SMEs "
    "and entrepreneurs, digitalization of businesses and government, digital economy policy, "
    "and the wider Sri Lankan economy and business environment. They want to see anything "
    "that is even remotely relevant to their work."
)


class QuotaExhausted(Exception):
    """Daily free-tier quota used up — stop calling Gemini for this run."""


class Gemini:
    def __init__(self, api_key: str, model: str, min_interval: float = 6.0):
        self.api_key = api_key
        self.model = model
        self.min_interval = min_interval
        self._last_call = 0.0
        self.calls = 0
        self.disabled_reason = "" if api_key else "no GEMINI_API_KEY set"

    @property
    def available(self) -> bool:
        return not self.disabled_reason

    def _throttle(self) -> None:
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def generate_json(self, system: str, prompt: str, schema: dict):
        if not self.available:
            raise QuotaExhausted(self.disabled_reason)

        base = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        # Try the richest request first; fall back if the model rejects a field.
        configs = [
            {"responseMimeType": "application/json", "responseJsonSchema": schema,
             "thinkingConfig": {"thinkingLevel": "minimal"}},
            {"responseMimeType": "application/json", "responseJsonSchema": schema},
            {"responseMimeType": "application/json"},
        ]
        last_error = ""
        for gen_cfg in configs:
            for attempt in range(4):
                self._throttle()
                self.calls += 1
                try:
                    r = requests.post(
                        API_URL.format(model=self.model),
                        headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                        json={**base, "generationConfig": gen_cfg},
                        timeout=120,
                    )
                except requests.RequestException as exc:
                    last_error = str(exc)
                    time.sleep(10 * (attempt + 1))
                    continue

                if r.status_code == 200:
                    return _parse_json_response(r.json())
                body = r.text[:500]
                last_error = f"HTTP {r.status_code}: {body}"
                if r.status_code == 400:
                    log.info("Gemini rejected request config, trying simpler one: %s", body[:200])
                    break  # next config
                if r.status_code in (401, 403):
                    self.disabled_reason = f"API key rejected ({r.status_code})"
                    raise QuotaExhausted(self.disabled_reason)
                if r.status_code == 404:
                    self.disabled_reason = f"model '{self.model}' not found — check GEMINI_MODEL"
                    raise QuotaExhausted(self.disabled_reason)
                if r.status_code == 429:
                    if re.search(r"per.?day|daily|PerDay", body, re.I):
                        self.disabled_reason = "daily free-tier quota reached"
                        raise QuotaExhausted(self.disabled_reason)
                    delay = _retry_delay(r) or 30 * (attempt + 1)
                    log.info("Gemini rate limit, waiting %ss", delay)
                    time.sleep(delay)
                    continue
                time.sleep(10 * (attempt + 1))  # 5xx
        raise RuntimeError(f"Gemini request failed: {last_error}")


def _retry_delay(resp: requests.Response) -> int | None:
    try:
        for d in resp.json().get("error", {}).get("details", []):
            if "retryDelay" in d:
                return int(float(str(d["retryDelay"]).rstrip("s"))) + 2
    except Exception:  # noqa: BLE001
        pass
    return None


def _parse_json_response(data: dict):
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _batches(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ── Keyword fallbacks (used when Gemini is unavailable) ──────────────────────

def _kw_hits(text: str, keywords: list[str]) -> int:
    text = f" {text.lower()} "
    hits = 0
    for kw in keywords:
        kw = kw.lower().strip()
        if re.search(r"(?<![a-z0-9])" + re.escape(kw), text):
            hits += 1
    return hits


def keyword_category(item: Item, categories: list[dict]) -> tuple[str, int]:
    hay = f"{item.title} {item.description}"
    default = next((c["name"] for c in categories if "econom" in c["name"].lower()),
                   categories[0]["name"])
    best, best_hits = default, 0
    for cat in categories:
        h = _kw_hits(hay, cat.get("keywords", []))
        if h > best_hits:
            best, best_hits = cat["name"], h
    return best, best_hits


def fallback_summary(item: Item) -> str:
    source = item.description or item.text
    return clean_text(source, 320) if source else ""


# ── Step 3: relevance screening ──────────────────────────────────────────────

TRIAGE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "score": {"type": "integer"}},
        "required": ["id", "score"],
    },
}

TRIAGE_SYSTEM = TEAM_CONTEXT + """

You will receive a numbered list of news headlines (with outlet and a short snippet).
Score each one for relevance to the team, 0–10:
- 9–10: core — Sri Lankan startups, accelerators/incubators, startup competitions, tech/IT-BPM
  companies, digital economy policy, digitalization initiatives, funding for tech ventures,
  digital-industry events.
- 6–8: clearly relevant — SMEs, fintech/banking digital services, telecoms, skills/education for
  the digital economy, major Sri Lankan economic news (CBSL, IMF, budget, exports, investment).
- 3–5: remotely relevant — general Sri Lankan business, corporate news, trade, tourism economics,
  infrastructure, international tech news with a clear Sri Lanka angle.
- 0–2: not relevant — crime, accidents, weather, sport, entertainment, politics with no economic
  or digital angle, obituaries, horoscopes, news with no connection to Sri Lanka.
Return a JSON array with one object per input item: {"id": <number>, "score": <0-10>}."""


def triage(items: list[Item], gemini: Gemini, settings: dict) -> None:
    """Sets item.score for every item."""
    categories = settings["categories"]
    for batch in _batches(items, settings.get("triage_batch_size", 40)):
        lines = [
            json.dumps({"id": i, "title": it.title, "outlet": it.outlet,
                        "snippet": clean_text(it.description, 220)}, ensure_ascii=False)
            for i, it in enumerate(batch)
        ]
        scores: dict[int, int] = {}
        if gemini.available:
            try:
                result = gemini.generate_json(TRIAGE_SYSTEM, "\n".join(lines), TRIAGE_SCHEMA)
                scores = {int(r["id"]): int(r["score"]) for r in result
                          if isinstance(r, dict) and "id" in r and "score" in r}
            except QuotaExhausted as exc:
                log.warning("Gemini unavailable for screening (%s) — using keyword rules", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("Screening batch failed (%s) — using keyword rules for it", exc)
        for i, it in enumerate(batch):
            if it.via == "submission":
                it.score = 10   # the team asked for it
            elif i in scores:
                it.score = max(0, min(10, scores[i]))
            else:
                _, hits = keyword_category(it, categories)
                it.score = 5 if hits else 0


# ── Step 5: category + summary ───────────────────────────────────────────────

def summary_schema(category_names: list[str]) -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "category": {"type": "string", "enum": category_names},
                "summary": {"type": "string"},
                "score": {"type": "integer"},
            },
            "required": ["id", "category", "summary", "score"],
        },
    }


def summary_system(categories: list[dict]) -> str:
    cats = "\n".join(f'- "{c["name"]}": {c["description"]}' for c in categories)
    return TEAM_CONTEXT + f"""

You will receive news articles as JSON objects (id, title, outlet, text). For each article:
1. category — pick the ONE best category:
{cats}
2. summary — 2 to 3 sentences, at most 55 words, written in your own words in plain,
   neutral English. State the key facts: who, what, and why it matters for Sri Lanka's
   digital industry or economy where that is evident. Do not copy sentences from the text,
   do not add facts that are not in the text, no hype, no quotes.
   If the text is empty or only a headline, write ONE sentence restating the headline
   and nothing more.
3. score — relevance to the team, 0–10 (9–10 core digital-industry news, 6–8 clearly
   relevant, 3–5 remotely relevant, 0–2 not relevant).
Return a JSON array with one object per article: {{"id", "category", "summary", "score"}}."""


def categorize_and_summarize(items: list[Item], gemini: Gemini, settings: dict) -> None:
    categories = settings["categories"]
    names = [c["name"] for c in categories]
    schema = summary_schema(names)
    system = summary_system(categories)
    max_chars = settings.get("article_chars_for_summary", 1800)

    for batch in _batches(items, settings.get("summary_batch_size", 10)):
        results: dict[int, dict] = {}
        if gemini.available:
            payload = [
                {"id": i, "title": it.title, "outlet": it.outlet,
                 "text": clean_text(it.text or it.description, max_chars)}
                for i, it in enumerate(batch)
            ]
            try:
                out = gemini.generate_json(system, json.dumps(payload, ensure_ascii=False), schema)
                results = {int(r["id"]): r for r in out if isinstance(r, dict) and "id" in r}
            except QuotaExhausted as exc:
                log.warning("Gemini unavailable for summaries (%s) — using feed snippets", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("Summary batch failed (%s) — using feed snippets for it", exc)

        for i, it in enumerate(batch):
            r = results.get(i)
            if r and r.get("summary"):
                it.category = r.get("category") if r.get("category") in names else keyword_category(it, categories)[0]
                it.summary = clean_text(r["summary"], 450)
                if it.via != "submission" and isinstance(r.get("score"), int):
                    it.score = max(0, min(10, r["score"]))
                it.ai = True
            else:
                it.category = keyword_category(it, categories)[0]
                it.summary = fallback_summary(it)
                it.ai = False
