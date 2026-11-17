"""End-to-end test with fake feeds, fake article pages and a fake Gemini API.

Run:  python -m pytest -q
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from pipeline import ai, collect, enrich, run

NOW = datetime.now(timezone.utc)


def rfc(dt):
    return format_datetime(dt)


def wordpress_feed(items):
    body = "".join(
        f"""<item><title>{t}</title><link>{u}</link><pubDate>{rfc(d)}</pubDate>
        <dc:creator><![CDATA[{a}]]></dc:creator><description><![CDATA[<p>{s}</p>]]></description></item>"""
        for t, u, d, a, s in items
    )
    return f"""<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
    <channel><title>Feed</title>{body}</channel></rss>""".encode()


def gnews_feed(items):
    body = "".join(
        f"""<item><title>{t} - {o}</title><link>https://news.google.com/rss/articles/{i}</link>
        <pubDate>{rfc(d)}</pubDate><source url="https://{o.lower().replace(' ', '')}.lk">{o}</source></item>"""
        for i, (t, o, d) in enumerate(items)
    )
    return f"""<?xml version="1.0"?><rss version="2.0"><channel><title>GN</title>{body}</channel></rss>""".encode()


FEEDS = {
    "economynext.com/feed": wordpress_feed([
        ("Sri Lanka startup Kapruka raises seed funding for AI logistics",
         "https://economynext.com/kapruka-ai-1/", NOW - timedelta(hours=3), "Jane Perera",
         "A Colombo startup raised funding."),
        ("SLASSCOM says IT/BPM exports grew 12 percent",
         "https://economynext.com/slasscom-exports-2/?utm_source=x", NOW - timedelta(hours=5), "admin",
         "IT exports grew."),
        ("Old story from last week about rupee", "https://economynext.com/old/",
         NOW - timedelta(days=6), "X", "old"),
    ]),
    "adaderana.lk/rss.php": wordpress_feed([
        ("Three injured in road accident in Kandy", "https://adaderana.lk/news/1",
         NOW - timedelta(hours=2), "", "Accident."),
        ("Central Bank keeps policy rates unchanged", "https://adaderana.lk/news/2",
         NOW - timedelta(hours=4), "", "The CBSL monetary board decided to keep rates."),
    ]),
    "news.google.com/rss/search": gnews_feed([
        ("SLASSCOM says IT/BPM exports grew 12 percent", "Daily Mirror", NOW - timedelta(hours=5)),
        ("First Capital Startup Nation Xcellerator opens applications for cohort 4", "Daily FT",
         NOW - timedelta(hours=8)),
        ("Cricket: Sri Lanka beat Pakistan", "Newsfirst", NOW - timedelta(hours=1)),
    ]),
}


class FakeResp:
    def __init__(self, content: bytes, status=200):
        self.content = content
        self.text = content.decode("utf-8", "ignore")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


def fake_get(url, *a, **kw):
    for key, content in FEEDS.items():
        if key in url:
            return FakeResp(content)
    return FakeResp(b"not found", 404)


ARTICLE_HTML = """<html><head><title>{t}</title>
<meta name="author" content="Nimal Silva"><meta property="article:published_time" content="{d}"></head>
<body><article><h1>{t}</h1><p>{p}</p><p>{p}</p><p>{p}</p></article></body></html>"""


def fake_article_get(url, *a, **kw):
    para = ("The programme supports early-stage technology founders in Sri Lanka with mentoring, "
            "investment readiness training and access to a network of investors and corporates. ") * 3
    return FakeResp(ARTICLE_HTML.format(t="Article", d=NOW.isoformat(), p=para).encode())


def fake_gemini_post(url, headers=None, json=None, timeout=None):  # noqa: A002
    prompt = json["contents"][0]["parts"][0]["text"]
    system = json["systemInstruction"]["parts"][0]["text"]
    if "Score each one" in system:
        out = []
        for line in prompt.splitlines():
            obj = __import__("json").loads(line)
            t = obj["title"].lower()
            score = 0 if re.search(r"accident|cricket", t) else 8
            out.append({"id": obj["id"], "score": score,
                        "group": "Unrelated" if score == 0 else "In scope"})
    else:
        arts = __import__("json").loads(prompt)
        out = [{"id": a["id"], "category": "Startups & Ecosystem",
                "summary": f"Summary of: {a['title']}", "score": 9} for a in arts]
    body = {"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(out)}]}}]}
    return FakeResp(__import__("json").dumps(body).encode())


@pytest.fixture
def env(tmp_path, monkeypatch):
    sources = {
        "outlets": [
            {"name": "EconomyNext", "type": "rss", "url": "https://economynext.com/feed/"},
            {"name": "Ada Derana", "type": "rss", "url": "https://www.adaderana.lk/rss.php", "general": True},
            {"name": "Broken Outlet", "type": "rss", "url": "https://broken.lk/feed/",
             "fallback_query": "site:broken.lk"},
        ],
        "topics": ['"Sri Lanka" startup'],
    }
    real_load = run.load_yaml
    monkeypatch.setattr(run, "load_yaml", lambda n: sources if n == "sources.yaml" else real_load(n))
    monkeypatch.setattr(run, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(run, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(collect, "_get", fake_get)
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(enrich.requests, "get", fake_article_get)
    monkeypatch.setattr(enrich, "decode_google_news_links",
                        lambda items: [setattr(i, "url", i.url.replace("news.google.com/rss/articles/",
                                                                          "dailyft.lk/article/")) for i in items])
    monkeypatch.setattr(ai.requests, "post", fake_gemini_post)
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return tmp_path


def load_latest(tmp):
    return json.loads((tmp / "data" / "latest.json").read_text())


def test_full_run(env):
    assert run.main([]) == 0
    data = load_latest(env)
    in_scope = [i for i in data["items"] if i["in_scope"]]
    titles = [i["title"] for i in in_scope]

    assert any("Kapruka" in t for t in titles)
    assert any("Xcellerator" in t for t in titles)
    assert any("Central Bank" in t for t in titles)
    assert not any("accident" in t.lower() for t in titles)        # screened out of the main list
    assert not any("Cricket" in t for t in titles)

    # ...but kept, filed under an out-of-scope group, without an article fetch / AI summary
    out = [i for i in data["items"] if not i["in_scope"]]
    assert any("Cricket" in i["title"] for i in out)
    assert all(i["category"] in data["out_of_scope_categories"] for i in out)
    assert all(i["ai_summary"] is False for i in out)
    assert data["stats"]["out_of_scope"] == len(out)
    assert all(c not in data["out_of_scope_categories"] for c in data["categories"])
    assert not any("Old story" in t for t in titles)               # outside window
    assert sum("SLASSCOM" in t for t in titles) == 1               # deduped across routes

    slass = next(i for i in data["items"] if "SLASSCOM" in i["title"])
    assert slass["outlet"] == "EconomyNext"                        # direct feed preferred
    assert any(x["outlet"] == "Daily Mirror" for x in slass["also_in"])

    kap = next(i for i in data["items"] if "Kapruka" in i["title"])
    assert kap["author"] == "Jane Perera"
    assert kap["summary"].startswith("Summary of")
    assert kap["ai_summary"] is True

    # broken feed fell back to Google News
    names = {s["name"]: s for s in data["sources"]}
    assert names["Broken Outlet"]["ok"] is False
    assert "Broken Outlet (Google News fallback)" in names

    # items sorted newest first
    pubs = [i["published"] for i in in_scope]
    assert pubs == sorted(pubs, reverse=True)

    # archive index written
    idx = json.loads((env / "data" / "archive" / "index.json").read_text())
    assert len(idx["dates"]) == 1


def test_second_run_does_not_repeat(env):
    run.main([])
    first = {i["id"] for i in load_latest(env)["items"]}
    assert first
    run.main([])
    # same day → merged into the same archive file, no duplicates
    second = load_latest(env)["items"]
    ids = [i["id"] for i in second]
    assert len(ids) == len(set(ids))
    assert set(ids) == first


def _old_item(id_, title, hours_ago, in_scope=True):
    return {"id": id_, "title": title, "url": f"https://x.lk/{id_}", "outlet": "X", "author": "",
            "published": (NOW - timedelta(hours=hours_ago)).isoformat(), "time_estimated": False,
            "category": "Tech Industry & SMEs" if in_scope else "Unrelated", "summary": "s", "score": 8 if in_scope else 1,
            "in_scope": in_scope, "via": "rss", "ai_summary": True, "also_in": []}


def test_latest_shows_a_rolling_24_hours(env):
    from zoneinfo import ZoneInfo
    yesterday = (datetime.now(ZoneInfo("Asia/Colombo")) - timedelta(days=1)).strftime("%Y-%m-%d")
    arch = env / "data" / "archive"
    arch.mkdir(parents=True)
    (arch / f"{yesterday}.json").write_text(json.dumps({"items": [
        _old_item("y-recent", "Yesterday afternoon story", 10),
        _old_item("y-oos", "Yesterday out-of-scope story", 12, in_scope=False),
        _old_item("y-old", "Yesterday morning story", 30),          # older than 24h
    ]}))

    assert run.main([]) == 0
    latest = load_latest(env)
    titles = {i["title"] for i in latest["items"]}

    assert "Yesterday afternoon story" in titles                    # still inside the 24h window
    assert "Yesterday out-of-scope story" in titles                 # out-of-scope rolls too
    assert "Yesterday morning story" not in titles                  # older than 24h: dropped
    assert any("Kapruka" in t for t in titles)                      # and today's new stories
    ids = [i["id"] for i in latest["items"]]
    assert len(ids) == len(set(ids))
    pubs = [i["published"] for i in latest["items"]]
    assert pubs == sorted(pubs, reverse=True)

    # counts describe what is shown
    assert latest["stats"]["published"] == sum(i["in_scope"] for i in latest["items"])
    assert latest["stats"]["out_of_scope"] == sum(not i["in_scope"] for i in latest["items"])
    # the window the header reports is 24 hours
    start = datetime.fromisoformat(latest["window_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(latest["window_end"].replace("Z", "+00:00"))
    assert abs((end - start) - timedelta(hours=24)) < timedelta(seconds=1)

    # today's own file only records what was new today; yesterday's file is untouched
    today_file = next(f for f in arch.glob("2*.json") if f.stem != yesterday)
    today_titles = {i["title"] for i in json.loads(today_file.read_text())["items"]}
    assert "Yesterday afternoon story" not in today_titles
    assert len(json.loads((arch / f"{yesterday}.json").read_text())["items"]) == 3


def test_fresh_run_rebuilds_the_day(env):
    run.main([])
    latest = load_latest(env)
    first = {i["id"] for i in latest["items"]}
    # simulate a leftover item from an earlier run under old rules
    legacy = {"id": "legacy1", "title": "Old-rules story", "url": "https://x.lk/old", "outlet": "X",
              "author": "", "published": latest["items"][0]["published"], "time_estimated": False,
              "category": "SMEs & Entrepreneurship", "summary": "", "score": 3, "via": "rss",
              "ai_summary": True, "also_in": []}
    for f in (env / "data").rglob("*.json"):
        if f.name == "index.json":
            continue
        d = json.loads(f.read_text())
        d["items"].append(legacy)
        f.write_text(json.dumps(d))
    run.main([])                                  # normal run: keeps the leftover, nothing new
    assert "legacy1" in {i["id"] for i in load_latest(env)["items"]}
    run.main(["--fresh"])                         # fresh run: re-screens everything, drops the leftover
    ids = [i["id"] for i in load_latest(env)["items"]]
    assert "legacy1" not in ids
    assert set(ids) == first and len(ids) == len(set(ids))


def test_runs_without_gemini(env, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    assert run.main([]) == 0
    data = load_latest(env)
    assert data["stats"]["gemini_status"] == "no GEMINI_API_KEY set"
    titles = [i["title"] for i in data["items"]]
    assert any("Kapruka" in t for t in titles)          # keyword fallback keeps it
    allowed = data["categories"] + data["out_of_scope_categories"]
    assert all(i["category"] in allowed for i in data["items"])
    assert all((i["category"] in data["categories"]) == i["in_scope"] for i in data["items"])


def test_gemini_daily_quota_falls_back(env, monkeypatch):
    calls = {"n": 0}

    def quota_post(*a, **kw):
        calls["n"] += 1
        return FakeResp(b'{"error": {"message": "Quota exceeded for GenerateRequestsPerDayPerProjectPerModel"}}', 429)

    monkeypatch.setattr(ai.requests, "post", quota_post)
    assert run.main([]) == 0
    data = load_latest(env)
    assert data["stats"]["gemini_status"] == "daily free-tier quota reached"
    assert calls["n"] == 1                               # stopped immediately
    assert data["items"]


def test_gemini_400_falls_back_to_simpler_config(monkeypatch):
    seen_cfgs = []

    def post(url, headers=None, json=None, timeout=None):  # noqa: A002
        cfg = json["generationConfig"]
        seen_cfgs.append(cfg)
        if "thinkingConfig" in cfg:
            return FakeResp(b'{"error": {"message": "Unknown field thinkingConfig"}}', 400)
        return FakeResp(b'{"candidates":[{"content":{"parts":[{"text":"[{\\"id\\":0,\\"score\\":7}]"}]}}]}')

    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    g = ai.Gemini("k", "m", 0)
    assert g.generate_json("s", "p", {}) == [{"id": 0, "score": 7}]
    assert len(seen_cfgs) == 2 and "thinkingConfig" not in seen_cfgs[1]


def test_sunday_times_url():
    # Friday 13 Nov 2026 → edition of Sunday 8 Nov 2026
    url = collect.sunday_times_feed_url(datetime(2026, 11, 13, 4, 0, tzinfo=timezone.utc))
    assert url == "https://www.sundaytimes.lk/261108/business-times/feed/business_times.xml"
    # On a Sunday morning it's that day's edition
    url = collect.sunday_times_feed_url(datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc))
    assert "/261115/" in url


def test_sunday_times_date_format():
    from pipeline.util import parse_date
    dt = parse_date("14 December 2019 @ 7:23 pm")
    assert dt.hour == 13 and dt.minute == 53   # 19:23 Sri Lanka = 13:53 UTC


def test_submissions_csv(monkeypatch):
    csv_text = ("Timestamp,Link to the post,Title (optional),Source / page name,Paste the post text (optional)\n"
                "13/11/2026 10:44:12,https://www.linkedin.com/posts/examiner-123,,Examiner,"
                "\"Examiner reports a new fintech sandbox announced by CBSL for startups.\"\n"
                "13/11/2026 10:50:00,not-a-link,,,\n")
    monkeypatch.setattr(collect, "_get", lambda url, timeout=20: SimpleNamespace(content=csv_text.encode()))
    items = collect.fetch_submissions("https://docs.google.com/x.csv")
    assert len(items) == 1
    assert items[0].outlet == "Examiner"
    assert "fintech sandbox" in items[0].title
    assert items[0].via == "submission"


def test_all_sources_failing_keeps_previous_edition(env, monkeypatch):
    run.main([])
    before = (env / "data" / "latest.json").read_text()
    state_before = (env / "state.json").read_text()
    monkeypatch.setattr(collect, "_get", lambda *a, **k: FakeResp(b"", 503))
    assert run.main([]) == 1
    assert (env / "data" / "latest.json").read_text() == before
    assert (env / "state.json").read_text() == state_before
