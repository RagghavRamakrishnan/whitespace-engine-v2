"""Headcount verification — the 1-50 gate's data source, behind one interface.

TARGET NUMBER: the count shown on the company's LinkedIn people page, not "true"
headcount. Those differ, and for this use case LinkedIn's number is the one that
matters. Providers are therefore ranked by how closely their employee count tracks
LinkedIn rather than by general data quality.


Deliberately NOT a LinkedIn scraper. Scraping linkedin.com/company/<slug>/people/
breaches the LinkedIn User Agreement, is blocked by auth walls and rate limits, and
would be a policy problem for an employee-built tool. Use a licensed firmographic
API, an approved first-party surface, or a fast human check instead. The gate logic
in gates.gate_headcount is identical either way.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

from .models import HeadcountResult, RawSignal

log = logging.getLogger(__name__)

# Two different bars, deliberately asymmetric.
#
# CAN_DISQUALIFY: sources solid enough to drop an account. A company's own "we are a
# 300-strong team" is reliable for EXCLUSION — nobody inflates their size in a funding
# release — so a press statement counts here.
CAN_DISQUALIFY = {"google_cse", "diffbot", "pdl", "partner_api", "internal",
                  "press_statement"}

# LINKEDIN_ALIGNED: sources whose number actually approximates the LinkedIn people
# page, and so may be shown as CONFIRMED. A press statement is excluded: "18-member
# team" is a claim about real headcount, which is not the number the AE will see, and
# under-counts are the direction that costs a wasted InMail.
LINKEDIN_ALIGNED = {"google_cse", "pdl", "diffbot", "partner_api", "internal"}

TRUSTED_SOURCES = CAN_DISQUALIFY          # backwards compatibility

# Stage/sector priors used only when no licensed source is configured. Bands are
# intentionally conservative — an over-estimate costs a missed account, an
# under-estimate costs a wasted enrichment.
STAGE_BANDS = {
    "grant": (3, 20), "accelerator": (2, 15), "pre-seed": (2, 15), "angel": (2, 15),
    "seed": (8, 35), "bridge": (10, 40), "pre-series a": (15, 45),
    "series a": (25, 70), "series b": (60, 200), "series c": (120, 400),
}
HC_IN_TEXT = re.compile(
    r"(?:team of|headcount of|employs|workforce of|staff of)\s+(?:about\s+|around\s+|~)?(\d{1,4})"
    r"|(\d{1,4})[\-\s]?(?:member|person|people|employee)s?\b", re.I)


def _from_text(sig: RawSignal) -> HeadcountResult | None:
    """Press releases very often state the team size outright — free and exact."""
    # Note: a team size quoted in a press release is the company's own claim about its
    # real headcount, which can differ from its LinkedIn count either way. Treated as a
    # decent signal, not as confirmation of the number the AE will see.
    m = HC_IN_TEXT.search(sig.deal_text)
    if not m:
        return None
    n = int(next(g for g in m.groups() if g))
    if 1 <= n <= 5000:
        return HeadcountResult(n, 0.8, "press_statement", m.group(0)[:80])
    return None


TOTAL_RAISED_RE = re.compile(
    r"(?:total(?:ling|ing)?|to date|so far|cumulative(?:ly)?|has raised|having raised)"
    r"[^.]{0,40}?(?:\$|usd)\s?([\d.,]+)\s?(mn|m|million|bn|billion)", re.I)


def total_raised_usd(sig: RawSignal) -> float:
    """'has raised $47M to date' is a strong independent size signal: a company that
    has absorbed serious cumulative capital is not a 1-50 person shop, whatever the
    current round says."""
    # Own deal text only — a roundup's cumulative figure is not this company's.
    m = TOTAL_RAISED_RE.search(sig.deal_text)
    if not m:
        return 0.0
    mult = 1e9 if m.group(2).lower() in ("bn", "billion") else 1e6
    try:
        return float(m.group(1).replace(",", "")) * mult
    except ValueError:
        return 0.0


def verify_estimate(sig: RawSignal, detail: dict[str, Any]) -> HeadcountResult:
    from_text = _from_text(sig)
    if from_text:
        return from_text
    rnd = str(detail.get("round_type", sig.signal_type)).lower().replace("-", " ")
    lo, hi = STAGE_BANDS.get(rnd, (5, 40))
    mid = (lo + hi) // 2
    conf = 0.65 if hi <= 50 else 0.45      # bands straddling 50 are inherently unsafe
    return HeadcountResult(mid, conf, "stage_estimate", f"{rnd} band {lo}-{hi}")


def verify_partner_api(sig: RawSignal, cfg: dict[str, Any]) -> HeadcountResult:
    """Licensed firmographic provider (ZoomInfo / Apollo / Crunchbase / Tracxn / Clearbit).

    Expected response contract: {"employee_count": int, "confidence": float}.
    Map your provider's field names here — everything else stays the same.
    """
    p = cfg["verification"]["partner_api"]
    base, key = p.get("base_url", ""), os.getenv(p.get("api_key_env", ""), "")
    if not base or not key:
        return HeadcountResult(None, 0.0, "partner_api", "provider not configured")
    try:
        r = requests.get(base, params={"domain": sig.domain_key or sig.company_name},
                         headers={"Authorization": f"Bearer {key}"}, timeout=20)
        r.raise_for_status()
        data = r.json()
        n = data.get("employee_count")
        return HeadcountResult(int(n) if n is not None else None,
                               float(data.get("confidence", 0.9)), "partner_api")
    except Exception as exc:                                    # noqa: BLE001
        log.warning("partner api lookup failed for %s: %s", sig.company_name, exc)
        return HeadcountResult(None, 0.0, "partner_api", str(exc)[:120])


# LinkedIn company pages publish a size BAND ("11-50 employees") that Google indexes.
# The band maps directly onto a 1-50 gate, so an exact number is not needed.
# CRITICAL: the same snippets carry FOLLOWER counts, which are 10-100x larger. Matching
# "2,400 followers" as headcount would be catastrophic, so only an explicit employee
# phrasing is accepted and follower text is never parsed as a count.
LI_BAND_RE = re.compile(
    r"\b([\d,]+)\s*[-–—]\s*([\d,]+)\s*employees\b", re.I)
LI_EXACT_RE = re.compile(
    r"\b([\d,]+)\+?\s*employees\b", re.I)
LI_SIZE_LABEL_RE = re.compile(
    r"company size[^\d]{0,20}([\d,]+)\s*[-–—]\s*([\d,]+)", re.I)


def _parse_linkedin_size(text: str) -> tuple[int | None, str, tuple[int, int] | None]:
    """Pull an employee count or size band out of a search snippet.

    Returns (upper_bound, note, band). `band` is set when the value came from
    LinkedIn's SELF-REPORTED size dropdown, which the caller must treat differently
    from a counted figure — see HeadcountResult.self_reported_band.
    """
    if not text:
        return None, "", None
    # Strip follower phrases outright before any number matching.
    cleaned = re.sub(r"[\d,.]+\s*(?:k|m)?\s*followers?", " ", text, flags=re.I)
    m = LI_BAND_RE.search(cleaned) or LI_SIZE_LABEL_RE.search(cleaned)
    if m:
        lo = int(m.group(1).replace(",", ""))
        hi = int(m.group(2).replace(",", ""))
        return hi, f"LinkedIn band {lo}-{hi} employees", (lo, hi)
    m = LI_EXACT_RE.search(cleaned)
    if m:
        n = int(m.group(1).replace(",", ""))
        if 1 <= n <= 100000:
            return n, f"LinkedIn snippet: {n} employees", None
    return None, "", None


def verify_google_cse(sig: RawSignal, cfg: dict[str, Any]) -> HeadcountResult:
    """Read the LinkedIn company page's published size band out of Google's index.

    Uses Google's official Custom Search JSON API against Google's own index — not a
    scrape of LinkedIn, and no circumvention of anyone's terms. 100 free queries/day
    (~3,000/month) comfortably covers this engine's volume.

    Needs GOOGLE_CSE_KEY and GOOGLE_CSE_CX (a Custom Search Engine configured to
    search the whole web).
    """
    key = os.getenv("GOOGLE_CSE_KEY", "")
    cx = os.getenv("GOOGLE_CSE_CX", "")
    if not key or not cx:
        return HeadcountResult(None, 0.0, "google_cse", "GOOGLE_CSE_KEY / _CX not set")

    if sig.slug_key:
        q = f'site:linkedin.com/company "{sig.slug_key}"'
    elif sig.company_name:
        q = f'site:linkedin.com/company "{sig.company_name}" India'
    else:
        return HeadcountResult(None, 0.0, "google_cse", "nothing to search for")

    try:
        r = requests.get("https://www.googleapis.com/customsearch/v1",
                         params={"key": key, "cx": cx, "q": q, "num": 3}, timeout=20)
        if r.status_code == 429:
            return HeadcountResult(None, 0.0, "google_cse", "daily query quota reached")
        r.raise_for_status()
        items = r.json().get("items") or []
        for item in items:
            link = (item.get("link") or "").lower()
            if "linkedin.com/company" not in link:
                continue
            # Only trust a result that is plausibly THIS company's page.
            if sig.slug_key and sig.slug_key not in link:
                continue
            blob = " ".join(filter(None, [item.get("title"), item.get("snippet"),
                                          str(item.get("pagemap", {}))[:2000]]))
            count, note, band = _parse_linkedin_size(blob)
            if count is not None:
                # Confidence is high for a band that sits entirely inside the target
                # range, low for anything above it — self-reported bands overstate far
                # more often than they understate.
                conf = 0.85 if band is None or band[1] <= 50 else 0.4
                return HeadcountResult(count, conf, "google_cse", note,
                                       self_reported_band=band)
        return HeadcountResult(None, 0.0, "google_cse", "no size found in snippets")
    except Exception as exc:                                    # noqa: BLE001
        log.warning("Google CSE lookup failed for %s: %s", sig.company_name, exc)
        return HeadcountResult(None, 0.0, "google_cse", str(exc)[:120])


def verify_diffbot(sig: RawSignal, cfg: dict[str, Any]) -> HeadcountResult:
    """Diffbot Knowledge Graph — broader coverage, but its employee count is built from
    web crawling rather than LinkedIn, so it is a looser match for the number the AE
    will actually see on the people page. Used as the fallback, not the primary.

    The strongest genuinely free tier available:
    ~10,000 credits/month with full API access, rate-limited to 5 calls/minute.
    This engine needs roughly 300/month, so the cap is not a constraint.

    Set DIFFBOT_TOKEN. Coverage of very small Indian startups is unproven, so a miss
    is normal and falls through to the article text and then the stage estimate.
    """
    token = os.getenv("DIFFBOT_TOKEN", "")
    if not token:
        return HeadcountResult(None, 0.0, "diffbot", "DIFFBOT_TOKEN not set")
    if sig.domain_key:
        query = f'type:Organization homepageUri:"{sig.domain_key}"'
    elif sig.company_name:
        query = f'type:Organization name:"{sig.company_name}"'
    else:
        return HeadcountResult(None, 0.0, "diffbot", "no domain or name to look up")
    try:
        r = requests.get("https://kg.diffbot.com/kg/v3/dql",
                         params={"type": "query", "query": query, "size": 1, "token": token},
                         timeout=25)
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            return HeadcountResult(None, 0.0, "diffbot", "no match")
        entity = data[0].get("entity", {})
        n = entity.get("nbEmployees") or entity.get("nbEmployeesMin")
        if n is None:
            return HeadcountResult(None, 0.0, "diffbot", "match without employee count")
        return HeadcountResult(int(n), 0.9, "diffbot", "Diffbot Knowledge Graph")
    except Exception as exc:                                    # noqa: BLE001
        log.warning("Diffbot lookup failed for %s: %s", sig.company_name, exc)
        return HeadcountResult(None, 0.0, "diffbot", str(exc)[:120])


def verify_pdl(sig: RawSignal, cfg: dict[str, Any]) -> HeadcountResult:
    """People Data Labs Company Enrichment — the closest free proxy for the number on
    the LinkedIn people page, because PDL's company records are largely LinkedIn-derived.
    That is normally raised as a criticism of PDL; for this use case it is the point.

    Free tier is ~100 lookups/month, and PDL's coverage is thinnest exactly where this
    engine looks — small, non-US companies. Treat a miss as normal and fall through.
    """
    key = os.getenv("PDL_API_KEY", "")
    if not key:
        return HeadcountResult(None, 0.0, "pdl", "PDL_API_KEY not set")
    params: dict[str, str] = {}
    if sig.domain_key:
        params["website"] = sig.domain_key
    elif sig.slug_key:
        params["profile"] = f"linkedin.com/company/{sig.slug_key}"
    else:
        params["name"] = sig.company_name
    try:
        r = requests.get("https://api.peopledatalabs.com/v5/company/enrich",
                         params=params, headers={"X-Api-Key": key}, timeout=20)
        if r.status_code == 404:
            return HeadcountResult(None, 0.0, "pdl", "no match in PDL")
        r.raise_for_status()
        d = r.json()
        n = d.get("employee_count")
        if n is None:
            return HeadcountResult(None, 0.0, "pdl", "match without employee_count")
        # PDL returns a match likelihood 0-10; below ~6 the match is unreliable.
        likelihood = float(d.get("likelihood", 8))
        return HeadcountResult(int(n), min(0.95, likelihood / 10 + 0.1), "pdl",
                               f"likelihood {likelihood}")
    except Exception as exc:                                    # noqa: BLE001
        log.warning("PDL lookup failed for %s: %s", sig.company_name, exc)
        return HeadcountResult(None, 0.0, "pdl", str(exc)[:120])


def verify_manual(sig: RawSignal, *_: Any) -> HeadcountResult:
    """Park it for a human 20-second check rather than guessing."""
    return HeadcountResult(None, 0.0, "manual", "queued for manual headcount check")


def verify_internal(sig: RawSignal, cfg: dict[str, Any]) -> HeadcountResult:
    """Hook for an approved first-party/internal data surface, if your role has access.
    Implement against that surface's client here; keep the return contract."""
    return HeadcountResult(None, 0.0, "internal", "internal provider not implemented")


PROVIDER_FNS = {
    "google_cse": verify_google_cse,
    "diffbot": verify_diffbot,
    "pdl": verify_pdl,
    "partner_api": verify_partner_api,
    "manual": verify_manual,
    "internal": verify_internal,
}


def _tiebreak_allowed(store: Any, cap: int, reserve_for_borderline: bool,
                      near_boundary: bool) -> bool:
    """Hard monthly cap on the scarce provider.

    Diffbot's coverage of very small Indian startups is unproven; if it misses often,
    those misses alone would exhaust PDL's ~100/month in days. So the budget is counted,
    and as it runs low the remaining calls are reserved for borderline cases — where a
    second opinion actually changes the decision — rather than spent on misses.
    """
    if store is None or cap <= 0:
        return True
    from datetime import datetime
    key = f"tiebreak_calls_{datetime.now():%Y_%m}"
    used = int(store.get_state(key) or 0)
    if used >= cap:
        log.info("tiebreaker budget exhausted for this month (%d/%d)", used, cap)
        return False
    if reserve_for_borderline and used >= cap * 0.8 and not near_boundary:
        log.info("tiebreaker budget low (%d/%d) — reserving for borderline cases", used, cap)
        return False
    store.set_state(key, str(used + 1))
    return True


def verify_headcount(sig: RawSignal, cfg: dict[str, Any],
                     detail: dict[str, Any] | None = None,
                     store: Any = None) -> HeadcountResult:
    """Run an ordered chain of providers, each with its own trigger condition.

    The free tiers differ by 100x, so they are not interchangeable and must not be
    called indiscriminately:

      google_cse  ~3,000/month  always      LinkedIn's own published size band
      diffbot    ~10,000/month  miss or     general web crawl; broad coverage
                                borderline
      pdl           ~100/month  miss only   last resort; scarce, so spent only when
                                            nothing else produced a number at all

    A later provider only runs when an earlier one left the decision genuinely open,
    so the scarce tier lasts. The first real number wins, except that a provider which
    supersedes an ambiguous earlier result replaces it.
    """
    detail = detail or {}
    v = cfg["verification"]
    chain = v.get("chain")

    # Legacy shapes still work.
    if not chain:
        legacy = [n for n in (v.get("primary"), v.get("tiebreaker")) if n]
        if not legacy:
            legacy = [n for n in (v.get("providers") or [v.get("provider", "estimate")])]
        chain = [{"name": legacy[0]}] + [{"name": n, "when": "miss_or_borderline"}
                                         for n in legacy[1:]]

    lo_band, hi_band = v.get("tiebreak_band", [40, 70])
    res = HeadcountResult(None, 0.0, "none", "no provider configured")

    for i, step in enumerate(chain):
        name = step.get("name")
        if not name or name == "estimate":
            break
        fn = PROVIDER_FNS.get(name)
        if not fn:
            log.warning("unknown verification provider: %s", name)
            continue

        if i > 0:
            missed = res.count is None
            g = cfg["gates"]["headcount"]
            band = res.self_reported_band

            # A self-reported band can be CONCLUSIVE in either direction: entirely
            # inside the target range, or starting so high that optimism cannot
            # explain it. Either way a second lookup would change nothing, so do not
            # spend one — even if the band's edge happens to sit in the borderline
            # numeric range (an "11-50" band ends at exactly the 50 limit).
            conclusive_band = band is not None and (
                band[1] <= g["max"] or band[0] >= g.get("band_disqualify_floor", 201))
            ambiguous = band is not None and not conclusive_band

            borderline = (res.count is not None and band is None
                          and lo_band <= res.count <= hi_band)

            when = step.get("when", "miss_or_borderline")
            if when == "miss_only":
                # "Nobody knows anything useful" — a genuine miss, or an unusable
                # self-reported band that the previous step failed to resolve. A
                # counted-but-borderline figure does NOT qualify: that is information,
                # and the scarce tier is not spent refining it.
                trigger = missed or ambiguous
            else:
                trigger = missed or borderline or ambiguous
            if conclusive_band or not trigger:
                break
            cap = int(step.get("monthly_cap", 0))
            if cap and not _tiebreak_allowed(store, cap,
                                             bool(v.get("reserve_tiebreaker", True)),
                                             borderline or ambiguous):
                break
            why = "nothing found yet" if missed else "earlier result was ambiguous"
            log.info("escalating to %s for %s (%s)", name, sig.company_name, why)

        got = fn(sig, cfg)
        if got.count is not None:
            # A counted figure always beats a self-reported band.
            if res.self_reported_band is not None and got.self_reported_band is None:
                got.note = f"{got.note} (supersedes {res.note})".strip()
            res = got
            if got.self_reported_band is None:
                return res            # counted and unambiguous; stop spending lookups

    if res.count is not None:
        return res

    # Both providers came back empty. That still is not a reason to lose the account:
    # fall through to the article's own statement, then to the stage-band estimate.
    # Either way the result is unconfirmed, so it reaches the AE flagged for a check
    # rather than being silently dropped.
    if any(step.get("name") == "manual" for step in chain):
        return HeadcountResult(None, 0.0, "manual", "queued for manual headcount check")
    fallback = _from_text(sig)
    if fallback:
        return fallback
    if v.get("fallback_to_estimate", True):
        est = verify_estimate(sig, detail)
        est.note = f"provider miss -> {est.note}"
        return est
    return res
