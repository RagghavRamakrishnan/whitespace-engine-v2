"""Core data structures passed between pipeline stages."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

_TRACKING = re.compile(r"[?#].*$")


def normalize_domain(url_or_domain: str | None) -> str:
    """vecton.ai from https://www.Vecton.ai/careers?utm=x  -> stable dedup key."""
    if not url_or_domain:
        return ""
    raw = url_or_domain.strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).netloc or ""
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_li_slug(url_or_slug: str | None) -> str:
    """vecton-ai from https://linkedin.com/company/vecton-ai/people/."""
    if not url_or_slug:
        return ""
    raw = _TRACKING.sub("", url_or_slug.strip().lower().rstrip("/"))
    if "linkedin.com" in raw:
        m = re.search(r"/company/([^/]+)", raw)
        return m.group(1) if m else ""
    return raw


_LEGAL_SUFFIX = re.compile(
    r"\b(pvt|private|ltd|limited|llp|inc|incorporated|corp|corporation|co|"
    r"technologies|technology|labs|systems|solutions|ventures|group)\b\.?", re.I)


def collapse_doubled_name(name: str | None) -> str:
    """'Clinics On Cloud Clinics On Cloud' -> 'Clinics On Cloud'. Google News
    snippets often repeat the name, which slipped past the 'already sent' check."""
    words = (name or "").split()
    half = len(words) // 2
    if half and len(words) % 2 == 0 and \
            [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
        return " ".join(words[:half])
    return (name or "").strip()


def normalize_company_name(name: str | None) -> str:
    """'Vecton AI Technologies Pvt. Ltd.' -> 'vecton ai' for fuzzy dedup."""
    if not name:
        return ""
    n = re.sub(r"[^\w\s&]", " ", collapse_doubled_name(name).lower())
    n = _LEGAL_SUFFIX.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


@dataclass
class RawSignal:
    """One unqualified capital-inflow candidate emitted by an ingestion adapter."""
    source_id: str
    title: str
    url: str
    published_at: datetime
    body: str = ""
    company_name: str = ""
    website: str = ""
    linkedin_url: str = ""
    signal_type: str = "funding"          # funding | grant | accelerator | social | mca
    raw: dict[str, Any] = field(default_factory=dict)
    # True when this signal was extracted from a SENTENCE inside the article rather
    # than from its headline — e.g. one company pulled out of a weekly funding
    # roundup. Such a signal carries its own sentence as `title`, so headline-level
    # rules (listicle detection, headline funding language) apply to that sentence.
    from_body: bool = False

    @property
    def headline(self) -> str:
        """The title alone.

        For a funding story the headline ALWAYS states the event — that is what a
        headline is for. Body text cannot be trusted for the same purpose: "related
        stories" blocks and inline promos put other companies' funding words inside
        the article, which is how a Microsoft product story acquires the word
        "raises".
        """
        return self.title or ""

    @property
    def lead_text(self) -> str:
        """Title + opening of the article ONLY.

        Gates must never read the full scraped page: navigation menus, "related
        articles" sidebars and footers contain stray mentions of "India", "AI" and
        funding words that pass every keyword gate. The first ~700 characters are
        where a news story states who raised what, and nothing else.
        """
        return f"{self.title}\n{self.body[:700]}"

    @property
    def deal_text(self) -> str:
        """This company's OWN deal text, with inherited article context removed.

        Context legitimately supplies geography and sector, but it must never supply
        an AMOUNT: a roundup headline says "$142 million across 21 deals", and reading
        that as one company's round wrongly disqualifies every company in the article.
        """
        body = self.body or ""
        marker = body.find("[Article context]")
        own = body[:marker] if marker >= 0 else body
        return f"{self.title}\n{own}"

    @property
    def domain_key(self) -> str:
        return normalize_domain(self.website)

    @property
    def slug_key(self) -> str:
        return normalize_li_slug(self.linkedin_url)

    @property
    def name_key(self) -> str:
        """Last-resort dedup key. Without it, a company whose domain and LinkedIn
        slug could not be extracted never dedups and alerts on every run."""
        return normalize_company_name(self.company_name)

    def signal_id(self) -> str:
        return f"{self.source_id}:{self.url}"


@dataclass
class GateResult:
    passed: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class HeadcountResult:
    count: Optional[int]
    confidence: float
    source: str
    note: str = ""
    # A LinkedIn size band is SELF-REPORTED: an admin picks it from a dropdown, often
    # once, often aspirationally. A 30-person company routinely shows "51-200". It is
    # therefore usable to CONFIRM smallness but never to disqualify, except at absurd
    # magnitudes. Kept distinct from a counted figure for exactly that reason.
    self_reported_band: Optional[tuple[int, int]] = None


@dataclass
class QualifiedAccount:
    """A signal that cleared every hard gate, plus LLM enrichment, ready to dispatch."""
    company_name: str
    website: str
    linkedin_url: str
    headcount: int
    headcount_source: str
    india_city: str
    industry_icp: str
    funding_amount: str
    round_type: str
    decision_makers: list[dict[str, str]]
    bottleneck: str
    expansion_trigger: str
    recommended_package: str
    positioning: str
    inmail_hook: str
    source_url: str
    is_follow_on: bool = False
    headcount_verified: bool = False   # False => shown in the "confirm" section
    headcount_note: str = ""           # e.g. "LinkedIn band 11-50 employees"
    source_label: str = ""             # e.g. "Inc42 — From GIVA To EloElo Group…"
    detected_at: datetime = field(default_factory=datetime.now)

    def headcount_display(self) -> str:
        """A band must not be shown as a precise number: reporting "50" for an
        11-50 company looks like a measurement when it is a bracket."""
        # Only a LinkedIn-published band displays as a range. The stage estimator also
        # mentions a "band" in its note, but that is a guess about a bracket, not a
        # bracket the company published, and must not be shown as one.
        m = re.match(r"LinkedIn band (\d[\d,]*-\d[\d,]*)", self.headcount_note)
        if m:
            return m.group(1) + ("" if self.headcount_verified else " (self-reported)")
        if self.headcount_verified:
            return str(self.headcount)
        if self.headcount_source == "press_statement":
            return f"{self.headcount} (stated in article)"
        # A stage-based guess is not shown as a number: "22" on every company looks
        # like data when it is a placeholder.
        return "Unknown"

    def to_row(self) -> list[str]:
        """Master spreadsheet columns A..O, in schema order."""
        dm = self.decision_makers + [{}, {}]
        fmt = lambda d: (
            f"{d.get('name', '')} — {d.get('title', '')} | {d.get('linkedin_url', '')}".strip(" —|")
            if d else ""
        )
        return [
            self.detected_at.strftime("%Y-%m-%d %H:%M:%S"),
            self.company_name,
            self.website,
            self.linkedin_url,
            self.headcount_display(),
            self.india_city,
            self.industry_icp,
            self.funding_amount,
            f"{self.round_type} [Follow-on Round]" if self.is_follow_on else self.round_type,
            fmt(dm[0]),
            fmt(dm[1]),
            self.bottleneck,
            self.recommended_package,
            self.inmail_hook,
            "Unclaimed",
        ]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["detected_at"] = self.detected_at.isoformat()
        return d


SHEET_HEADERS = [
    "Timestamp (IST)", "Company Name", "Domain / Website", "Company LinkedIn URL",
    "Verified Headcount", "India Base City", "Industry ICP", "Funding Amount",
    "Round Type", "Decision Maker 1", "Decision Maker 2", "AI Hiring Bottleneck",
    "Recommended Package", "Draft InMail Hook", "CRM Status (Manual)",
]
