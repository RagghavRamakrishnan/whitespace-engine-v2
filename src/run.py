"""Orchestrator. Run every 15 minutes.

Ordering is load-bearing: dedup (free) -> hard gates (free) -> headcount
verification (may cost a credit) -> LLM enrichment (costs money) -> dispatch.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
import logging
import re
import sys
from typing import Any

import yaml

from . import extract, ingest, verify
from .db import Store
from .dispatch import append_row, send_digest, send_heartbeat
from .enrich import call_llm, enrich
from .gates import gate_headcount, run_hard_gates
from .report import RunReport, has_problem, overall
from .models import (RawSignal, collapse_doubled_name, normalize_company_name, normalize_domain,
                     normalize_li_slug)

logging.basicConfig(level=logging.INFO,
                    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}')
log = logging.getLogger("whitespace")


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def dry_run_llm_off(cfg: dict[str, Any]) -> bool:
    """Extraction needs a live model; a dry run without credentials falls back to the
    regex net rather than failing."""
    import os
    keys = {"google": "GOOGLE_API_KEY", "groq": "GROQ_API_KEY",
            "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    return not os.getenv(keys.get(cfg["llm"]["provider"], ""), "")


_DEAL_HINT = re.compile(
    r"\b(?:rais(?:e|es|ed|ing)|secured|bagged|netted|landed|funding round|seed round|"
    r"series\s+[a-f]|pre-seed|stealth|grant)\b", re.I)


_AMOUNT_IN_HEADLINE = re.compile(
    r"[$₹€£]\s?\d|\b(?:rs|inr|usd)\.?\s?\d|\d\s?(?:cr|crore|mn|million|bn|billion|lakh|k)\b",
    re.I)


def _looks_like_a_deal(sig: RawSignal) -> bool:
    """A headline that names an amount AND uses a funding word ("X raises $2 Mn").
    "Jefferies raises India allocation to 14%" has the word but no amount."""
    return bool(_DEAL_HINT.search(sig.title or "")
                and _AMOUNT_IN_HEADLINE.search(sig.title or ""))


# AI reasons meaning "the page only had a headline / one-line mention of this company".
_HEADLINE_ONLY = re.compile(
    r"headline|snippet|single[- ]line mention|roundup (?:fragment|mention)|"
    r"no (?:substantive|actionable|real) (?:article |details|information|text)|"
    r"(?:lacks?|without|not contain) (?:sufficient |any )?(?:company )?details|"
    r"could ?n[o']?t (?:be )?read", re.I)


# ---- remembered rejections: a clear-cut "no" is not re-judged for a week ----------
# Each new article used to give the AI a fresh chance to decide differently (Eggoz was
# rejected as too large, then sent an hour later) and the same names filled every email.
REJECT_MEMORY_DAYS = 7
_CLEAR_STAGES = {"capital", "headcount", "is_company"}
_CLEAR_AI = re.compile(
    r"well[- ]known|far (?:more|above|exceed)|more than 50|well over 50|over 50 "
    r"employees|hundreds of employees|large (?:frontline )?workforce|established "
    r"(?:company|nbfc|brand|business)|not an operating company|not a company|"
    r"rights issue|internal (?:funding|transfer|investment)|parent[- ]company|"
    r"no (?:genuine )?new funding|not a (?:genuine )?(?:new )?funding", re.I)


def _reject(store: Store, report: Any, sig: RawSignal, stage: str, reason: str) -> None:
    report.reject(sig.company_name, stage, reason)
    st = re.sub(r"^gate_", "", stage or "")
    key = sig.name_key
    if key and (st in _CLEAR_STAGES or (st == "ai_veto" and _CLEAR_AI.search(reason or ""))):
        store.set_state(f"rejected:{key}", datetime.now().isoformat())


def _recently_rejected(store: Store, key: str) -> bool:
    raw = store.get_state(f"rejected:{key}") if key else None
    if not raw:
        return False
    return datetime.now() - datetime.fromisoformat(raw) < timedelta(days=REJECT_MEMORY_DAYS)


# ---- links that may not mention the company ----------------------------------------
_ROUNDUP_TITLE = re.compile(r"\bstartups?\b.*\b(?:raised|funding|deals?)\b|this week|"
                            r"weekly|round-?up|funding (?:news|wrap)\b|\btop \d+|"
                            r"\blist\b|deals? (?:this|of the)", re.I)


def _name_in_title(name: str, title: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
             if len(w) > 2 and w not in {"pvt", "ltd", "private", "limited", "inc", "the",
                                         "india", "technologies", "labs"}]
    return bool(words) and words[0] in (title or "").lower()


def _name_in_lead(name: str, body: str) -> bool:
    """True when the company is named in the opening of the article it came from.
    A founder-profile story ("Kishore Manepalli's Mission…") names the company in its
    first lines, not its headline — that is its own story, not a headline list."""
    lead = (body or "").split("[Article context]", 1)
    if len(lead) < 2:
        return False
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
             if w not in {"pvt", "ltd", "private", "limited", "inc", "the"}]
    if not words:
        return False
    return re.search(r"\b" + r"\W+".join(map(re.escape, words)) + r"\b",
                     lead[1].lower()) is not None


def _same_company(a: str, b: str) -> bool:
    """Same company under a shorter or longer name: "scanxt" vs "scanxt scientific"."""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and long_.startswith(short + " ")


_SOURCE_NAMES = {"entrackr": "Entrackr", "inc42": "Inc42", "yourstory": "YourStory",
                 "ettech": "ET Tech", "vccircle": "VCCircle", "livemint_tech": "Mint",
                 "moneycontrol_startup": "Moneycontrol", "google_news": "Google News"}


def _source_label(sig: RawSignal) -> str:
    raw = sig.raw or {}
    name = raw.get("publisher") or _SOURCE_NAMES.get(sig.source_id, sig.source_id)
    title = raw.get("parent_title") or sig.title or ""
    title = title if len(title) <= 100 else title[:97] + "…"
    return f"{name} — {title}" if title else name


def _list_india_unknown(store: Store, report: Any, sig: RawSignal, hq: str) -> None:
    """Add to the 'India presence unknown — your call' list, once per 30 days."""
    key = f"india_unknown:{sig.name_key or (sig.company_name or '').lower()}"
    last = store.hours_since(key)
    if last is not None and last < 24 * 30:
        return
    if any(n == sig.company_name for n, _, _, _ in report.india_unknown):
        return
    store.mark_now(key)
    raw = sig.raw or {}
    report.india_unknown.append((sig.company_name, hq or "HQ not stated",
                                 _source_label(sig), raw.get("publisher_url") or sig.url))


def process(cfg: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    report = RunReport(cfg)
    logging.getLogger().addHandler(report.logs)
    try:
        return _process(cfg, dry_run, report)
    finally:
        logging.getLogger().removeHandler(report.logs)


def _process(cfg: dict[str, Any], dry_run: bool, report: RunReport) -> dict[str, int]:
    store = Store(cfg["storage"].get("sqlite_path", "./whitespace.db"),
                  cool_off_days=cfg["dispatch"].get("dedup_days", 180))
    stats = {"ingested": 0, "seen": 0, "articles_read": 0, "companies_found": 0,
             "signals": 0, "possible_misses": 0, "thin_body": 0, "quota_blocked": 0,
             "gave_up": 0, "too_old": 0, "dead_sources": 0, "rechecked": 0,
             "recovered_on_recheck": 0, "short_lists": 0, "dedup": 0, "gated": 0,
             "headcount": 0, "hc_verified": 0, "hc_unverified": 0, "ai_veto": 0,
             "review": 0, "enriched": 0, "alerted": 0}
    # How long since the previous run STARTED? The timer that wakes the engine can
    # silently skip runs (GitHub's own timer once went 7 hours without firing), and a
    # run that finds something looks normal — so measure the gap and report it.
    report.gap_hours = store.hours_since("last_run_start")
    store.mark_now("last_run_start")

    # THE CLOCK. GitHub kills a run at 12 minutes, and a killed run sends nothing
    # and saves nothing — so the next run repeats the same work and can die the same
    # way. The engine therefore stops starting new work in time, carries the rest to
    # the next run, and ALWAYS finishes by sending what it has.
    import time as _time
    t0 = _time.monotonic()
    budget_s = 60 * float(cfg["run"].get("time_budget_minutes", 8.5))
    # Reading may use everything except a fixed reserve for write-ups and the email.
    # (It used to be 60% of the run counted from the START — so a slow news-collection
    # step could use up the reading time before a single article was read.)
    read_until = t0 + budget_s - 150          # reading articles
    enrich_until = t0 + budget_s - 45         # write-ups; leaves time to email
    stats["time_deferred"] = 0

    pending: list[tuple[Any, int, int, bool]] = []
    alerted_this_run: set[str] = set()
    alerted_names_this_run: set[str] = set()
    articles = ingest.collect(cfg, store)
    for s in cfg.get("sources", []):
        if s.get("enabled") is not False:
            report.by_source[s["id"]]["stories"] += 0
    for a in articles:
        report.by_source[a.source_id]["stories"] += 1
    report.source_errors = dict(ingest.SOURCE_ERRORS)

    # Only recent news. A funding story older than this is not a whitespace race any
    # more — someone has already had weeks to claim the account.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    max_age = int(cfg["run"].get("max_article_age_days", 3))
    cutoff = _dt.now(_tz.utc) - _td(days=max_age)
    recent = []
    for a in articles:
        pub = a.published_at if a.published_at.tzinfo else a.published_at.replace(tzinfo=_tz.utc)
        if pub < cutoff:
            stats["too_old"] += 1
            continue
        recent.append(a)
    articles = recent
    stats["ingested"] = len(articles)
    stats["browser_opened"] = ingest.FETCH_STATS["browser_opened"]
    stats["expanded"] = ingest.FETCH_STATS["expanded"]
    stats["paywalled"] = ingest.FETCH_STATS["paywalled"]
    stats["gn_tried"] = ingest.FETCH_STATS["gn_tried"]
    stats["gn_decoded"] = ingest.FETCH_STATS["gn_decoded"]
    report.gn_error = ingest.FETCH_STATS["gn_error"]

    # A source silently returning nothing is the most likely way this degrades.
    dead = store.unhealthy_sources(int(cfg["run"].get("dead_source_runs", 20)))
    stats["dead_sources"] = len(dead)
    for d in dead:
        log.error("source %s has returned nothing for %d runs (last OK: %s)",
                  d["source_id"], d["empty_streak"], d["last_ok_at"] or "never")

    # ---------------------------------------------------------------- READING
    # An LLM reads every article that could be about money and lists the companies
    # that raised — the same job the AE does by hand, and the step that decides
    # whether anything is missed. The regex net from ingest runs alongside it; the
    # union is used, so a company is only lost if BOTH extractors missed it.
    signals: list[RawSignal] = []
    fresh: list[RawSignal] = []
    attempt_no: dict[str, int] = {}
    for art in articles:
        if store.is_signal_seen(art.signal_id()):
            stats["seen"] += 1
            continue
        report.by_source[art.source_id]["new"] += 1
        # Record an ATTEMPT, not a completion. The article is only marked processed
        # once it has actually been read — a failed read comes back next run instead
        # of vanishing.
        attempt_no[art.signal_id()] = store.claim_signal(art.signal_id(), art.source_id)
        fresh.append(art)

    import json as _json
    try:
        carried_ids = set(_json.loads(store.get_state("carried_over") or "[]"))
    except ValueError:
        carried_ids = set()
    readable = []
    for a in fresh:
        if a.signal_id() in carried_ids:
            a.raw = a.raw or {}
            a.raw["carried"] = True
        link = (a.raw or {}).get("publisher_url") or a.url
        if ingest.is_section_page(link, a.title):
            stats["not_article"] = stats.get("not_article", 0) + 1
            report.section_pages.append(link)
            log.info("skipping %s — a website section page, not a story", link)
            continue
        if extract.worth_reading(a):
            readable.append(a)
    stats["articles_read"] = len(readable)
    use_llm = cfg["run"].get("llm_extraction", True) and not dry_run_llm_off(cfg)
    # Cap requests per run so one busy day cannot burn the whole daily allowance in a
    # single run. Anything not read stays queued and is read on the next run.
    call_budget = int(cfg["llm"].get("max_llm_calls_per_run", 12))

    found: dict[str, list[dict[str, Any]]] = {}
    status: dict[str, str] = {a.signal_id(): ("read" if not use_llm else "pending")
                              for a in readable}
    def timed_llm(system: str, user: str, c: dict[str, Any]) -> str:
        if _time.monotonic() > read_until:
            report.time_up_reading = True
            # Same path as running out of AI allowance: the rest wait for next run.
            raise extract.QuotaExhausted("quota: time budget for reading used up")
        return call_llm(system, user, c)

    if use_llm and readable:
        try:
            found, status, notes, call_budget = extract.read_articles(
                readable, cfg, timed_llm, call_budget)
        except Exception as exc:                                # noqa: BLE001
            # Whatever went wrong, the run carries on: unread articles stay queued
            # for the next run, and anything already found still gets emailed.
            log.error("article reading stopped early: %s — remaining articles "
                      "will be read next run", exc)
            found, status = {}, {a.signal_id(): "quota" for a in readable}
            notes = {"rechecked": 0, "recovered": 0, "short_lists": 0}
        stats["rechecked"] += notes["rechecked"]
        stats["recovered_on_recheck"] += notes["recovered"]
        stats["short_lists"] += notes["short_lists"]

    for art in readable:
        sid = art.signal_id()
        st = status.get(sid, "quota")
        if st in ("quota", "pending"):
            # Not read this run. Stays queued; the next run picks it up, and the
            # deferral does not count as a failed attempt.
            stats["quota_blocked"] += 1
            store.release_signal(sid)
            continue
        report.by_source[art.source_id]["read"] += 1

        thin = extract.body_looks_truncated(art)
        from_llm = extract.to_signals(art, found.get(sid, []))
        from_regex = ingest.extract_body_deals(
            art, int(cfg["run"].get("max_deals_per_article", 40)))
        merged = extract.merge_signals(from_llm, from_regex)
        for m in merged:
            m.raw = m.raw or {}
            m.raw["source_thin"] = bool(thin)
        signals.append(art)
        signals.extend(merged)
        stats["companies_found"] += len(merged)
        report.by_source[art.source_id]["companies"] += len(merged)
        report.article_kind[art.url] = (
            "roundup" if extract.stated_deal_count(art) > 0 or len(merged) >= 3
            else "individual")

        if thin and st == "read" and attempt_no.get(sid, 1) >= 2:
            # Already retried once and the full text still won't download. Re-reading
            # the same summary with the AI again gains nothing and used to end in a
            # "given up" alarm. Keep what was found and move on.
            stats["thin_accepted"] = stats.get("thin_accepted", 0) + 1
            thin = False
        if st != "read" or thin:
            # Not a complete read: leave unprocessed so the next run tries again.
            if thin:
                stats["thin_body"] += 1
                report.thin_titles.append(art.title[:90])
                store.queue_review(
                    art.company_name or art.title[:60], art.url,
                    "article did not download — only the feed summary was available, "
                    "so companies inside it may be missing")
            continue

        store.mark_signal_processed(sid)
        shortfall = (art.raw or {}).get("shortfall")
        if shortfall:
            report.short_titles.append(art.title[:90])
            store.queue_review(art.title[:80], art.url,
                               f"article states {shortfall[0]} deals; "
                               f"{shortfall[1]} were found after two full reads")
        if not merged and _looks_like_a_deal(art):
            stats["possible_misses"] += 1
            report.miss_titles.append(art.title[:90])
            store.queue_review(art.company_name or art.title[:60], art.url,
                               "article looked like a funding story but no company "
                               "was extracted — check for a missed deal")

    # Articles with no financial content at all are complete as they stand.
    for a in fresh:
        if a not in readable:
            store.mark_signal_processed(a.signal_id())
            signals.append(a)

    # Articles given up on after repeated failures must not disappear quietly.
    for row in store.stale_unprocessed(int(cfg["run"].get("max_read_attempts", 4))):
        stats["gave_up"] += 1
        report.gave_up.append(row["signal_id"].split(":", 1)[-1])
        store.mark_signal_processed(row["signal_id"])   # reported once, not every run

    stats["signals"] = len(signals)
    budget = cfg["run"].get("max_enrichments_per_run", 25)
    carried_now: set[str] = set()
    # Leftovers from the previous run first (stable sort keeps the rest in order).
    signals.sort(key=lambda s: s.signal_id() not in carried_ids)

    for sig in signals:
        # 0. Already sent to the AE? Checked FIRST: a company already in the inbox must
        #    never be re-judged (and wrongly "rejected") from a thinner second article.
        if any((sig.domain_key, sig.slug_key, sig.name_key)):
            early, _r = store.dedup_check(sig.domain_key, sig.slug_key, sig.name_key)
            if early == "cooloff":
                stats["dedup"] += 1
                if sig.company_name and sig.company_name not in report.already_sent:
                    report.already_sent.append(sig.company_name)
                continue
        if sig.from_body and _recently_rejected(store, sig.name_key):
            # Clearly rejected in the last week: skip quietly, don't re-judge.
            stats["rejected_earlier"] = stats.get("rejected_earlier", 0) + 1
            if sig.company_name not in report.rejected_earlier:
                report.rejected_earlier.append(sig.company_name)
            continue
        # 1. hard gates (cheap, deterministic)
        passed, reason, detail = run_hard_gates(sig, cfg)
        if not passed:
            stats["gated"] += 1
            stage = reason.split(":")[0]
            # Only real company candidates are listed — an article that is itself a
            # roundup, or a headline with no company in it, is not a rejection.
            if sig.company_name and stage != "company" and (
                    sig.from_body or stage in ("india", "icp")):
                _reject(store, report, sig, stage, reason)
            store.log_drop(reason.split(":")[0], reason, sig.company_name, sig.url)
            continue

        # 2. dedup + 180-day cool-off
        # Same company mentioned by two articles in THIS run: the database only records
        # an alert once the email has gone out, so it cannot catch this on its own.
        run_keys = {k for k in (sig.domain_key, sig.slug_key, sig.name_key) if k}
        if run_keys & alerted_this_run or (
                sig.name_key and any(_same_company(sig.name_key, k)
                                     for k in alerted_names_this_run)):
            stats["dedup"] += 1
            continue
        state, _row = store.dedup_check(sig.domain_key, sig.slug_key, sig.name_key)
        if state == "cooloff":
            stats["dedup"] += 1
            if sig.company_name and sig.company_name not in report.already_sent:
                report.already_sent.append(sig.company_name)
            store.log_drop("dedup", "alerted within cool-off window",
                           sig.company_name, sig.url)
            continue
        is_follow_on = state == "follow_on"
        alerted_this_run |= run_keys
        if sig.name_key:
            alerted_names_this_run.add(sig.name_key)

        # 2b. Based abroad, India presence unknown: the AE's call, not ours. Listed in a
        #     short section of the email (no AI write-up — saves the daily allowance).
        if detail.get("india_unknown"):
            _list_india_unknown(store, report, sig, detail.get("hq", ""))
            continue

        # 3. headcount. An UNVERIFIED number may never delete a prospect: a guess that
        #    said "small" is how a 367-person company reached the inbox, and a guess
        #    that says "large" would silently bury a good one. Only a trusted source
        #    can disqualify; everything else is surfaced flagged, for a human check.
        hc = verify.verify_headcount(sig, cfg, detail, store=store)
        min_conf = cfg["verification"].get("min_confidence", 0.5)
        solid = hc.count is not None and hc.confidence >= min_conf

        # Asymmetric on purpose: a company's own stated team size is good enough to
        # EXCLUDE it, but only a LinkedIn-aligned source can mark it CONFIRMED.
        can_drop = solid and hc.source in verify.CAN_DISQUALIFY
        trusted = solid and hc.source in verify.LINKEDIN_ALIGNED

        # A SELF-REPORTED LinkedIn size band is a special case. Admins pick it from a
        # dropdown, usually once, usually optimistically — a 30-person company showing
        # "51-200" is common. So the band may confirm smallness, but may only
        # disqualify at a magnitude no amount of optimism explains.
        band = hc.self_reported_band
        if band:
            lo, hi = band
            floor = cfg["gates"]["headcount"].get("band_disqualify_floor", 201)
            if hi <= cfg["gates"]["headcount"]["max"]:
                can_drop, trusted = False, True          # e.g. "11-50": genuinely small
            elif lo >= floor:
                can_drop, trusted = True, True           # e.g. "1001-5000": really large
            else:
                # e.g. "51-200" — the ambiguous middle. Could be 30 people with a stale
                # dropdown, could be 180. Never dropped; surfaced for a ten-second check.
                can_drop, trusted = False, False
                hc.note = (f"{hc.note} — self-reported band, often overstated; "
                           f"check the people page")

        if can_drop:
            hc_gate = gate_headcount(hc.count, cfg)
            if not hc_gate.passed:
                stats["headcount"] += 1
                _reject(store, report, sig, "headcount",
                              f"{hc_gate.reason} (source: {hc.source})")
                store.log_drop("headcount", f"{hc_gate.reason} (source: {hc.source})",
                               sig.company_name, sig.url)
                continue
        if trusted:
            stats["hc_verified"] += 1
        else:
            # An UNCONFIRMED account still has to be PLAUSIBLE. Surfacing a 367-person
            # company for the AE to reject by hand is a failure, not a safety net —
            # so independent size signals can veto even without a confirmed count.
            ceiling = cfg["gates"]["headcount"].get("unconfirmed_ceiling", 80)
            # An ambiguous self-reported band is EXEMPT from the plausibility ceiling.
            # Its upper bound (200 for "51-200") is not a measurement of anything — it
            # is the top of a dropdown option. Applying the ceiling to it would drop
            # precisely the 30-person companies this branch exists to rescue.
            ambiguous_band = band is not None and not trusted
            raised = verify.total_raised_usd(sig)
            cap = cfg["gates"]["headcount"].get("max_total_raised_usd", 25_000_000)
            if raised >= cap:
                stats["headcount"] += 1
                _reject(store, report, sig, "headcount",
                              f"has raised about ${raised / 1e6:.0f}M in total")
                store.log_drop("headcount",
                               f"has raised ~${raised / 1e6:.0f}M to date — far above a "
                               f"1-{cfg['gates']['headcount']['max']} headcount",
                               sig.company_name, sig.url)
                continue
            # A stage-based GUESS ("Series B companies are usually ~130 people") is
            # never allowed to delete a prospect — only a stated or measured number.
            guessed = hc.source in ("stage_estimate", "estimate", "llm_estimate")
            if not ambiguous_band and not guessed and hc.count is not None \
                    and hc.count > ceiling:
                stats["headcount"] += 1
                _reject(store, report, sig, "headcount",
                              f"about {hc.count} staff according to {hc.source}")
                store.log_drop("headcount",
                               f"estimated {hc.count}, beyond the {ceiling} plausibility "
                               f"ceiling for an unconfirmed account",
                               sig.company_name, sig.url)
                continue
            stats["hc_unverified"] += 1

        # 4. enrichment (the only paid reasoning step)
        if budget <= 0 or _time.monotonic() > enrich_until:
            # Out of time or allowance: re-read this article next run rather than
            # lose the company. Nothing here is dropped.
            store.requeue_signal(sig.signal_id())
            carried_now.add(sig.signal_id())
            stats["time_deferred"] += 1
            report.carried_over.append(sig.company_name)
            continue
        budget -= 1
        account, reason = enrich(sig, hc, detail, is_follow_on, cfg)
        _parent = (sig.raw or {}).get("parent_title") or sig.title
        if (account is not None and (sig.raw or {}).get("ai_unreadable")) or (
                account is None and _HEADLINE_ONLY.search(reason or "")):
            report.note_headline_only(sig.url, _parent)
        if account is None:
            if reason.startswith("india_unknown"):
                _list_india_unknown(store, report, sig, reason.split(":", 1)[1].strip())
                continue
            if reason.startswith("ai_veto"):
                # The model rejected it outright — a drop, not something to review.
                stats["ai_veto"] += 1
                _reject(store, report, sig, "ai_veto", reason)
                store.log_drop("ai_veto", reason, sig.company_name, sig.url)
            else:
                stats["review"] += 1
                store.queue_review(sig.company_name, sig.url, reason)
            continue
        account.headcount_verified = trusted
        account.headcount_note = hc.note
        account.source_label = _source_label(sig)
        if (sig.raw or {}).get("publisher_url"):
            account.source_url = sig.raw["publisher_url"]   # the real article, not a Google link
        # The AI may name it differently from the headline ("ScaNxt" vs "ScaNxt
        # Scientific Technologies Pvt Ltd"). Check its final name against this run too.
        final_key = normalize_company_name(account.company_name)
        account.company_name = collapse_doubled_name(account.company_name)
        # The AI's cleaned-up name or website may match an account already sent even
        # when the article's raw name did not ("Clinics On Cloud" was sent twice).
        f_state, _ = store.dedup_check(normalize_domain(account.website or ""), "",
                                       final_key)
        if f_state == "cooloff":
            stats["dedup"] += 1
            if account.company_name not in report.already_sent:
                report.already_sent.append(account.company_name)
            continue
        parent_title = (sig.raw or {}).get("parent_title") or ""
        if sig.from_body and parent_title and not _ROUNDUP_TITLE.search(parent_title) \
                and not _name_in_title(account.company_name, parent_title) \
                and not _name_in_lead(account.company_name, sig.body):
            # Picked up from a list of headlines on a page about something else.
            account.source_label += (" (found in a headline list on this page — search "
                                     "the company name if the link doesn't mention it)")
        if final_key and final_key != sig.name_key and any(
                _same_company(final_key, k) for k in alerted_names_this_run):
            stats["dedup"] += 1
            continue
        if final_key:
            alerted_names_this_run.add(final_key)
        stats["enriched"] += 1

        # 5. collected now, dispatched once at the end of the run as a single digest
        account_id = store.upsert_account(
            account.company_name,
            normalize_domain(sig.domain_key or account.website),
            normalize_li_slug(sig.slug_key or account.linkedin_url),
            account.headcount, json.dumps(account.as_dict()),
            norm_name=normalize_company_name(account.company_name) or sig.name_key)
        alert_id = store.open_alert(account_id, json.dumps(account.as_dict()))
        wrote = append_row(account, cfg, dry_run)
        pending.append((account, account_id, alert_id, wrote))
        report.alerts.append((account.company_name, sig.source_id,
                              report.article_kind.get(sig.url, "individual")))
        report.by_source[sig.source_id]["alerts"] += 1
        log.info("QUALIFIED %s | HC %s | %s", account.company_name, account.headcount,
                 account.recommended_package)

    # 6. ONE digest for the whole run — six qualified accounts is one email, not six.
    #    Every email ends with Run Details: what happened and anything that looks wrong.
    from datetime import datetime as _now_dt, timezone as _now_tz
    report.finished = _now_dt.now(_now_tz.utc)
    stats["india_unknown"] = len(report.india_unknown)
    # Earlier runs that sent nothing are summarised at the end of the next email.
    try:
        report.quiet_runs = json.loads(store.get_state("quiet_runs") or "[]")
    except ValueError:
        report.quiet_runs = []
    emailed = send_digest([p[0] for p in pending], cfg, dry_run, stats, dead, report)

    # 7. Heartbeat. Silence should never be ambiguous: after N quiet hours, say so.
    hb = cfg["dispatch"].get("heartbeat", {})
    any_email = bool((pending or report.india_unknown) and emailed)
    if hb.get("enabled", True):
        every = float(hb.get("quiet_hours", 6))
        if (pending or report.india_unknown) and emailed:
            store.mark_now("last_email")
        else:
            quiet = store.hours_since("last_email")
            if quiet is None:
                store.mark_now("last_email")          # first ever run; start the clock
            elif quiet >= every:
                if send_heartbeat(cfg, stats, quiet, dry_run, dead, report):
                    any_email = True
                    store.mark_now("last_email")
                    store.mark_now("last_problem_email")
            elif has_problem(report, stats, dead):
                # Something is broken and there is no digest to carry the news. Say so
                # now rather than waiting hours for the heartbeat — at most every 2 hours
                # so a lasting fault does not flood the inbox.
                since = store.hours_since("last_problem_email")
                if since is None or since >= float(hb.get("problem_every_hours", 2)):
                    if send_heartbeat(cfg, stats, quiet, dry_run, dead, report,
                                      problem=True):
                        any_email = True
                        store.mark_now("last_problem_email")

    # Articles whose companies were not all finished: first in line next run.
    store.set_state("carried_over", _json.dumps(sorted(carried_now)))

    # Remember what this run did if nobody was told, so the next email can show it.
    if any_email:
        store.set_state("quiet_runs", "[]")
    else:
        head = overall(report, stats, dead)[0]
        report.quiet_runs.append(report.quiet_summary(stats, head))
        store.set_state("quiet_runs", json.dumps(report.quiet_runs[-60:]))

    for account, account_id, alert_id, wrote in pending:
        # The 180-day cool-off starts only once the account has actually been
        # delivered; a failed send is retried on the next run instead of being
        # silently swallowed.
        store.close_alert(alert_id, account_id, emailed, wrote)
        if emailed and wrote:
            stats["alerted"] += 1

    log.info("run complete %s", json.dumps(stats))
    return stats


def check_sources(cfg: dict[str, Any]) -> int:
    """`--check-sources`: poll every source once and report what each returned.

    This is the answer to "how do I know it is working without reading ten sites
    myself". Run it after any config change and whenever alerts go quiet.
    """
    print(f"{'SOURCE':24} {'TYPE':12} {'ITEMS':>6}  STATUS")
    print("-" * 72)
    broken = 0
    for source in cfg.get("sources", []):
        if source.get("enabled") is False:
            print(f"{source['id']:24} {source['type']:12} {'-':>6}  disabled")
            continue
        fn = ingest.ADAPTERS.get(source["type"])
        if not fn:
            print(f"{source['id']:24} {source['type']:12} {'-':>6}  NO ADAPTER")
            broken += 1
            continue
        try:
            got = fn(source)
            if got:
                print(f"{source['id']:24} {source['type']:12} {len(got):>6}  ok")
            else:
                print(f"{source['id']:24} {source['type']:12} {0:>6}  EMPTY - check "
                      f"the URL or selector")
                broken += 1
        except Exception as exc:                                # noqa: BLE001
            print(f"{source['id']:24} {source['type']:12} {'-':>6}  FAILED: "
                  f"{str(exc)[:40]}")
            broken += 1
    print("-" * 72)
    if broken:
        print(f"{broken} source(s) need attention. An empty source is silent data loss:\n"
              f"whole categories of companies simply never arrive.")
    else:
        print("All sources returned data.")
    return 1 if broken else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LTS Whitespace Alert Engine")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="single pass (scheduler drives cadence)")
    ap.add_argument("--dry-run", action="store_true", help="no email, no sheet write")
    ap.add_argument("--check-sources", action="store_true",
                    help="poll every source once and report what each returned")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.check_sources:
        return check_sources(cfg)
    dry = args.dry_run or cfg["run"].get("dry_run", False)
    process(cfg, dry_run=dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
