"""Gate regression tests. Run before every deploy — these gates decide what reaches
the AE's inbox, and a silent loosening is the worst failure mode in this system."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.gates import (gate_capital, gate_headcount, gate_icp,  # noqa: E402
                       gate_india, gate_is_company)
from src.models import RawSignal, normalize_domain, normalize_li_slug  # noqa: E402

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())


def sig(title: str, body: str = "") -> RawSignal:
    return RawSignal(source_id="test", title=title, url="https://x.test/a",
                     published_at=datetime.now(timezone.utc), body=body,
                     company_name=title.split(" raises")[0].strip()[:60])


# ------------------------------------------------------------------ headcount
@pytest.mark.parametrize("n,expected", [(1, True), (25, True), (50, True),
                                        (51, False), (0, False), (None, False)])
def test_headcount_gate_is_strict(n, expected):
    assert gate_headcount(n, CFG).passed is expected


# -------------------------------------------------------------------- capital
def test_capital_detects_seed_round():
    r = gate_capital(sig("Vecton AI raises $2.4M seed round led by Blume"), CFG)
    assert r.passed and r.detail["round_type"].lower() == "seed"


def test_capital_rejects_non_funding_news():
    assert not gate_capital(sig("Vecton AI launches new dashboard"), CFG).passed


# --- regressions from real bad alerts (Microsoft / Anthropic, Sept 2026) -------
def test_funding_word_in_body_alone_does_not_qualify():
    """A 'related stories' sidebar carrying another company's funding words must not
    turn a product announcement into a prospect."""
    s = sig("Microsoft announces new Copilot features",
            "Updates to Office. Related: Vecton AI raises $2.4M seed round.")
    assert not gate_capital(s, CFG).passed


def test_investor_headline_rejected():
    assert not gate_capital(sig("Microsoft invests in Indian AI startups"), CFG).passed
    assert not gate_capital(sig("Infosys acquires Bengaluru AI startup"), CFG).passed


def test_vc_raising_its_own_fund_rejected():
    assert not gate_capital(sig("Blume raises Rs 500 crore maiden fund"), CFG).passed
    assert not gate_capital(sig("Antler closes $75M India fund"), CFG).passed


def test_raises_funding_is_not_a_fund_raise():
    """'fund' must not match inside 'funding' — this phrasing is extremely common."""
    assert gate_capital(sig("Vecton AI raises funding"), CFG).passed
    assert gate_capital(sig("Gridbot raises fresh funding from Blume"), CFG).passed


def test_listicle_rejected():
    s = sig("Top 10 startups that raised funding this week")
    s.company_name = "Top 10 startups that"
    assert not gate_is_company(s).passed


def test_capital_rejects_late_stage():
    assert not gate_capital(sig("Zeta raises $250M Series D"), CFG).passed


def test_grant_signal_passes_structurally():
    s = sig("BIRAC awardee list 2026")
    s.signal_type = "grant"
    assert gate_capital(s, CFG).passed


# ---------------------------------------------------------------------- india
def test_india_hub_city_passes():
    assert gate_india(sig("Startup raises seed, based in Bengaluru"), CFG).passed


def test_non_india_fails():
    assert not gate_india(sig("Berlin-based Foo raises seed round"), CFG).passed


def test_india_headcount_percentage_overrides_text():
    assert gate_india(sig("Delaware C-corp raises seed"), CFG, india_pct=75).passed
    assert not gate_india(sig("Delaware C-corp raises seed"), CFG, india_pct=20).passed


# ------------------------------------------------------------------------ icp
def test_blue_collar_staffing_disqualified():
    assert not gate_icp(sig("Seed for a plumbing and electrician staffing agency"), CFG).passed


def test_deeptech_accepted():
    r = gate_icp(sig("Robotics startup raises pre-seed in Pune"), CFG)
    assert r.passed and r.detail["vertical"] == "deeptech"


def test_unclassified_is_passed_to_llm_not_dropped():
    r = gate_icp(sig("Mystery Co raises seed"), CFG)
    assert r.passed and r.detail["vertical"] == "unclassified"


# --------------------------------------------------------------- dedup keys
def test_domain_normalization():
    assert normalize_domain("https://WWW.Vecton.ai/careers?utm=x") == "vecton.ai"
    assert normalize_domain("vecton.ai") == "vecton.ai"


def test_slug_normalization():
    assert normalize_li_slug("https://www.linkedin.com/company/vecton-ai/people/") == "vecton-ai"
    assert normalize_li_slug("vecton-ai") == "vecton-ai"


# --- roundup explosion --------------------------------------------------------
# Entrackr/YourStory weekly roundups carry most small seed rounds; those companies
# never get their own article. One article must yield one signal PER company.

def _load_extractor():
    import re as _re
    src = (Path(__file__).resolve().parents[1] / "src" / "ingest.py").read_text()
    ns = {"re": _re, "RawSignal": RawSignal}
    exec(src[src.index("# Leading editorial noise"):src.index("def _fetch(")], ns)
    return ns["extract_body_deals"]


ROUNDUP_BODY = (
    "Bengaluru-based Vecton AI raised $2.4 million in a seed round led by Blume Ventures. "
    "Gridbot, a Pune-based robotics startup, secured $900,000 in pre-seed funding. "
    "Medha bagged Rs 15 crore from Accel and angel investors. "
    "Blume Ventures raised Rs 500 crore for its maiden fund. "
    "Chennai's Orbit landed Rs 8 crore in a pre-Series A round."
)


def test_roundup_yields_one_signal_per_company():
    extract = _load_extractor()
    parent = sig("Indian startups raised $142 million across 21 deals this week",
                 ROUNDUP_BODY)
    names = {c.company_name for c in extract(parent)}
    assert {"Vecton AI", "Gridbot", "Medha", "Orbit"} <= names


def test_roundup_excludes_the_investor_raising_its_own_fund():
    extract = _load_extractor()
    parent = sig("Weekly funding roundup", ROUNDUP_BODY)
    assert not any("Blume" in c.company_name for c in extract(parent))


def test_parent_roundup_itself_is_blocked():
    s = sig("Indian startups raised $142 million across 21 deals this week", ROUNDUP_BODY)
    s.company_name = "Indian startups"
    assert not gate_is_company(s).passed


def test_sidebar_funding_attaches_to_the_named_company_not_the_headline():
    """The Microsoft bug: a sidebar's funding verb must never attach to the
    headline's company. It should produce a signal for the company the sentence
    actually names."""
    extract = _load_extractor()
    parent = sig("Microsoft announces new Copilot features",
                 "Microsoft announced updates. Related stories: Vecton AI raised "
                 "$2.4 million in seed funding.")
    parent.company_name = "Microsoft"
    names = {c.company_name for c in extract(parent)}
    assert "Vecton AI" in names
    assert "Microsoft" not in names


def test_past_tense_funding_verbs_recognised():
    """Roundup entries are written in the past tense."""
    for headline in ("Gridbot bagged $900K pre-seed",
                     "Medha secured Rs 15 crore",
                     "Nexa netted $1.2 million in angel funding",
                     "Orbit landed Rs 8 crore in a pre-Series A round"):
        assert gate_capital(sig(headline), CFG).passed, headline


# --- recall: the pre-filter must not skip real funding articles ----------------

def test_prefilter_reads_every_kind_of_funding_article():
    """A skipped article is never read, so a miss here is permanent and silent.
    These phrasings all previously failed because of a broken word boundary."""
    from src.extract import worth_reading
    must_read = [
        ("Indian startups raised $142M across 21 deals this week", ""),
        ("Weekly funding report: 18 Indian startups raise", ""),
        ("Kaaro emerges from stealth", "Launched with backing from angels."),
        ("Gridbot announces Series A", "The Pune firm concluded its round."),
        ("Foo picked up an undisclosed sum", "The company confirmed the investment."),
        ("Nexa bagged Rs 8 crore", ""),
        ("Acme selected for Y Combinator batch", ""),
    ]
    for title, body in must_read:
        assert worth_reading(sig(title, body)), title


def test_prefilter_skips_articles_with_no_financial_content():
    from src.extract import worth_reading
    assert not worth_reading(sig("Microsoft launches new Copilot UI",
                                 "A redesign of the sidebar."))
    assert not worth_reading(sig("How to hire your first engineer",
                                 "A guide to recruiting."))


def test_article_total_is_not_charged_to_each_company():
    """A roundup's '$142 million across 21 deals' must not disqualify the individual
    companies inside it as mega-rounds."""
    from src.extract import to_signals
    parent = sig("Weekly funding report",
                 "Indian startups raised $142 million across 21 deals this week.")
    kids = to_signals(parent, [{"company_name": "Medha", "amount": None,
                                "round_type": "Seed", "city": "Hyderabad",
                                "sector": "medtech", "investors": "Accel",
                                "evidence": "picked up an undisclosed sum"}])
    assert len(kids) == 1
    assert gate_capital(kids[0], CFG).passed


def test_union_of_extractors_loses_nothing():
    """A company must survive if EITHER extractor found it."""
    from src.extract import merge_signals, to_signals
    parent = sig("Weekly funding report", "Gridbot announced a $900,000 pre-seed.")
    only_llm = to_signals(parent, [{"company_name": "Gridbot", "amount": "$900,000",
                                    "round_type": "Pre-Seed", "city": "Pune",
                                    "sector": "robotics", "investors": None,
                                    "evidence": "announced a $900,000 pre-seed"}])
    merged = merge_signals(only_llm, [])
    assert {s.company_name for s in merged} == {"Gridbot"}
