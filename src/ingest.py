"""Ingestion adapters. Every adapter returns RawSignal[] — one shape for the whole pipeline.

Adding a source = adding one function + one entry in config.yaml. Nothing downstream changes.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

import feedparser
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

from .models import RawSignal

log = logging.getLogger(__name__)
# Requests look like an ordinary desktop browser. News sites and their CDNs refuse
# requests from unfamiliar robot names — the first live run had every article
# download refused for exactly that reason.
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
TIMEOUT = 12          # keep a slow site from dominating the run

# Leading editorial noise and descriptors that sit in front of the actual company name.
_LEAD_NOISE = re.compile(
    r"^(?:exclusive|breaking|scoop|just in|update|watch|explained)\s*[:\-–]\s*", re.I)
_LEAD_DESCRIPTOR = re.compile(
    r"^(?:"
    r"(?:[A-Z][\w.&]+)-(?:backed|funded|led|owned|incubated|supported)\s+"  # Microsoft-backed
    r"|(?:[A-Z][a-z]+(?:-[A-Z]?[a-z]+)?)-(?:based|headquartered)\s+"      # Bengaluru-based
    r"|(?:deep\s?tech|fintech|healthtech|medtech|edtech|agritech|climatetech|insurtech|"
    r"spacetech|foodtech|proptech|cleantech|biotech|ai|a\.i\.|saas|b2b|b2c|d2c|"
    r"quick\s?commerce|logistics|robotics|consumer|enterprise|indian|india)\s+"
    r"|(?:startup|start-up|firm|company|platform|venture|brand|major|player|unicorn|"
    r"soonicorn)\s+"
    r")+", re.I)

# Deliberately excludes bare "announces": "Microsoft announces Copilot features" is not
# a funding event, and treating it as one turns any product story into a prospect.
# Announcement phrasing only counts when it is explicitly about raising money.
_ACTION = (r"raises?|raised|secures?|secured|bags?|nets?|lands?|gets?|picks up|"
           r"mops up|closes?|emerges? from stealth|comes? out of stealth|"
           r"exits? stealth|is out of stealth|"
           r"announces? (?:a |its |the )?(?:\$|₹|rs\.?|inr|usd|\d|seed|pre-seed|"
           r"series|funding|round|raise)")
COMPANY_RE = re.compile(rf"^([A-Z][\w&.\- ]{{1,40}}?)\s+(?:{_ACTION})\b", re.I)
LI_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[\w\-.]+", re.I)


# ------------------------------------------------------------------ helpers
_POSSESSIVE_LEAD = re.compile(
    r"^(?:India|Bengaluru|Bangalore|Mumbai|Delhi|Gurugram|Noida|Pune|Hyderabad|Chennai|"
    r"Kolkata|Ahmedabad|Jaipur|Kochi)['\u2019]s\s+", re.I)
# "background music startup Tringbox" -> "Tringbox"
_DESCRIPTOR_LEAD = re.compile(
    r"^(?:[\w\-]+\s+){0,4}?(?:startup|start-up|firm|company|platform|brand|maker|"
    r"provider|player|venture|app)\s+(?=[A-Z0-9])", re.I)


def _guess_company(title: str) -> str:
    """Pull the company name out of a funding headline.

    Headlines carry a lot of scaffolding — "Exclusive:", "Bengaluru-based",
    "Deeptech startup" — and leaving it attached corrupts the dedup key and the
    company name shown to the AE. Strip the scaffolding, then match up to the verb.
    """
    t = _LEAD_NOISE.sub("", title.strip())
    t = _POSSESSIVE_LEAD.sub("", t)
    t = _LEAD_DESCRIPTOR.sub("", t).strip()
    t = _DESCRIPTOR_LEAD.sub("", t).strip()
    m = COMPANY_RE.search(t)
    if m:
        return m.group(1).strip(" ,-–—")
    # No recognised verb: fall back to the text before the first action word, which
    # gate_is_company will reject if it is really a headline fragment.
    return re.split(rf"\s+(?:{_ACTION})\b", t, flags=re.I)[0].strip()[:60]


# --- per-company extraction from article bodies ------------------------------
#
# Weekly funding roundups ("Indian startups raised $120M across 18 deals this week")
# are where most small seed rounds actually appear — they rarely get their own
# article. Reading only headlines throws all of them away.
#
# The rule that makes body-reading safe: the company name and the funding verb must
# come from the SAME SENTENCE, and the company is taken FROM that sentence. That is
# precisely what the Microsoft bug violated — a verb from a sidebar attached to a
# company from the headline.

_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z\"\'])')

_RAISE_VERB = (r"raised|raises|secured|secures|bagged|bags|netted|nets|landed|lands|"
               r"mopped up|mops up|picked up|picks up|closed|closes|received|receives")

# "Vecton AI raised $2.4 million", "Bengaluru-based Gridbot has raised Rs 15 crore"
_BODY_DEAL_RE = re.compile(
    r"(?P<name>(?:[A-Z][\w&.'’\-]*)(?:\s+[A-Z0-9][\w&.'’\-]*){0,3})"
    r"(?P<mid>(?:,[^,.]{0,80},)?\s+(?:has |have |had )?)"
    r"(?P<verb>" + _RAISE_VERB + r")\s+"
    r"(?P<tail>[^.]{0,90})",
    re.U)

_AMOUNT_IN_TAIL = re.compile(
    r"(?:\$|usd|inr|rs\.?|₹)\s?[\d.,]+\s?(?:k|mn|m|million|bn|billion|cr|crore|lakh)?"
    r"|[\d.,]+\s?(?:crore|cr|lakh|million|mn|billion|bn)\b"
    r"|\b(?:seed|pre-seed|pre-series|series\s+[a-c]|angel|bridge|undisclosed)\b", re.I)

# Words that mean the capitalised phrase is an investor, a publication, or scaffolding
# rather than the company that received money.
_NOT_A_PROSPECT = re.compile(
    r"\b(?:ventures?|capital|partners|fund|funds|llp|advisors?|investments?|holdings|"
    r"angels?|network|syndicate|group|media|news|magazine|report|exchange|bank|"
    r"the company|company|startup|startups|it|they|this|these|other|another|also|"
    r"meanwhile|however|additionally|separately|earlier|last|meanwhile)\b", re.I)


_POSSESSIVE_CITY = re.compile(
    r"^(?:Bengaluru|Bangalore|Mumbai|Delhi|Gurugram|Gurgaon|Noida|Pune|Hyderabad|"
    r"Chennai|Kolkata|Ahmedabad|Jaipur|Kochi|Indore|India)['’]s\s+", re.I)


def _clean_company(name: str) -> str:
    name = re.sub(r"^(?:And|But|Also|Meanwhile|However|Separately|Additionally|"
                  r"Besides|Further|Moreover)\s+", "", name.strip(), flags=re.I)
    name = _POSSESSIVE_CITY.sub("", name)          # "Chennai's Orbit" -> "Orbit"
    name = _LEAD_DESCRIPTOR.sub("", name).strip()
    return name.strip(" ,;:-–—")


def extract_body_deals(sig: RawSignal, max_per_article: int = 12) -> list[RawSignal]:
    """Pull one signal per company that a SENTENCE says raised money.

    This is what unlocks weekly roundups. It is also why a stray sidebar headline is
    now harmless: it produces a signal for the company the sidebar actually names,
    which deduplication then collapses against that company's own article.
    """
    body = sig.body or ""
    if not body:
        return []

    out: list[RawSignal] = []
    seen: set[str] = set()
    headline_company = (sig.company_name or "").lower()

    for sentence in _SENTENCE_SPLIT.split(body):
        sentence = sentence.strip()
        if not (25 <= len(sentence) <= 400):
            continue
        for m in _BODY_DEAL_RE.finditer(sentence):
            tail = m.group("tail")
            # A raise must come with a sum or a round name, or it is not a deal.
            if not _AMOUNT_IN_TAIL.search(tail):
                continue
            name = _clean_company(m.group("name"))
            if len(name) < 2 or len(name.split()) > 4:
                continue
            if _NOT_A_PROSPECT.search(name):
                continue
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            if not key or key in seen or key == re.sub(r"[^a-z0-9]", "", headline_company):
                continue
            seen.add(key)

            # The sentence alone often omits geography and sector — a roundup entry
            # reads "Medha bagged Rs 15 crore" and states India only in the article
            # headline. So the child carries the parent's framing as CONTEXT for the
            # India and ICP gates, while the capital gate still reads the sentence
            # alone (it is the title). Context can inform who a company is; it can
            # never manufacture a funding event.
            context = f"{sig.title} {body[:400]}"
            child = RawSignal(
                source_id=sig.source_id,
                # The sentence becomes the title, so headline-level rules apply to the
                # text that actually asserts the deal.
                title=sentence[:300],
                url=sig.url,
                published_at=sig.published_at,
                body=f"{sentence}\n\n[Article context] {context}",
                company_name=name,
                signal_type=sig.signal_type,
                from_body=True,
            )
            out.append(child)
            if len(out) >= max_per_article:
                return out
    return out


def _parse_feed(url: str):
    """Download a feed as a browser would, then parse it. feedparser's own downloader
    uses a robot name that many news sites block."""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        if feed.entries:
            return feed
    except Exception as exc:                                    # noqa: BLE001
        log.warning("feed download failed %s: %s", url, exc)
    return feedparser.parse(url)


_HOST_LOCKS: dict[str, Any] = {}
_HOST_LAST: dict[str, float] = {}
_HOST_GAP = 2.0          # seconds between requests to the same site


def _fetch(url: str) -> str:
    """Download a page politely: at most one request every couple of seconds per
    site, and if the site says "too many requests" (429), wait and try again.
    Inc42 refused downloads twice for being asked too quickly."""
    import threading
    import time as _time
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    lock = _HOST_LOCKS.setdefault(host, threading.Lock())
    for attempt, pause in enumerate((0, 8, 20)):
        if pause:
            _time.sleep(pause)
        with lock:
            gap = _HOST_GAP - (_time.monotonic() - _HOST_LAST.get(host, 0.0))
            if gap > 0:
                _time.sleep(gap)
            _HOST_LAST[host] = _time.monotonic()
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        if getattr(r, "status_code", 200) == 429 and attempt < 2:
            continue
        r.raise_for_status()
        return r.text
    return ""


def _article_text(html: str) -> str:
    """The article itself, not the page around it.

    Uses trafilatura, a library built specifically to separate a news story from its
    menus, ads, "related stories" and footers, with tables kept (roundups are often
    tables) and recall favoured over precision. Falls back to plain page text if it
    cannot isolate the article.

    There is deliberately NO length cut-off here. A weekly roundup listing 25 deals
    is long, and the companies at the bottom are as real as the ones at the top.
    """
    try:
        import trafilatura
        text = trafilatura.extract(html, include_tables=True, include_comments=False,
                                   include_links=False, favor_recall=True) or ""
        if len(text) >= 400:
            return text
    except Exception as exc:                                    # noqa: BLE001
        log.debug("trafilatura unavailable or failed: %s", exc)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup
    return re.sub(r"[ \t]+", " ", main.get_text("\n")).strip()


# Safety ceiling only — far above any real article — to stop a malformed page from
# feeding megabytes into the reader. Never reached by a genuine story.
_MAX_ARTICLE_CHARS = 120_000


def _enrich_from_article(sig: RawSignal) -> RawSignal:
    """Download the full article and keep all of its text.
    A failure here never kills the signal; the reader flags it as an incomplete read."""
    try:
        html = _fetch(sig.url)
        text = _article_text(html)
        if text and len(text) > len(sig.body or ""):
            sig.body = text[:_MAX_ARTICLE_CHARS]
        from . import browser
        sig.raw["paywalled"] = browser.is_paywalled(sig.body or "")
        sig.raw["needs_browser"] = (not sig.raw["paywalled"]
                                    and browser.looks_cut_short(sig.body or "", html))
        li = LI_RE.search(html)
        if li and not sig.linkedin_url:
            sig.linkedin_url = li.group(0)
    except Exception as exc:                                    # noqa: BLE001
        # Refused, timed out, or a redirect page (Google News links redirect with
        # JavaScript). A real browser can usually still open it, so hand it over.
        log.warning("article fetch failed %s: %s — will try the browser", sig.url, exc)
        sig.raw["paywalled"] = False
        sig.raw["needs_browser"] = True
    return sig


# Set by collect(): returns True for an article already read in an earlier run.
# Feeds show the same recent items for hours, so without this every run would
# re-download ~400 pages it has already read — slow, and the fastest way to get
# blocked by a news site.
_already_read = None


def _enrich_many(sigs: list[RawSignal], workers: int = 3) -> list[RawSignal]:
    """Fetch article bodies in parallel.

    Sequential fetching dominated run time (~1s x 40 articles), and GitHub Actions
    bills per minute started — so this is the difference between a 2-minute and a
    1-minute run, which halves monthly minute consumption.
    """
    if not sigs:
        return []
    new = [s for s in sigs if not (_already_read and _already_read(s.signal_id()))]
    if new:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_enrich_from_article, new))     # fills each sig.body in place
        _expand_in_browser(new)
    for s in new:
        if s.raw.get("paywalled"):
            FETCH_STATS["paywalled"] += 1
    return sigs


# Counters for the email footer; reset at the start of each run by collect().
FETCH_STATS = {"browser_opened": 0, "expanded": 0, "paywalled": 0}
SOURCE_ERRORS: dict[str, str] = {}      # source id -> why it failed this run
_BROWSER = {"enabled": True, "max_pages": 15, "used": 0, "deadline": None}


def _expand_in_browser(sigs: list[RawSignal]) -> None:
    """Re-read, in a real browser, any article whose text still looks cut short —
    clicking "read more" / "show more" so the whole article is captured."""
    if not _BROWSER["enabled"]:
        return
    todo = [s for s in sigs if s.raw.get("needs_browser") and s.url]
    if not todo:
        return
    # The page limit and time slice are shared by ALL sources in a run. (They used
    # to apply per source, so one run could try 100+ pages and blow the time limit.)
    import time as _time
    left = _BROWSER["max_pages"] - _BROWSER["used"]
    dl = _BROWSER["deadline"]
    if left <= 0 or (dl is not None and _time.monotonic() > dl):
        return
    todo = todo[:left]
    _BROWSER["used"] += len(todo)
    from . import browser
    got = browser.fetch_full_pages([s.url for s in todo], _article_text,
                                   max_pages=left, deadline=dl)
    for s in todo:
        r = got.get(s.url)
        if not r:
            continue
        FETCH_STATS["browser_opened"] += 1
        if r["paywalled"]:
            s.raw["paywalled"] = True
        if r["text"] and len(r["text"]) > len(s.body or ""):
            s.body = r["text"][:_MAX_ARTICLE_CHARS]
            FETCH_STATS["expanded"] += 1


def _entry_to_signal(source: dict[str, Any], e: Any, strip_publisher: bool = False) -> RawSignal:
    published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc) \
        if getattr(e, "published_parsed", None) else datetime.now(timezone.utc)
    title = getattr(e, "title", "") or ""
    publisher = ""
    if strip_publisher:
        pm = re.search(r"\s+-\s+([^-]{2,40})$", title)
        publisher = pm.group(1).strip() if pm else ""
        # Google News appends " - Publisher"; strip it before company extraction.
        title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
    # Feeds that carry the full article (Entrackr does) put it in `content`.
    body = ""
    for c in getattr(e, "content", None) or []:
        v = c.get("value") if isinstance(c, dict) else getattr(c, "value", "")
        if v and len(v) > len(body):
            body = v
    if not body:
        body = getattr(e, "summary", "") or ""
    body = re.sub(r"\s+", " ", re.sub("<[^<]+?>", " ", body)).strip()
    sig = RawSignal(source_id=source["id"], title=title, url=getattr(e, "link", "") or "",
                     published_at=published, body=body[:_MAX_ARTICLE_CHARS],
                     company_name=_guess_company(title), signal_type="funding")
    if publisher:
        sig.raw["publisher"] = publisher
    return sig


def ingest_rss(source: dict[str, Any]) -> list[RawSignal]:
    feed = _parse_feed(source["url"])
    sigs = [_entry_to_signal(source, e) for e in feed.entries[:60]]
    # Download (only new articles), then open any that still look cut short in the
    # browser and click "read more".
    return _enrich_many(sigs)


def ingest_html_list(source: dict[str, Any]) -> list[RawSignal]:
    """Grant awardee tables, accelerator/portfolio directories."""
    out: list[RawSignal] = []
    try:
        soup = BeautifulSoup(_fetch(source["url"]), "html.parser")
    except Exception as exc:                                    # noqa: BLE001
        log.warning("html source failed %s: %s", source["id"], exc)
        return out
    for row in soup.select(source.get("row_selector", "tr"))[:200]:
        text = re.sub(r"\s+", " ", row.get_text(" ")).strip()
        if len(text) < 3:
            continue
        link = row.find("a", href=True)
        out.append(RawSignal(
            source_id=source["id"],
            title=text[:180],
            url=(link["href"] if link else source["url"]),
            published_at=datetime.now(timezone.utc),
            body=text,
            company_name=text.split("—")[0].split("|")[0].strip()[:60],
            signal_type="grant" if source["id"] in {"idex", "birac", "tdb"} else "accelerator",
        ))
    return out


def ingest_google_news(source: dict[str, Any]) -> list[RawSignal]:
    """Google News search feeds — free, keyless, and they reach publishers whose own
    feeds are closed or blocked (VCCircle, Moneycontrol) via `site:` queries.

    Google News links redirect to the publisher with JavaScript, which a plain
    download cannot follow; those articles are opened in the browser instead.
    """
    days = int(source.get("max_age_days", 3))
    seen_links: set[str] = set()
    sigs: list[RawSignal] = []
    for query in source.get("queries", []):
        q = query if "when:" in query else f"{query} when:{days}d"
        url = ("https://news.google.com/rss/search?q=" + requests.utils.quote(q)
               + f"&hl={source.get('lang', 'en-IN')}&gl={source.get('country', 'IN')}"
               + f"&ceid={source.get('country', 'IN')}:{source.get('lang', 'en-IN')[:2]}")
        try:
            feed = _parse_feed(url)
        except Exception as exc:                                # noqa: BLE001
            log.warning("google news query failed (%s): %s", query, exc)
            continue
        for e in feed.entries[:30]:
            sig = _entry_to_signal(source, e, strip_publisher=True)
            if sig.url and sig.url not in seen_links:
                seen_links.add(sig.url)
                sigs.append(sig)
    return _enrich_many(sigs)


def ingest_mca(source: dict[str, Any]) -> list[RawSignal]:
    """MCA PAS-3 (return of allotment) — equity issuance 1-3 weeks before press.

    MCA has no open bulk API; supply filings via a licensed data vendor or an
    authorised export and drop them as JSON into MCA_FEED_PATH. The adapter shape
    is fixed so the rest of the pipeline is unaffected by where they come from.
    """
    import json
    path = os.getenv("MCA_FEED_PATH", "")
    if not path or not os.path.exists(path):
        log.info("mca adapter idle — no MCA_FEED_PATH configured")
        return []
    rows = json.loads(open(path, encoding="utf-8").read())
    return [RawSignal(
        source_id=source["id"],
        title=f"{r.get('company_name')} allotment filing ({r.get('form_type', 'PAS-3')})",
        url=r.get("filing_url", ""),
        published_at=datetime.fromisoformat(r["filed_at"]),
        body=json_dumps_safe(r),
        company_name=r.get("company_name", ""),
        website=r.get("website", ""),
        signal_type="mca",
        raw=r,
    ) for r in rows]


def ingest_x_search(source: dict[str, Any]) -> list[RawSignal]:
    """X/Twitter recent search for stealth & seed announcements."""
    token = os.getenv("X_BEARER_TOKEN", "")
    if not token:
        log.info("x adapter idle — no X_BEARER_TOKEN")
        return []
    r = requests.get(
        "https://api.twitter.com/2/tweets/search/recent",
        params={"query": source["query"], "max_results": 50,
                "tweet.fields": "created_at,author_id,entities"},
        headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    r.raise_for_status()
    return [RawSignal(
        source_id=source["id"],
        title=t["text"][:180],
        url=f"https://x.com/i/status/{t['id']}",
        published_at=datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")),
        body=t["text"],
        signal_type="social",
        raw=t,
    ) for t in r.json().get("data", [])]


def json_dumps_safe(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)[:8000]
    except Exception:                                           # noqa: BLE001
        return str(obj)[:8000]


ADAPTERS: dict[str, Callable[[dict[str, Any]], list[RawSignal]]] = {
    "rss": ingest_rss,
    "google_news": ingest_google_news,
    "html_list": ingest_html_list,
    "mca": ingest_mca,
    "x_search": ingest_x_search,
}


def collect(cfg: dict[str, Any], store: Any = None) -> list[RawSignal]:
    """Poll every enabled source, recording per-source yield.

    A source that quietly returns nothing forever — a moved RSS feed, a page whose
    markup changed under a CSS selector — is invisible without this. It is also the
    single most likely way this tool degrades, because the AE cannot notice the
    absence of an article they never saw.
    """
    global _already_read
    _already_read = store.is_signal_seen if store is not None else None
    for k in FETCH_STATS:
        FETCH_STATS[k] = 0
    SOURCE_ERRORS.clear()
    _BROWSER["enabled"] = bool(cfg.get("run", {}).get("browser_expand", True))
    _BROWSER["max_pages"] = int(cfg.get("run", {}).get("browser_max_pages", 15))
    _BROWSER["used"] = 0
    import time as _time
    _BROWSER["deadline"] = _time.monotonic() + 60 * float(
        cfg.get("run", {}).get("browser_time_budget_minutes", 2.5))
    signals: list[RawSignal] = []
    for source in cfg.get("sources", []):
        if source.get("enabled") is False:
            continue
        fn = ADAPTERS.get(source["type"])
        if not fn:
            log.warning("no adapter for source type %s", source["type"])
            continue
        got: list[RawSignal] = []
        try:
            got = fn(source)
            log.info("ingest %s -> %d signals", source["id"], len(got))
        except Exception as exc:                                # noqa: BLE001
            log.error("source %s failed: %s", source["id"], exc)
            SOURCE_ERRORS[source["id"]] = str(exc)[:200]
        if store is not None:
            store.record_source(source["id"], len(got))
        signals.extend(got)
    return signals
