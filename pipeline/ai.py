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

TEAM_CONTEXT = """\
You work for the Digital Industry Development team of GovTech in Sri Lanka. The team's job is
to grow Sri Lanka's DIGITAL INDUSTRY: IT/BPM and software exports, tech companies, startups and
the startup ecosystem, digital skills and talent, and the digital economy that supports them.
You screen news for this team. Be strict: a short, relevant feed is far more useful to them than
a long one. When in doubt, keep it as out of scope.

THE DIGITAL-ANGLE TEST
A story is relevant only if it directly affects Sri Lanka's digital industry or its digital
ecosystem. Ask: "Would someone building or supporting Sri Lanka's tech/digital sector need to
know this?" A Sri Lankan story must have a digital/tech angle. The one exception is GLOBAL
TECH (below), which needs no Sri Lanka connection but must be about the technology sector.

IN SCOPE
- IT/BPM, software and ICT services and exports (including export targets and policy).
- Sri Lankan startups, founders, accelerators, incubators, competitions, hackathons, and
  funding or investment into tech ventures.
- Tech companies operating in Sri Lanka, and foreign investment or partnerships in Sri Lanka's
  tech sector, including data centres and telecoms/digital infrastructure.
- Digital economy policy, regulation and strategy (ICTA, Ministry of Digital Economy, GovTech,
  digital ID, data protection, online safety, digital public infrastructure, e-government).
- Digital and tech skills, training and talent (academies, tech degrees, upskilling programmes).
- SMEs and entrepreneurs ADOPTING digital tools or selling through digital platforms, and
  programmes that support tech or digital entrepreneurship.
- Fintech, digital payments and e-commerce as industries (new platforms, regulation, adoption).
- AI, cybersecurity and emerging tech when it concerns Sri Lankan organisations, policy or talent.
- Digital-industry events, grants, calls for applications and deadlines.
- GLOBAL TECH: significant international technology-sector news, with no Sri Lanka connection
  needed. This means AI (models, products, regulation, major deals), software and cloud,
  cybersecurity incidents and policy, semiconductors and chips, big-tech company news
  (Nvidia, Microsoft, Google, Apple, Meta, OpenAI, Anthropic and similar), telecoms and
  networks, tech startups, funding and IPOs, and tech regulation or digital policy of major
  countries. Typically from international outlets such as BBC and CNN, but a global tech story
  from a Sri Lankan outlet counts too. Score it 5-8 depending on how significant it is for
  the tech industry. These are relevant stories, not out-of-scope ones.
- Macro or policy news (budget, CBSL, IMF, trade) ONLY IF it explicitly affects the IT sector,
  digital economy, startups or digital SMEs.

OUT OF SCOPE (not relevant even though they may sound economic, "tech" or Sri Lankan)
- General business and corporate news with no digital angle: bank or company MoUs,
  sponsorships, awards, CSR, product launches, profits, appointments, marketing and influencer
  campaigns, events with no tech content.
- Non-digital sectors: agriculture, fisheries, food, manufacturing, tourism, energy and
  solar, construction, real estate, transport. This includes SME or export stories from those
  sectors (for example seafood exporters or a fruit-ripening facility): being an SME or an
  exporter does not make a story digital.
- Macro and market news with no digital angle: budget summaries, inflation, interest rates,
  exchange rates, stock market, credit card or loan statistics.
- International news that is NOT about the technology sector: space missions and rockets,
  science, consumer gadget reviews and deals, video games, celebrity or executive gossip,
  social media drama, and general world news (those go to "Unrelated").
- Opinion or commentary that only touches technology in passing; fact-checks and
  misinformation debunks; e-waste, environment or safety incidents.
- Politics, crime, accidents, weather, sport, entertainment, obituaries, horoscopes, and any
  news with no connection to Sri Lanka.
- Education news that has no tech or digital-skills content (for example a general quality
  assurance award).
"""

# Worked examples (headline -> score and why). They calibrate the scoring below.
SCORING_EXAMPLES = """\
WORKED EXAMPLES
- "EDB, Digital Economy Ministry join hands to achieve US$5bn ICT export target" -> 10
  (ICT exports and digital economy policy: core.)
- "GovTech to set up Digital Transformation Academy" -> 10 (GovTech, digital skills: core.)
- "GLEC digital platform launched to connect Sri Lankan entrepreneurs worldwide" -> 8
  (digital platform for entrepreneurs: clearly relevant.)
- "eChannelling shines at SLIM National Sales Awards 2026" -> 6
  (Sri Lankan digital health platform, but only an award mention: marginal but relevant.)
- "Competition in energy sector creates opportunities for SMEs" -> 2, group "Other Sectors"
  (an energy-sector story; the SME mention does not make it digital.)
- "Seafood exporters hail Budget 2027, eye $680m by 2030" -> 1, group "Other Sectors"
  (fisheries and a budget reaction; no digital angle. Use "Macro & Economy" if the story is only about the budget itself.)
- "Hygienic fruit-ripening chamber opened in Anuradhapura" -> 1, group "Other Sectors"
  (agriculture; no digital angle.)
- "Cargills Bank-Toyota Lanka sign landmark MoU" -> 2, group "Corporate News"
  (a corporate MoU with no digital element.)
- "Sri Lanka's new active credit card count up 1-pct in September" -> 3, group "Macro & Economy"
  (financial statistics, not about digital payments or fintech platforms.)
- "HighVoltagePR leads influencer rollout for Daraz 11.11 sale" -> 3, group "Corporate News"
  (a marketing campaign, not about the e-commerce industry itself.)
- "Uber Eats joins Eat Street Colombo as title sponsor" -> 1, group "Corporate News" (sponsorship; no digital angle.)
- "SpaceX completes first-ever spaceship capture" -> 1, group "Unrelated"
  (space and rockets, not the IT/digital sector.)
- "Musk says Nvidia will lose critical deals without Jensen Huang" -> 5, category Global Tech
  (big-tech and chips industry, but mostly an executive's opinion: marginal.)
- "Anthropic's $90 Billion IPO tests investor confidence in the global AI boom" -> 7, category Global Tech
  (major AI-industry funding news: relevant as global tech.)
- "BBC: EU opens investigation into big-tech AI chatbots over data protection" -> 7, category Global Tech
  (global tech regulation.)
- "CNN: The best noise-cancelling headphones to buy this Black Friday" -> 1, group "Unrelated"
  (consumer shopping, not the tech industry.)
- "Fire destroys e-waste warehouse in Divulapitiya" -> 0, group "Fact-checks, Incidents & Opinion" (an incident, not digital industry.)
- "Anuradhapura Satyagraha photo of smashed Buddha statues is AI-generated" -> 0, group "Fact-checks, Incidents & Opinion"
  (a fact-check; not about the digital industry.)
"""


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

IN_SCOPE = "In scope"


def oos_names(settings: dict) -> list[str]:
    return [c["name"] for c in settings.get("out_of_scope_categories", [])]


def _oos_text(settings: dict) -> str:
    return "\n".join(f'- "{c["name"]}": {c["description"]}' for c in settings.get("out_of_scope_categories", []))


def triage_schema(oos: list[str]) -> dict:
    props = {"id": {"type": "integer"}, "score": {"type": "integer"}}
    if oos:
        props["group"] = {"type": "string", "enum": [IN_SCOPE, *oos]}
    return {
        "type": "array",
        "items": {"type": "object", "properties": props, "required": ["id", "score"]},
    }


def triage_system(settings: dict) -> str:
    min_rel = settings.get("min_relevance", 5)
    oos = _oos_text(settings)
    group_rule = ""
    ret = 'Return a JSON array with one object per input item: {"id": <number>, "score": <0-10>}.'
    if oos:
        group_rule = f"""
Stories that score below {min_rel} are not deleted: they are filed under an out-of-scope group so
the team can still browse them. For every story also give "group":
- "{IN_SCOPE}" if the score is {min_rel} or more.
- otherwise the ONE best-fitting out-of-scope group:
{oos}
"""
        ret = 'Return a JSON array with one object per input item: {"id": <number>, "score": <0-10>, "group": <group>}.'
    return TEAM_CONTEXT + f"""

You will receive a numbered list of news headlines (with outlet and a short snippet).
Score each one 0-10 for relevance to the team, applying the digital-angle test:
- 9-10: core. Directly about Sri Lanka's digital industry: IT/BPM and ICT exports, startups and
  funding, tech companies, digital economy policy or GovTech, digital skills, digital-industry events.
- 7-8: clearly relevant. A strong digital angle in a neighbouring area: digital SMEs, fintech or
  e-commerce industry news, telecoms and digital infrastructure, Sri Lankan AI or cyber news.
- 5-6: relevant but marginal. A genuine, if minor, digital angle (for example a digital platform
  mentioned in an awards story). Use this sparingly.
- 3-4: NOT relevant. General business or economy news with no digital angle, or a loosely
  tech-flavoured story that does not affect Sri Lanka's digital industry.
- 0-2: NOT relevant. Out-of-scope sectors, non-tech international news, consumer gadgets,
  space, incidents, crime, politics, sport, entertainment, fact-checks, or Sri Lankan news
  with no digital angle.
Only {min_rel} and above are shown to the team as relevant. Judge on whether the story passes the
digital-angle test, not on whether it merely sounds like business or technology. A headline
that does not show a clear digital angle should score {min_rel - 1} or lower.
{group_rule}
""" + SCORING_EXAMPLES + "\n" + ret


def triage(items: list[Item], gemini: Gemini, settings: dict) -> None:
    """Sets item.score (and item.oos_group for low scorers) for every item."""
    categories = settings["categories"]
    oos = oos_names(settings)
    fallback_group = oos[-1] if oos else ""
    min_rel = settings.get("min_relevance", 5)
    system = triage_system(settings)
    schema = triage_schema(oos)
    for batch in _batches(items, settings.get("triage_batch_size", 40)):
        lines = [
            json.dumps({"id": i, "title": it.title, "outlet": it.outlet,
                        "snippet": clean_text(it.description, 220)}, ensure_ascii=False)
            for i, it in enumerate(batch)
        ]
        scores: dict[int, int] = {}
        groups: dict[int, str] = {}
        if gemini.available:
            try:
                result = gemini.generate_json(system, "\n".join(lines), schema)
                for r in result:
                    if isinstance(r, dict) and "id" in r and "score" in r:
                        scores[int(r["id"])] = int(r["score"])
                        if r.get("group") in oos:
                            groups[int(r["id"])] = r["group"]
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
                it.score = min_rel if hits else 0
            if it.score < min_rel:
                it.oos_group = groups.get(i, fallback_group)


# ── Step 5: category + summary ───────────────────────────────────────────────

def summary_schema(category_names: list[str], oos: list[str] | None = None) -> dict:
    props = {
        "id": {"type": "integer"},
        "category": {"type": "string", "enum": category_names},
        "summary": {"type": "string"},
        "score": {"type": "integer"},
    }
    if oos:
        props["group"] = {"type": "string", "enum": [IN_SCOPE, *oos]}
    return {
        "type": "array",
        "items": {"type": "object", "properties": props,
                  "required": ["id", "category", "summary", "score"]},
    }


def summary_system(categories: list[dict], settings: dict | None = None) -> str:
    settings = settings or {}
    min_rel = settings.get("min_relevance", 5)
    oos = _oos_text(settings)
    cats = "\n".join(f'- "{c["name"]}": {c["description"]}' for c in categories)
    group_rule = ""
    ret = '{"id", "category", "summary", "score"}'
    if oos:
        group_rule = f"""
4. group - "{IN_SCOPE}" if your score is {min_rel} or more. Otherwise the ONE best-fitting
   out-of-scope group:
{oos}
"""
        ret = '{"id", "category", "summary", "score", "group"}'
    return TEAM_CONTEXT + "\n" + SCORING_EXAMPLES + f"""

You will receive news articles as JSON objects (id, title, outlet, text). For each article:
1. category — pick the ONE best category (even if the story turns out not to be relevant):
{cats}
2. summary — 2 to 3 sentences, at most 55 words, written in your own words in plain,
   neutral English. State the key facts: who, what, and why it matters for Sri Lanka's
   digital industry or economy where that is evident. Do not copy sentences from the text,
   do not add facts that are not in the text, no hype, no quotes.
   If the text is empty or only a headline, write ONE sentence restating the headline
   and nothing more.
3. score — relevance to the team, 0-10, using the digital-angle test. You now have the full
   article text, so check carefully: 9-10 core digital-industry news, 7-8 clearly relevant,
   5-6 marginal but genuinely digital or tech, 0-4 NOT relevant (no real digital/tech angle).
   Global tech-sector stories need no Sri Lanka link; Sri Lankan stories need a digital angle. Lower the score if the article turns out to be only loosely digital.
{group_rule}Return a JSON array with one object per article: {ret}."""


def categorize_and_summarize(items: list[Item], gemini: Gemini, settings: dict) -> None:
    categories = settings["categories"]
    names = [c["name"] for c in categories]
    oos = oos_names(settings)
    min_rel = settings.get("min_relevance", 5)
    schema = summary_schema(names, oos)
    system = summary_system(categories, settings)
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
                    if r.get("group") in oos:
                        it.oos_group = r["group"]
                it.ai = True
            else:
                it.category = keyword_category(it, categories)[0]
                it.summary = fallback_summary(it)
                it.ai = False
        for it in batch:
            if it.score < min_rel and oos and not it.oos_group:
                it.oos_group = oos[-1]
