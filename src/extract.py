"""Article reading — the layer that replaces the AE's manual scan of each source.

DESIGN PRINCIPLE: RECALL FIRST.

The competing AEs reading these same sources by hand do not pattern-match; they read
and understand. So does this. An LLM reads each article in full and lists every company
that received capital, however the article phrases it.

Regex extraction is kept, but ONLY as a safety net that runs alongside — the union of
both is used. A hand-written pattern cannot anticipate "announced a round", "has closed
its Series A", "picked up an undisclosed sum", bullet tables, or a company named one
sentence before the amount. Missing a company is the one failure this tool cannot have,
so a duplicate from two extractors is cheap and deduplication collapses it.

Cost is controlled by batching several articles into one call and by a deliberately
permissive pre-filter, not by narrowing what counts as a funding event.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

from .models import RawSignal, collapse_doubled_name

log = logging.getLogger(__name__)

# Pre-filter. DELIBERATELY PERMISSIVE — its only job is to skip articles with no
# financial content at all (a product launch, an opinion piece). Anything that could
# conceivably be about money goes to the model. A false positive here costs a fraction
# of a call; a false negative loses a company forever.
# NOTE: no blanket \b wrapper. A trailing \b after "rais" never matches "raised", and
# a \b around "$" never matches at all — both were silent, and the second one skipped
# weekly funding roundups entirely. Each alternative carries its own boundaries.
MONEY_HINT = re.compile(
    r"rais(?:e|es|ed|ing)"
    r"|fund(?:s|ed|ing)?\b"
    r"|invest(?:s|ed|ing|ment|or|ors)?\b"
    r"|\bseed\b|\bpre-?seed\b|\bseries\s+[a-f]\b|\bround\b|\bcapital\b"
    r"|\bback(?:s|ed|er|ers|ing)\b|\bvaluation\b|\bcheque\b|\bticket size\b"
    r"|\bstealth\b|\bgrant(?:s|ed)?\b|\ballotment\b|\bcorpus\b"
    r"|\bcrore\b|\blakh\b|\bmillion\b|\bbillion\b|\bmn\b|\bbn\b"
    r"|[$₹]|\bUSD\b|\bINR\b|\bRs\.?\b"
    r"|\bangel\b|\bventure\b|\bVC\b|\bdeal(?:s)?\b"
    r"|\bclos(?:e|es|ed|ing)\b|\bsecur(?:e|es|ed)\b|\bbag(?:s|ged)\b"
    r"|\bnet(?:s|ted)\b|\bland(?:s|ed)\b|\bpick(?:s|ed)\s+up\b"
    r"|\bmop(?:s|ped)\s+up\b|\bundisclosed\b|\bsum\b|\bcheck size\b"
    r"|\bacceler(?:ator|ates)\b|\bcohort\b|\bbatch\b|\bdemo day\b", re.I)


# Company results, IPO and stock stories mention money constantly but are never a new
# funding round. Skipped ONLY when the headline also has no funding verb, so
# "X posts Q2 profit, raises Rs 20 Cr" is still read.
NOT_A_RAISE_HEADLINE = re.compile(
    r"\bQ[1-4]\b|\bFY\s?\d{2}\b|\brevenue\b|\bprofit\b|\bloss(?:es)?\b|\bEBITDA\b|"
    r"\bIPO\b|\bshares?\b|\bstock\b|\blisting\b|\bappoints?\b|\belevates?\b|"
    r"\blaunch(?:es|ed)?\b|\bacquires?\b|\blayoffs?\b|\bjob cuts\b", re.I)
FUNDING_HEADLINE = re.compile(
    r"rais(?:e|es|ed|ing)|secur(?:e|es|ed)|\bbag(?:s|ged)?\b|\bnet(?:s|ted)\b|"
    r"\bland(?:s|ed)\b|funding|\bround\b|\bseed\b|series\s+[a-f]|stealth|"
    r"\binvest|\bbacked\b|\bbacks\b|\bgrant\b|\bcheque|\bmops?\b|\bpicks? up\b", re.I)


def worth_reading(sig: RawSignal) -> bool:
    title = sig.title or ""
    if NOT_A_RAISE_HEADLINE.search(title) and not FUNDING_HEADLINE.search(title):
        return False
    blob = f"{title} {(sig.body or '')[:3000]}"
    return bool(MONEY_HINT.search(blob))


EXTRACTION_SYSTEM = """You are reading Indian startup news to find every company that \
has just received capital. You are replacing a human analyst who reads these articles \
end to end, and you are competing against other analysts doing the same. Missing a \
company is the only unacceptable error.

Read the WHOLE article. Weekly funding roundups list many companies in a row, sometimes \
as prose, sometimes as bullets or a table — extract every one of them.

EXTRACT a company when the article says it received capital, in any phrasing:
- "raised", "raises", "secured", "bagged", "netted", "landed", "mopped up", "picked up"
- "announced a round", "closed its Series A", "has closed", "concluded a raise"
- "emerged from stealth with", "came out of stealth", "launched with backing from"
- "received a grant", "was awarded", "selected for <accelerator> batch"
- "in talks" ONLY if the article states the round has actually closed
- Undisclosed amounts still count. "raised an undisclosed sum" is a valid extraction.
- A company named in an earlier sentence and referred to later as "the startup", "the \
company", "it" — resolve the reference and extract the company.

DO NOT extract:
- Investors, VC firms, angels, accelerators, or funds — the entities GIVING the money
- A VC or fund raising its OWN pool ("Blume raised Rs 500 crore for its maiden fund")
- Acquirers or acquisition targets, unless the target separately raised a round
- Companies mentioned only as comparisons, customers, partners, or competitors
- The publication itself

For each company return what the article states. Use null for anything it does not say \
— never invent an amount, city, or investor.

If the article contains no funding event at all, return an empty list. Do not force a \
result. But when a mention is borderline, INCLUDE it: a human reviews these, and a \
false positive costs seconds while a miss costs the account."""

EXTRACTION_USER = """Extract every company that received capital in the article(s) below.

{articles}

Return JSON only, in this exact shape:
{{"articles": [{{"article_index": <int>, "companies": [
  {{"company_name": "<as written>",
    "amount": "<e.g. $2.4M or Rs 15 crore, or null>",
    "round_type": "<Pre-Seed|Seed|Angel|Pre-Series A|Series A|Pre-Series B|Series B|Bridge|Grant|Accelerator|Debt|Extension|Undisclosed>",
    "city": "<Indian city if stated, else null>",
    "hq_country": "<country where the company is headquartered, if stated, else null>",
    "india_link": "<if based outside India: any India team, office, entity or Indian founders the article mentions, else null>",
    "sector": "<one short phrase if stated, else null>",
    "investors": "<comma-separated if stated, else null>",
    "evidence": "<the sentence or fragment that states the raise>"}}
]}}]}}"""

EXTRACTION_SCHEMA_HINT = {
    "articles": [{"article_index": 0, "companies": [{
        "company_name": "", "amount": None, "round_type": "Seed", "city": None,
        "hq_country": None, "india_link": None, "sector": None, "investors": None, "evidence": ""}]}]
}


def _format_batch(batch: Sequence[RawSignal]) -> str:
    parts = []
    for i, s in enumerate(batch):
        parts.append(
            f"--- ARTICLE {i} ---\n"
            f"SOURCE: {s.source_id}\n"
            f"HEADLINE: {s.title}\n"
            f"TEXT:\n{s.body or ''}\n")
    return "\n".join(parts)


def _parse(raw: str) -> Any:
    """Read the model's JSON answer, which may be an object {...} or a list [...],
    possibly wrapped in a code fence or a sentence of commentary."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    # Cut out the JSON, using whichever bracket opens first.
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in model answer")
    start = min(starts)
    end = raw.rfind("}" if raw[start] == "{" else "]")
    if end <= start:
        raise ValueError("incomplete JSON in model answer")
    return json.loads(raw[start:end + 1])


class QuotaExhausted(RuntimeError):
    """The model refused on rate/quota grounds.

    This must NOT be treated as 'no companies in these articles'. The run has to stop
    reading and leave the remaining articles unprocessed so the next run retries them
    — otherwise a midday quota block silently loses a day of deals.
    """


_QUOTA_MARKERS = ("quota", "rate limit", "resource_exhausted", "429",
                  "too many requests", "insufficient_quota")


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _QUOTA_MARKERS)


def _entries_from_answer(data: Any, n_articles: int) -> list[dict[str, Any]] | None:
    """Turn the model's answer into per-article entries, whatever shape it chose.

    Asked for {"articles": [{"article_index": 0, "companies": [...]}]}, models
    sometimes return the bare list, or (for one article) just the companies. All
    of those are understood. Anything unrecognisable returns None, which the caller
    treats as "not read" so the article is retried — never as "no companies".
    """
    if isinstance(data, dict):
        if isinstance(data.get("articles"), list):
            return data["articles"]
        if n_articles == 1 and isinstance(data.get("companies"), list):
            return [{"article_index": 0, "companies": data["companies"]}]
        if n_articles == 1 and data.get("company_name"):
            return [{"article_index": 0, "companies": [data]}]
        return None
    if isinstance(data, list):
        if not data:
            return []
        if all(isinstance(e, dict) and "article_index" in e for e in data):
            return data
        if n_articles == 1 and all(isinstance(e, dict) for e in data):
            return [{"article_index": 0, "companies": data}]
    return None


def _results_from_entries(entries: list[dict[str, Any]], n: int) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = [[] for _ in range(n)]
    for e in entries:
        if not isinstance(e, dict):
            continue
        i = e.get("article_index")
        if isinstance(i, str) and i.isdigit():
            i = int(i)
        if isinstance(i, int) and 0 <= i < n:
            comps = e.get("companies") or []
            out[i] = [c for c in comps if isinstance(c, dict) and c.get("company_name")]
    return out


def extract_batch(batch: Sequence[RawSignal], cfg: dict[str, Any],
                  call_llm) -> tuple[list[list[dict[str, Any]]], bool]:
    """Returns (per-article company lists, ok).

    `ok` is False when the call failed for a reason that means "not read", so the
    caller can leave these articles unprocessed for a later retry. An empty list with
    ok=True genuinely means "read it, found no funding events".
    """
    results: list[list[dict[str, Any]]] = [[] for _ in batch]
    if not batch:
        return results, True
    user = EXTRACTION_USER.format(articles=_format_batch(batch))
    try:
        data = _parse(call_llm(EXTRACTION_SYSTEM, user, cfg))
    except Exception as exc:                                    # noqa: BLE001
        if _is_quota_error(exc):
            if "time budget" in str(exc):
                log.info("reading paused: this run's time is used up — the rest "
                         "are read next run")
            else:
                log.error("extraction stopped: model quota/rate limit hit — %s", exc)
            raise QuotaExhausted(str(exc)) from exc
        log.error("article extraction failed for batch of %d: %s", len(batch), exc)
        return results, False
    entries = _entries_from_answer(data, len(batch))
    if entries is None:
        log.warning("unrecognised answer format for batch of %d — will retry next run",
                    len(batch))
        return results, False
    return _results_from_entries(entries, len(batch)), True


# ------------------------------------------------------------------ full reading
#
# How an article is read, so that nothing in it is skipped:
#   - Short articles are grouped a few to a request (cheap, and short texts are easy).
#   - A long article (a weekly roundup) gets its OWN request, never shared — a model
#     reading many long texts at once skims and drops items from long lists.
#   - A very long article is split into overlapping sections and each section is
#     read in full. Overlap means a company straddling a boundary is still seen.
#   - Completeness check: when an article states how many deals it covers ("21
#     deals this week") and fewer were found, it is read again, given the list
#     already found and told to find the rest.

SHORT_ARTICLE = 6000           # chars; at or below this, articles share a request
SHORT_GROUP_CHARS = 20000      # total text per shared request
MAX_GROUP_ARTICLES = 4         # at most this many articles share one request
SECTION_CHARS = 9000           # longest single section sent to the model
SECTION_OVERLAP = 800

STATED_COUNT = re.compile(
    r"\b(\d{1,3})\s+(?:funding\s+)?(?:deals|startups|companies|rounds|transactions|"
    r"ventures|firms)\b", re.I)

RECHECK_SYSTEM = EXTRACTION_SYSTEM + """

THIS IS A SECOND READ. The article states it covers more funding deals than were \
found the first time. Read it again, line by line, including any table or bulleted \
list, and return ONLY the companies NOT already in the "already found" list. Do not \
repeat companies already found. If you are certain none were missed, return an empty \
list."""


def stated_deal_count(sig: RawSignal) -> int:
    """'Indian startups raised $142M across 21 deals' -> 21. 0 if not stated."""
    m = STATED_COUNT.search(f"{sig.title} {(sig.body or '')[:800]}")
    return int(m.group(1)) if m else 0


def split_sections(text: str) -> list[str]:
    """Overlapping sections, cut on paragraph or sentence boundaries."""
    if len(text) <= SECTION_CHARS:
        return [text]
    sections, start = [], 0
    while start < len(text):
        end = min(start + SECTION_CHARS, len(text))
        if end < len(text):
            cut = max(text.rfind("\n", start + SECTION_CHARS // 2, end),
                      text.rfind(". ", start + SECTION_CHARS // 2, end))
            if cut > start:
                end = cut + 1
        sections.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - SECTION_OVERLAP, start + 1)
    return sections


def _section_sig(parent: RawSignal, text: str, part: int, total: int) -> RawSignal:
    title = parent.title if total == 1 else f"{parent.title} (part {part} of {total})"
    return RawSignal(source_id=parent.source_id, title=title, url=parent.url,
                     published_at=parent.published_at, body=text,
                     company_name=parent.company_name, signal_type=parent.signal_type)


def _union(found: list[dict[str, Any]], more: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .models import normalize_company_name
    seen = {normalize_company_name(c.get("company_name")) for c in found}
    for c in more:
        k = normalize_company_name(c.get("company_name"))
        if k and k not in seen:
            found.append(c)
            seen.add(k)
    return found


def plan_requests(articles: Sequence[RawSignal]) -> list[list[RawSignal]]:
    """Group short articles; give every long article its own request(s).

    Long articles and anything stating a deal count (roundups) are read FIRST, so if
    a run's request budget runs out it is never the roundup that waits.
    """
    # Individual funding stories first, newest first: they are the early signal.
    # Roundups go LAST — every company in them was usually reported individually
    # days earlier, so a roundup is a backstop, not the main source.
    # Articles left unfinished by the previous run go FIRST — otherwise new stories
    # keep jumping ahead and the same leftovers are carried over run after run.
    articles = sorted(articles, key=lambda a: (not (a.raw or {}).get("carried"),
                                               stated_deal_count(a) > 0,
                                               -a.published_at.timestamp()))
    plan: list[list[RawSignal]] = []
    group: list[RawSignal] = []
    size = 0
    for a in articles:
        n = len(a.body or "")
        if n > SHORT_ARTICLE:
            # Send any short articles already queued FIRST, so the reading order
            # above is kept — a long roundup must never jump ahead of them.
            if group:
                plan.append(group)
                group, size = [], 0
            plan.append([a])
            continue
        if group and (size + n > SHORT_GROUP_CHARS or len(group) >= MAX_GROUP_ARTICLES):
            plan.append(group)
            group, size = [], 0
        group.append(a)
        size += n
    if group:
        plan.append(group)
    return plan


def read_articles(articles: Sequence[RawSignal], cfg: dict[str, Any], call_llm,
                  call_budget: int) -> tuple[dict[str, list[dict[str, Any]]],
                                             dict[str, str], dict[str, int], int]:
    """Read every article in full and list every company that raised.

    Returns (companies_by_article, status_by_article, notes, calls_left).
    status is "read" only when every part of the article was read successfully.
    Anything else ("not_read", "quota") leaves the article queued for the next run.
    notes holds completeness-check counts for the email.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    status: dict[str, str] = {}
    notes = {"rechecked": 0, "recovered": 0, "short_lists": 0}

    def ask(system: str, batch: list[RawSignal], extra: str = "") -> tuple[list, bool]:
        nonlocal call_budget
        if call_budget <= 0:
            raise QuotaExhausted("per-run request budget spent")
        call_budget -= 1
        if extra:
            user = EXTRACTION_USER.format(articles=_format_batch(batch)) + extra
            try:
                data = _parse(call_llm(system, user, cfg))
            except Exception as exc:                            # noqa: BLE001
                if _is_quota_error(exc):
                    raise QuotaExhausted(str(exc)) from exc
                return [[] for _ in batch], False
            entries = _entries_from_answer(data, len(batch))
            if entries is None:
                return [[] for _ in batch], False
            return _results_from_entries(entries, len(batch)), True
        return extract_batch(batch, cfg, call_llm)

    try:
        for group in plan_requests(articles):
            if len(group) > 1:
                res, ok = ask(EXTRACTION_SYSTEM, group)
                for a, comps in zip(group, res):
                    found[a.signal_id()] = comps
                    status[a.signal_id()] = "read" if ok else "not_read"
                continue

            art = group[0]
            sid = art.signal_id()
            sections = split_sections(art.body or "")
            comps: list[dict[str, Any]] = []
            ok_all = True
            for i, text in enumerate(sections, 1):
                res, ok = ask(EXTRACTION_SYSTEM,
                              [_section_sig(art, text, i, len(sections))])
                ok_all &= ok
                comps = _union(comps, res[0])

            stated = stated_deal_count(art)
            # A roundup that says "...and others" does not name every company, so a
            # second read cannot find more; skip it rather than waste requests.
            names_all = not re.search(r"\band others\b|\bamong others\b",
                                      art.body or "", re.I)
            if ok_all and names_all and stated >= 3 and len(comps) < stated:
                notes["rechecked"] += 1
                already = ", ".join(c["company_name"] for c in comps) or "none"
                before = len(comps)
                for i, text in enumerate(sections, 1):
                    res, ok = ask(RECHECK_SYSTEM,
                                  [_section_sig(art, text, i, len(sections))],
                                  extra=f"\n\nALREADY FOUND: {already}\n"
                                        f"The article states {stated} deals.")
                    comps = _union(comps, res[0])
                notes["recovered"] += len(comps) - before
                if len(comps) < stated:
                    notes["short_lists"] += 1
                    art.raw["shortfall"] = (stated, len(comps))

            found[sid] = comps
            status[sid] = "read" if ok_all else "not_read"
    except QuotaExhausted:
        for a in articles:
            status.setdefault(a.signal_id(), "quota")
    return found, status, notes, call_budget


def body_looks_truncated(sig: RawSignal) -> bool:
    """A failed page fetch leaves only the RSS summary — often a sentence or two.

    A roundup reduced to its summary has no companies left to find, and the failure
    is otherwise invisible. Anything this short is treated as an incomplete read.
    """
    body = (sig.body or "").strip()
    if len(body) >= 600:
        return False
    # Short is only a problem when the article promises more than it shows.
    return bool(MONEY_HINT.search(f"{sig.title} {body}"))


def to_signals(parent: RawSignal, companies: Sequence[dict[str, Any]]) -> list[RawSignal]:
    """Turn extracted companies into one signal each, carrying article context."""
    out: list[RawSignal] = []
    context = f"{parent.title} {(parent.body or '')[:400]}"
    for c in companies:
        name = collapse_doubled_name((c.get("company_name") or "").strip())
        if len(name) < 2:
            continue
        evidence = (c.get("evidence") or "").strip()
        bits = [f"{name} raised {c['amount']}" if c.get("amount") else f"{name} raised funding"]
        if c.get("round_type") and c["round_type"] != "Undisclosed":
            bits.append(f"in a {c['round_type']} round")
        if c.get("investors"):
            bits.append(f"from {c['investors']}")
        if c.get("city"):
            bits.append(f"The company is based in {c['city']}, India.")
        if c.get("sector"):
            bits.append(f"Sector: {c['sector']}.")
        stated = " ".join(bits)

        s = RawSignal(
            source_id=parent.source_id,
            # Title carries the asserted deal, so headline-level gates read a
            # statement that genuinely describes THIS company's raise.
            title=f"{name} raised {c.get('amount') or 'funding'}"
                  f"{' (' + c['round_type'] + ')' if c.get('round_type') else ''}",
            url=parent.url,
            published_at=parent.published_at,
            body=f"{stated}\n{evidence}\n\n[Article context] {context}",
            company_name=name,
            signal_type=parent.signal_type,
            from_body=True,
        )
        s.raw = {"extracted": dict(c), "parent_title": parent.title,
                 "publisher_url": (parent.raw or {}).get("publisher_url", ""),
                 "publisher": (parent.raw or {}).get("publisher", "")}
        out.append(s)
    return out


def merge_signals(*groups: Sequence[RawSignal]) -> list[RawSignal]:
    """Union several extractors by normalised company name, keeping the richest entry.

    The LLM and the regex net disagree at the margins; taking the union means a company
    only disappears if BOTH missed it.
    """
    from .models import normalize_company_name
    best: dict[str, RawSignal] = {}
    for group in groups:
        for s in group:
            key = normalize_company_name(s.company_name)
            if not key:
                continue
            existing = best.get(key)
            if existing is None or len(s.body or "") > len(existing.body or ""):
                best[key] = s
    return list(best.values())
