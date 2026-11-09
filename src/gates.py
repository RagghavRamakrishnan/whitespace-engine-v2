"""Deterministic hard gates. The LLM is NOT allowed to make these calls.

Every gate returns a GateResult with a human-readable reason so the daily drop
digest tells you exactly why a company never reached your inbox.
"""
from __future__ import annotations

import re
from typing import Any

from .models import GateResult, RawSignal

FUNDING_PATTERNS = [
    # Present AND past tense. Roundup entries are written in the past ("Medha bagged
    # Rs 15 crore"), so a present-only list silently loses most roundup companies.
    r"\braises?\b", r"\braised\b", r"\bsecures?\b", r"\bsecured\b",
    r"\bbags?\b", r"\bbagged\b", r"\bnets?\b", r"\bnetted\b",
    r"\blands?\b", r"\blanded\b", r"\bpicked up\b", r"\bpicks up\b",
    r"\bmopped up\b", r"\bmops up\b", r"\breceived\b", r"\breceives\b",
    r"\bfunding\b", r"\bfunded\b", r"\binvestment from\b",
    r"\bpre-?seed\b", r"\bseed round\b", r"\bseries [a-c]\b",
    r"\bangel (?:round|investors?|funding)\b",
    r"\bgrant\b", r"\bawarded\b", r"\bbacked by\b", r"\ballotment\b",
    # Stealth exits are capital events in practice — they almost always announce a
    # previously-unreported round, and they are prime 1-50 headcount territory.
    r"\bemerges? from stealth\b", r"\bcomes? out of stealth\b", r"\bexits? stealth\b",
    r"\bout of stealth\b", r"\bmops up\b", r"\bcloses? (?:a |its )?\$?\d",
]
# Parsed numerically rather than by pattern: "$900K" and "$900M" differ only in the
# unit, so a regex on the digits alone misfires. Anything at or above USD 100M (or
# ~INR 800 crore) belongs to a company far larger than 50 people.
MONEY_RE = re.compile(
    r"(?:\$|usd|inr|rs\.?|₹)\s?([\d.,]+)\s?(k|thousand|mn|m|million|bn|billion|cr|crore|lakh)?"
    r"|([\d.,]+)\s?(crore|cr|lakh|million|mn|billion|bn)\b", re.I)
# INR units converted to approximate USD: 1 crore = 10,000,000 INR ~ USD 120,000;
# 1 lakh = 100,000 INR ~ USD 1,200. Rates move, but only the order of magnitude
# matters for a 100M threshold.
_UNIT_USD = {"k": 1e3, "thousand": 1e3, "mn": 1e6, "m": 1e6, "million": 1e6,
             "bn": 1e9, "billion": 1e9, "cr": 1.2e5, "crore": 1.2e5, "lakh": 1.2e3,
             None: 1.0, "": 1.0}
MEGA_ROUND_USD = 100_000_000


def _largest_amount_usd(text: str) -> tuple[float, str]:
    """Biggest money figure in the lead, roughly converted to USD."""
    best, best_raw = 0.0, ""
    for m in MONEY_RE.finditer(text):
        num = (m.group(1) or m.group(3) or "").replace(",", "")
        unit = (m.group(2) or m.group(4) or "").lower() or None
        if not num or num in (".",):
            continue
        try:
            val = float(num) * _UNIT_USD.get(unit, 1.0)
        except ValueError:
            continue
        if val > best:
            best, best_raw = val, m.group(0).strip()
    return best, best_raw

AMOUNT_RE = re.compile(
    r"(?:(?:\$|usd|inr|rs\.?|₹)\s?[\d.,]+\s?(?:mn|m|million|bn|billion|cr|crore|lakh)?)"
    r"|(?:[\d.,]+\s?(?:crore|cr|lakh|million|mn)\b)", re.I)
ROUND_RE = re.compile(
    r"\b(pre-?seed|seed|angel|pre-?series\s?a|series\s?[a-c]|bridge|grant|debt|extension)\b", re.I)
# Explicit late-stage language is a hard reject: those companies are far past 1-50.
# Explicit non-India HQ markers. Only decisive when no India hub is also present.
FOREIGN_HQ_RE = re.compile(
    r"\b(san francisco|new york|london|singapore|tokyo|berlin|paris|sydney|toronto|"
    r"dubai|tel aviv|shanghai|beijing|seoul|boston|seattle|austin|silicon valley|"
    r"us-based|uk-based|japan-based|china-based|europe-based|singapore-based)\b", re.I)

LATE_STAGE_RE = re.compile(
    r"\b(series\s?[d-j]\b|growth round|pre-?ipo|ipo|mezzanine|secondary sale|"
    r"unicorn|late-stage)\b", re.I)


def _text(sig: RawSignal) -> str:
    """Gates read the article LEAD only — never the full scraped page. Page furniture
    (nav, 'related articles', footers) is full of stray 'India' and 'AI' mentions that
    would otherwise pass every keyword gate."""
    return sig.lead_text.lower()


# Entities that are never prospects: the publisher itself, funds, accelerators,
# government bodies. These appear as "company names" when headline parsing fails.
# Verbs where the entity named FIRST is the investor, acquirer or partner — never the
# company being funded. "Microsoft invests in X" is a story about X, not Microsoft.
INVESTOR_ACTION_RE = re.compile(
    r"^\s*[\w&.\- ]{2,45}?\s+(?:invests?|invested|backs?|backed|leads?|led|acquires?|"
    r"acquired|buys?|bought|takes? over|picks? up a stake|partners? with|ties? up with|"
    r"joins? hands|commits?|pledges?|launches? a fund|sets? up a fund|to invest)\b", re.I)

# A VC or accelerator raising its OWN pool of capital. The headline reads exactly like
# a startup round ("Blume raises Rs 500 crore maiden fund") but the money is going TO an
# investor, not to a prospect. Detected by the OBJECT of the raise being a fund.
# NOTE the closing word boundary: without it, "fund" matches inside "fundING", and
# "raises funding" — one of the most common headline phrasings there is — would be
# misread as a VC raising its own pool.
FUND_RAISE_RE = re.compile(
    r"\b(?:raises?|raised|closes?|closed|announces?|launches?|unveils?|secures?|mops up)\b"
    r"([^.]{0,60}?)"
    r"\b(?:fund|funds|corpus|vehicle|AIF)\b", re.I)
# ...unless the fund is merely the INVESTOR in someone else's round.
_FUND_AS_INVESTOR = re.compile(r"\b(?:from|led by|backed by|by)\b", re.I)


def _is_fund_raising_itself(headline: str) -> bool:
    m = FUND_RAISE_RE.search(headline)
    if not m:
        return False
    return not _FUND_AS_INVESTOR.search(m.group(1))


NON_COMPANY_RE = re.compile(
    r"\b(yourstory|entrackr|inc42|vccircle|livemint|techcrunch|moneycontrol|economic times|"
    r"fund|funds|capital|ventures?|partners|vc firm|accelerator|incubator|angel network|"
    r"consortium|ministry|government|commission|authority|council|association|federation|"
    r"summit|conclave|awards?|report|survey|index)\b", re.I)


# Roundups and listicles: "Top 10 startups that raised funding this week". These parse
# as a company because a verb follows a capitalised phrase, but they are about many
# companies at once and name none of them as the subject.
LISTICLE_RE = re.compile(
    r"^\s*(?:top|these|the|india\'?s)\s+\d+\b"
    r"|\b\d+\s+(?:startups?|companies|firms|founders?|deals?)\b"
    r"|\b(?:funding|startup|deal|weekly|monthly)\s+(?:roundup|round-up|wrap|digest|recap)\b"
    r"|\bstartups? that (?:raised|secured|bagged)\b"
    r"|\b(?:this|last) week\b.*\braised\b"
    r"|\braised\b.*\b(?:this|last) week\b", re.I)


def gate_is_company(sig: RawSignal) -> GateResult:
    """Reject publishers, funds and institutions before anything else."""
    name = (sig.company_name or "").strip()
    if len(name) < 2:
        return GateResult(False, "no company name extracted")
    if LISTICLE_RE.search(sig.headline):
        return GateResult(False, "roundup/listicle covering many companies, not one prospect")
    if NON_COMPANY_RE.search(name):
        return GateResult(False, f"not an operating company: '{name[:60]}'")
    # A "name" that is really a headline fragment.
    if len(name.split()) > 6:
        return GateResult(False, f"company name looks like a headline: '{name[:60]}'")
    return GateResult(True, "looks like an operating company")


def gate_capital(sig: RawSignal, cfg: dict[str, Any]) -> GateResult:
    """Primary non-negotiable trigger: is this actually a capital-inflow event?

    The funding language must be in the HEADLINE. Body text is unreliable for this:
    "related stories" blocks and inline promos carry other companies' funding words,
    which is exactly how a Microsoft product announcement passes a naive keyword check.
    A real funding story always says so in its headline.
    """
    headline = sig.headline.lower()
    text = _text(sig)

    if sig.signal_type in ("grant", "accelerator", "mca"):
        return GateResult(True, "structural capital source", {"round_type": sig.signal_type})

    if not any(re.search(p, headline) for p in FUNDING_PATTERNS):
        return GateResult(False, "no capital-inflow language in the headline")

    if _is_fund_raising_itself(sig.headline):
        return GateResult(False, "a fund/VC raising its own capital pool, not a prospect")

    # The subject of an investing/acquiring verb is the investor, not the prospect.
    inv = INVESTOR_ACTION_RE.search(sig.headline)
    if inv:
        return GateResult(False, "headline subject is an investor/acquirer, not the "
                                 "company receiving capital")
    # Amounts are read from THIS company's own deal text only. Inherited article
    # context is excluded: a roundup's "$142 million across 21 deals" is the week's
    # total, and charging it to each company disqualifies the whole article.
    biggest, raw = _largest_amount_usd(headline)
    if biggest == 0:
        biggest, raw = _largest_amount_usd(sig.deal_text)
    if biggest >= MEGA_ROUND_USD:
        return GateResult(False, f"round size '{raw}' implies a company far above 50 staff")
    late = LATE_STAGE_RE.search(text)
    if late:
        return GateResult(False, f"late-stage capital event ('{late.group(0)}') — outside SMB band")
    own = sig.deal_text
    rnd = ROUND_RE.search(headline) or ROUND_RE.search(own)
    amt = AMOUNT_RE.search(headline) or AMOUNT_RE.search(own)
    round_type = (rnd.group(0).title() if rnd else "Undisclosed")
    accepted = [r.lower() for r in cfg["gates"]["capital"]["accepted_round_types"]]
    if rnd and rnd.group(0).lower().replace("-", " ") not in [a.replace("-", " ") for a in accepted]:
        return GateResult(False, f"round type '{round_type}' outside target stages")
    return GateResult(True, "capital event detected",
                      {"round_type": round_type,
                       "amount": amt.group(0).upper() if amt else "Undisclosed"})


def gate_india(sig: RawSignal, cfg: dict[str, Any], india_pct: float | None = None) -> GateResult:
    """Indian entity OR >60% of headcount in India."""
    g = cfg["gates"]["india"]
    if india_pct is not None:
        if india_pct >= g["min_india_headcount_pct"]:
            return GateResult(True, f"{india_pct:.0f}% of headcount in India")
        return GateResult(False, f"only {india_pct:.0f}% of headcount in India")
    # What the AI reader found when it read the whole article. This beats keyword
    # matching on a few hundred characters (which once called Hyderabad-based
    # Cosmoserve "not Indian" because a one-line summary didn't name the city).
    ex = (sig.raw or {}).get("extracted") or {}
    country = str(ex.get("hq_country") or "").strip()
    link = str(ex.get("india_link") or "").strip()
    if ex.get("city"):
        return GateResult(True, f"India hub stated: {ex['city']}", {"city": ex["city"]})
    if country and re.search(r"\bindia\b", country, re.I):
        return GateResult(True, "headquartered in India", {"city": "India"})
    if country:
        if link:
            # Foreign HQ with an India connection: borderline — the AE decides.
            return GateResult(True, f"based in {country}; India link: {link} — AI to verify",
                              {"city": "Unknown", "needs_india_check": True,
                               "india_link": link})
        return GateResult(False, f"based in {country}, no India team or entity mentioned")

    text = _text(sig)
    foreign = FOREIGN_HQ_RE.search(text)
    hits = [(text.find(c.lower()), c) for c in g["hub_cities"] if c.lower() in text]
    hit = min(hits, key=lambda t: t[0])[1] if hits else None

    # A foreign HQ is NOT disqualifying on its own: US/Singapore-incorporated flips
    # with their engineering team in Bengaluru are squarely in the ICP. Keyword rules
    # cannot judge where the headcount actually sits, so anything with a foreign HQ
    # marker is passed through flagged, and the AI decides on the >60% India rule.
    if foreign and not hit:
        return GateResult(True, f"foreign HQ '{foreign.group(0)}' — AI to verify India headcount",
                          {"city": "Unknown", "needs_india_check": True})
    if foreign and hit:
        return GateResult(True, f"foreign HQ with India hub {hit} — AI to verify share",
                          {"city": hit, "needs_india_check": True})
    if hit:
        return GateResult(True, f"India hub referenced: {hit}", {"city": hit})
    # A bare "India" in the lead is weak but acceptable; page furniture can no longer
    # reach this gate because _text() only sees the article lead.
    if re.search(r"\bindia(n)?\b", text):
        return GateResult(True, "India entity referenced", {"city": "India"})
    if sig.from_body:
        # A company the reader pulled out of an article, with no location either way:
        # not knowing is not evidence. Let the AI check, never silently drop.
        return GateResult(True, "location not stated — AI to verify",
                          {"city": "Unknown", "needs_india_check": True})
    return GateResult(False, "no Indian entity or talent footprint found")


def gate_icp(sig: RawSignal, cfg: dict[str, Any]) -> GateResult:
    """Knowledge-work verticals only; pure gig/blue-collar labour is disqualified."""
    icp = cfg["gates"]["icp"]
    text = _text(sig)
    blocked = next((k for k in icp["disqualified_keywords"] if k.lower() in text), None)
    if blocked:
        return GateResult(False, f"disqualified vertical signal: '{blocked}'")
    for vertical, keywords in icp["accepted_verticals"].items():
        hit = next((k for k in keywords if k.lower() in text), None)
        if hit:
            return GateResult(True, f"ICP match: {vertical} ({hit})",
                              {"vertical": vertical, "keyword": hit})
    # Unmatched is NOT an auto-drop: the LLM classifies it later, gate is advisory.
    return GateResult(True, "vertical unresolved — flagged for LLM classification",
                      {"vertical": "unclassified"})


def gate_headcount(count: int | None, cfg: dict[str, Any]) -> GateResult:
    """Strict 1-50. This is the gate that must never be softened."""
    g = cfg["gates"]["headcount"]
    if count is None:
        return GateResult(False, "headcount unverified")
    if g["min"] <= count <= g["max"]:
        return GateResult(True, f"headcount {count} within {g['min']}-{g['max']}")
    return GateResult(False, f"headcount {count} outside {g['min']}-{g['max']}")


def run_hard_gates(sig: RawSignal, cfg: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Pre-verification gates. Returns (passed, failing_stage_or_'', merged_detail)."""
    detail: dict[str, Any] = {}
    for stage, res in (("gate_is_company", gate_is_company(sig)),
                       ("gate_capital", gate_capital(sig, cfg)),
                       ("gate_india", gate_india(sig, cfg)),
                       ("gate_icp", gate_icp(sig, cfg))):
        if not res.passed:
            return False, f"{stage}: {res.reason}", detail
        detail.update(res.detail)
    return True, "", detail
