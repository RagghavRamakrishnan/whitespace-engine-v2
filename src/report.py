"""'Run Details' — a plain-language account of what one run did and what looks wrong.

The AE cannot read logs. Everything that could quietly stop the engine finding
companies (a dead feed, pages not downloading, the AI out of allowance, a crash, or
alerts coming only from weekly roundups) is turned into a sentence with a traffic
light, so a fault is visible in the inbox instead of hidden in a log.
"""
from __future__ import annotations

import html
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))

SOURCE_NAMES = {"entrackr": "Entrackr", "inc42": "Inc42", "yourstory": "YourStory",
                "ettech": "ET Tech", "vccircle": "VCCircle", "livemint_tech": "Mint",
                "moneycontrol_startup": "Moneycontrol", "google_news": "Google News",
                "mca_pas3": "MCA filings", "idex": "iDEX", "birac": "BIRAC", "tdb": "TDB",
                "yc_india": "Y Combinator", "antler": "Antler", "x_social": "X / Twitter"}

STAGE_WORDS = {"company": "not a company", "is_company": "not a company",
               "capital": "funding check", "india": "not India-based", "icp": "outside ICP",
               "headcount": "too large", "ai_veto": "AI review", "dedup": "already sent"}


def source_name(source_id: str) -> str:
    return SOURCE_NAMES.get(source_id, source_id)


# --------------------------------------------------------------- log capture
# Noise that is normal and says nothing about health.
_IGNORE = re.compile(r"discarding data: None|AFC is enabled|HTTP Request:|escalating to"
                     r"|time budget", re.I)     # time-outs have their own check

# Known technical messages -> what they mean for the AE.
_PLAIN = [
    (re.compile(r"feed download failed (\S+): (\d{3})"),
     lambda m: f"A news feed could not be downloaded ({_site(m.group(1))}, error "
               f"{m.group(2)}). If this repeats, that site's stories are not being seen."),
    (re.compile(r"source (\S+) failed: (.*)"),
     lambda m: f"The {source_name(m.group(1))} source crashed this run, so none of its "
               f"stories were checked. Technical detail: {m.group(2)[:120]}"),
    (re.compile(r"article reading stopped early: (.*?) —"),
     lambda m: f"The AI stopped reading articles part-way through. Unread articles "
               f"will be read on the next run. Technical detail: {m.group(1)[:120]}"),
    (re.compile(r"RESOURCE_EXHAUSTED|quota|429.*generativelanguage", re.I),
     lambda m: "The AI hit its usage limit. Unread articles wait for the next run; "
               "the daily allowance resets at 1:30 pm IST."),
    (re.compile(r"503|UNAVAILABLE|overloaded|temporarily unavailable", re.I),
     lambda m: "The AI service was briefly overloaded; the engine waited and retried."),
    (re.compile(r"429"),
     lambda m: "A news site asked the engine to slow down; it waited and retried."),
    (re.compile(r"Page\.goto|browser|playwright", re.I),
     lambda m: "The built-in browser could not open some pages."),
    (re.compile(r"smtp|heartbeat send failed", re.I),
     lambda m: "An email could not be sent. Technical detail: " + m.string[:120]),
    (re.compile(r"enrichment failed", re.I),
     lambda m: "The AI could not finish the write-up for a company; it was held back "
               "rather than sent half-done."),
]


def _site(url: str) -> str:
    m = re.search(r"//(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


class LogCollector(logging.Handler):
    """Keeps every warning and error raised during a run, for the report."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:                                       # noqa: BLE001
            return
        if not _IGNORE.search(msg):
            self.records.append((record.levelname, msg))

    def plain_problems(self) -> list[tuple[str, str, int]]:
        """[(level, plain sentence, how many times)] most serious first."""
        counts: Counter = Counter()
        level_of: dict[str, str] = {}
        for lvl, msg in self.records:
            text = None
            for pat, fn in _PLAIN:
                m = pat.search(msg)
                if m:
                    text = fn(m)
                    break
            if text is None:
                text = "Unusual technical message: " + msg[:160]
            counts[text] += 1
            if lvl == "ERROR" or level_of.get(text) != "ERROR":
                level_of[text] = lvl
        out = [(level_of[t], t, n) for t, n in counts.items()]
        out.sort(key=lambda x: (x[0] != "ERROR", -x[2]))
        return out


# ------------------------------------------------------------- run tracking
class RunReport:
    """Filled in by the orchestrator as the run goes."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.started = datetime.now(timezone.utc)
        self.finished: datetime | None = None
        self.by_source: dict[str, Counter] = defaultdict(Counter)
        self.source_errors: dict[str, str] = {}
        self.article_kind: dict[str, str] = {}          # url -> individual/roundup
        self.alerts: list[tuple[str, str, str]] = []    # (company, source, kind)
        self.rejected: list[tuple[str, str]] = []        # (company, plain reason)
        self.already_sent: list[str] = []
        self.rejected_earlier: list[str] = []   # clear "no" in the last 7 days, skipped
        self.thin_titles: list[str] = []
        self.miss_titles: list[str] = []
        self.short_titles: list[str] = []
        self.oldest_new_hours: float | None = None
        self.newest_alert_hours: list[float] = []
        self.logs = LogCollector()
        self.time_up_reading = False
        self.gave_up: list[str] = []
        self.gn_error = ""
        self.india_unknown: list[tuple[str, str, str, str]] = []  # name, hq, source, url
        self.carried_over: list[str] = []
        self.section_pages: list[str] = []
        self.gap_hours: float | None = None   # time since the previous run started   # home/section pages skipped (not stories)
        # Articles that downloaded but held only headlines / one-line mentions, so the
        # AI had no real story to judge companies from. url -> [title, companies hit]
        self.headline_only: dict[str, list] = {}
        # Short summaries of earlier runs that sent no email (newest last).
        self.quiet_runs: list[dict[str, Any]] = []

    def note_headline_only(self, url: str, title: str) -> None:
        entry = self.headline_only.setdefault(url or "?", [title or "untitled", 0])
        entry[1] += 1

    def quiet_summary(self, stats: dict[str, Any], head: str) -> dict[str, Any]:
        """What this run did, in a few words — kept until the next email goes out."""
        when = (self.finished or self.started).astimezone(IST).strftime("%a %d %b, %I:%M %p")
        return {"when": when, "new_read": max(stats.get("articles_read", 0)
                                              - stats.get("quota_blocked", 0), 0),
                "found": stats.get("companies_found", 0),
                "already": self.already_sent[:10],
                "rejected": [f"{c} ({r[:70]})" for c, r in self.rejected[:10]],
                "n_rejected": len(self.rejected), "head": head}

    def reject(self, company: str, stage: str, reason: str) -> None:
        if not company:
            return
        key = company.strip().lower()
        if any(c.strip().lower() == key for c, _ in self.rejected):
            return
        reason = re.sub(r"^\w+:\s*", "", reason or "").strip()
        reason = re.sub(r"^ai_veto:\s*", "", reason)
        stage = re.sub(r"^gate_", "", stage or "")
        label = STAGE_WORDS.get(stage, stage)
        if stage == "ai_veto" and re.search(
                r"not india|no (?:real |genuine )?india|india talent|presence .{0,40}india|"
                r"footprint .{0,20}india", reason, re.I):
            label = "not India-based"
        self.rejected.append((company, f"{label} — {reason}" if reason else label))


# ------------------------------------------------------------------ verdicts
OK, WATCH, PROBLEM, INFO = "OK", "WATCH", "PROBLEM", "INFO"
_ICON = {OK: "✅", WATCH: "⚠️", PROBLEM: "🔴", INFO: "ℹ️"}


def _checks(rep: RunReport, stats: dict[str, Any],
            dead: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """[(level, headline, what it means / what to do)]"""
    out: list[tuple[str, str, str]] = []
    cfg = rep.cfg
    enabled = [s["id"] for s in cfg.get("sources", []) if s.get("enabled") is not False]

    # 1. Did every source answer?
    broken = [source_name(s) for s in enabled if s in rep.source_errors]
    empty = [source_name(s) for s in enabled
             if s not in rep.source_errors and rep.by_source[s]["stories"] == 0]
    if broken:
        out.append((PROBLEM, f"{', '.join(broken)} failed this run",
                    "None of their stories were checked. A one-off blip is harmless; if "
                    "the same name shows up in several emails in a row, send me the log."))
    if empty:
        out.append((WATCH, f"{', '.join(empty)} returned no stories",
                    "Usually just a quiet feed. If it keeps happening, the feed address "
                    "may have changed."))
    if dead:
        names = ", ".join(f"{source_name(d['source_id'])} ({d['empty_streak']} runs)"
                          for d in dead)
        out.append((PROBLEM, f"Sources silent for a long time: {names}",
                    "Whole groups of companies are not being seen. Send me the log."))
    if not broken and not empty and not dead:
        out.append((OK, f"All {len(enabled)} news sources responded", ""))

    # 1b. Is the engine actually being woken every 15 minutes?
    gap = rep.gap_hours
    if gap is not None:
        g = (f"{gap * 60:.0f} minutes" if gap < 1.5 else f"{gap:.1f} hours")
        if gap > 2:
            out.append((PROBLEM, f"The previous run was {g} ago — the engine should run "
                                 f"every 15 minutes",
                        "Runs were missed, so news from that gap reached you late; the "
                        "engine catches up on those stories automatically. If GitHub "
                        "emailed you that runs failed, they started but crashed — send "
                        "me that log. If there was no such email, the timer that starts "
                        "the engine did not fire — tell me."))
        elif gap > 0.75:
            out.append((WATCH, f"The previous run was {g} ago (should be about 15 minutes)",
                        "A run or two was missed (skipped or failed). Occasional is fine; "
                        "if this repeats, tell me."))
        else:
            out.append((OK, f"Running on time — previous run {g} ago", ""))

    # 2. Did articles actually download in full?
    read = max(stats.get("articles_read", 0) - stats.get("quota_blocked", 0), 0)
    thin = stats.get("thin_body", 0)
    if read and thin:
        share = thin / read
        # One paywalled page on a quiet run is "1 of 1" = 100%; that is worth a
        # look, not a red "needs fixing". Red needs at least 3 affected articles.
        lvl = (PROBLEM if share > 0.4 and thin >= 3 else
               WATCH if share > 0.15 else INFO)
        ex = f" e.g. “{rep.thin_titles[0]}”" if rep.thin_titles else ""
        out.append((lvl, f"{thin} of {read} articles only partly downloaded",
                    "The engine saw only the summary, so a company named lower down "
                    f"could be missed. They are retried automatically next run.{ex}"))
    elif read and not rep.headline_only:
        out.append((OK, f"All {read} funding-related articles downloaded in full", ""))
    if read and rep.headline_only:
        n = len(rep.headline_only)
        title, hits = max(rep.headline_only.values(), key=lambda v: v[1])
        out.append((WATCH, f"{n} of {read} article(s) downloaded, but held only headlines "
                           f"— not a real story",
                    f"The page opened, but the AI found just one-line mentions with no "
                    f"funding details, so companies from it can't be judged properly. "
                    f"Example: “{title[:90]}” ({hits} compan{'y' if hits == 1 else 'ies'} "
                    f"affected). If you see this often from the same site, tell me."))

    if rep.section_pages:
        n = len(rep.section_pages)
        out.append((INFO, f"Skipped {n} link{'s' if n > 1 else ''} that went to a website's "
                          f"home or section page, not a story",
                    f"e.g. {_site(rep.section_pages[0])}"
                    f"{'/' + rep.section_pages[0].split('/', 3)[3] if rep.section_pages[0].count('/') >= 3 else ''}"
                    f" — these list old headlines. If you see a real funding story here, "
                    f"tell me."))

    # 2b. Google News links turned into the real article link?
    tried, ok = stats.get("gn_tried", 0), stats.get("gn_decoded", 0)
    if tried:
        if ok == tried:
            out.append((OK, f"Google News links: all {tried} opened as the real article", ""))
        elif ok == 0:
            out.append((PROBLEM, f"Google News links: 0 of {tried} could be opened directly",
                        "The link-conversion step is not working, so these articles fall back "
                        "to the slower browser (15 a run) and many are read from the headline "
                        "only. Send me the log."
                        + (f" Technical detail: {rep.gn_error}" if rep.gn_error else "")))
        else:
            out.append((WATCH if ok / tried < 0.8 else OK,
                        f"Google News links: {ok} of {tried} opened as the real article",
                        "The rest fell back to the browser. A few failures are normal."))

    # 2c. Was the browser available? (It is only a fallback for a few pages.)
    missed = stats.get("browser_missed", 0)
    if missed:
        out.append((WATCH, f"The browser wasn't available this run — {missed} page(s) "
                           f"that needed it were read from the plain download only",
                    "Every other article was read as normal. Those few pages may show "
                    "less text, so a company named lower down could be missed. If you "
                    "see this on several runs in a row, tell me."))

    # 3. Did the AI read everything it was meant to?
    waiting = stats.get("quota_blocked", 0)
    if waiting:
        out.append((WATCH, f"{waiting} article(s) waiting to be read",
                    "The run's time or AI allowance ran out. Nothing is lost — they "
                    "are first in line next run. If this number grows run after run, the "
                    "engine is falling behind; tell me."))
    if read >= 5 and stats.get("companies_found", 0) == 0 and not waiting:
        out.append((PROBLEM, f"Read {read} articles but found no companies at all",
                    "Reading may be broken. Send me the log."))
    if stats.get("gave_up"):
        ex = "; ".join(rep.gave_up[:3])
        out.append((WATCH, f"{stats['gave_up']} article(s) could not be read after 4 tries",
                    "Usually pages that never download. Each is reported once, then "
                    "dropped from this list."))

    # 4. Possible misses
    if stats.get("possible_misses"):
        ex = "; ".join(f"“{t}”" for t in rep.miss_titles[:3])
        out.append((WATCH, f"{stats['possible_misses']} funding-looking article(s) gave "
                    "no company", f"Worth a 10-second look: {ex}"))
    if stats.get("short_lists"):
        ex = "; ".join(f"“{t}”" for t in rep.short_titles[:2])
        out.append((WATCH, "A roundup lists more deals than the AI found",
                    f"Some companies in it may be missing: {ex}"))
    if stats.get("paywalled"):
        out.append((INFO, f"{stats['paywalled']} article(s) behind a paywall or login",
                    "Only the free part was read. The engine never logs in or bypasses "
                    "paywalls."))

    # 5. Are alerts coming from individual stories (early) or only weekly roundups (late)?
    kinds = Counter(k for _, _, k in rep.alerts)
    if rep.alerts:
        if kinds["roundup"] and kinds["roundup"] / len(rep.alerts) > 0.6:
            out.append((WATCH, f"{kinds['roundup']} of {len(rep.alerts)} alerts came from "
                        "roundups or articles covering several companies",
                        "Roundups arrive days after the news. Occasional is fine; if most "
                        "emails look like this, individual stories are being missed — tell "
                        "me."))
        else:
            out.append((OK, f"{kinds['individual']} of {len(rep.alerts)} alerts came from "
                        "individual funding stories", ""))
    # (A "roundup" here is any article naming 3+ funded companies, including profiles.)

    # 6. Headcount checking
    have_hc = bool(os.getenv("DIFFBOT_TOKEN") or os.getenv("PDL_API_KEY"))
    unv = len(rep.alerts)
    if unv and not have_hc:
        out.append((INFO, f"Headcount not checked for {'this company' if unv == 1 else f'these {unv} companies'}",
                    "No headcount data service is connected yet, so open each company's "
                    "LinkedIn people page before claiming it."))

    # 7. Time
    ai_read = max(stats.get("articles_read", 0) - stats.get("quota_blocked", 0), 0)
    if rep.time_up_reading and ai_read == 0:
        out.append((PROBLEM, "No time was left to read any articles",
                    "Collecting the news used up the whole run, so the AI read nothing. "
                    "Articles are queued, not lost, but if this repeats the engine never "
                    "catches up. Send me the log."))
    elif rep.time_up_reading or rep.carried_over:
        bits = []
        if rep.time_up_reading:
            bits.append("some articles were left for the next run")
        if rep.carried_over:
            names = ", ".join(rep.carried_over[:8])
            bits.append(f"{len(rep.carried_over)} compan"
                        f"{'y' if len(rep.carried_over) == 1 else 'ies'} will be finished "
                        f"on the next run ({names})")
        out.append((WATCH, "Ran out of time this run — nothing lost",
                    "The engine stops itself before GitHub's 12-minute limit: "
                    + "; ".join(bits) + ". Normal after a busy news spell. If it shows "
                    "up in most emails, the engine is falling behind; tell me."))

    # 8. Anything else unusual the log recorded
    seen_heads = {h for _, h, _ in out}
    for lvl, text, n in rep.logs.plain_problems()[:6]:
        level = PROBLEM if lvl == "ERROR" else WATCH
        if re.match(r"The AI service was briefly overloaded|The built-in browser|"
                    r"A news site asked", text):
            level = INFO      # handled automatically; shown for completeness only
        if broken and (text.startswith("A news feed could not be downloaded")
                       or re.match(r"The .* source crashed", text)):
            continue      # already reported above as "failed this run"
        head = text if n == 1 else f"{text} (×{n})"
        if head not in seen_heads:
            out.append((level, head, ""))
    order = {PROBLEM: 0, WATCH: 1, INFO: 2, OK: 3}
    out.sort(key=lambda x: order[x[0]])
    return out


def _split_rejected(rep: RunReport):
    """(foreign, other). Foreign ones become one scannable line of names; the rest keep
    their reasons, because those are the ones worth reading."""
    foreign, other = [], []
    for c, r in rep.rejected:
        if r.startswith("not India-based"):
            m = re.search(r"based in ([A-Z][\w .,]+?)(?=\s+(?:with|and|but)\b|[;.]|, no |$)", r)
            foreign.append(f"{c} ({m.group(1).strip()})" if m else c)
        else:
            other.append((c, r))
    return foreign, other


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _story_lines(rep: RunReport, stats: dict[str, Any]) -> list[str]:
    total = sum(c["stories"] for c in rep.by_source.values())
    old = stats.get("too_old", 0)
    seen = stats.get("seen", 0)
    new = total - old - seen
    money = stats.get("articles_read", 0)
    waiting = stats.get("quota_blocked", 0)
    read = max(money - waiting, 0)
    skipped = max(new - money, 0)
    found = stats.get("companies_found", 0)
    sent = len(rep.alerts)
    days = rep.cfg.get("run", {}).get("max_article_age_days", 3)
    lines = [
        f"Checked {sum(1 for s in rep.cfg.get('sources', []) if s.get('enabled') is not False)} "
        f"news sources and saw {total} stories.",
        f"  • {_n(old, 'was', 'were')} older than {days} days (ignored — too late to race "
        f"for).",
        f"  • {_n(seen, 'was', 'were')} already read on an earlier run.",
        f"  • {_n(max(new, 0), 'was', 'were')} new: {money} mention money, {skipped} "
        f"skipped (results, IPOs, launches, hiring news etc.).",
        f"  • Of the {money} about money, the AI read {read} in full"
        + (f"; {waiting} wait for the next run." if waiting else "."),
        f"The AI found {found} funding mentions. After removing duplicates and checks:",
        f"  • {sent} sent to you in this email",
        f"  • {len(rep.india_unknown)} with India presence unknown — listed for your call",
        f"  • {len(rep.rejected)} rejected (listed below — tell me if any look wrong)",
        f"  • {len(rep.already_sent)} already sent to you on an earlier run",
    ] + ([f"  • {len(rep.rejected_earlier)} rejected on an earlier run this week "
          f"(not re-checked)"] if rep.rejected_earlier else [])
    return lines


def _source_rows(rep: RunReport) -> list[tuple[str, int, int, int, int, int]]:
    rows = []
    for sid, c in rep.by_source.items():
        rows.append((source_name(sid), c["stories"], c["new"], c["read"],
                     c["companies"], c["alerts"]))
    rows.sort(key=lambda r: (-r[5], -r[2], r[0]))
    return rows


def overall(rep: RunReport, stats: dict[str, Any], dead: list[dict[str, Any]]):
    checks = _checks(rep, stats, dead)
    probs = sum(1 for c in checks if c[0] == PROBLEM)
    watch = sum(1 for c in checks if c[0] == WATCH)
    if probs:
        head = (f"🔴 {probs} problem{'s need' if probs > 1 else ' needs'} fixing")
    elif watch:
        head = f"⚠️ Worked, with {watch} thing{'s' if watch > 1 else ''} to keep an eye on"
    else:
        head = "✅ Everything worked normally"
    return head, checks, probs


def has_problem(rep: RunReport | None, stats: dict[str, Any],
                dead: list[dict[str, Any]]) -> bool:
    return bool(rep) and overall(rep, stats, dead)[2] > 0


# ------------------------------------------------------------------ rendering
def _quiet_lines(rep: RunReport) -> list[str]:
    """One plain line per earlier run that did not email, newest first."""
    out = []
    # Runs with no new articles and no issues are just counted, not listed one by one.
    idle = [q for q in rep.quiet_runs if not q["new_read"] and q["head"].startswith("✅")]
    busy = [q for q in rep.quiet_runs if q not in idle]
    if idle:
        out.append(f"{len(idle)} run{'s' if len(idle) > 1 else ''} had no new funding "
                   f"articles to read — all checks normal.")
    for q in reversed(busy[-12:]):
        bits = [f"read {q['new_read']} new article{'s' if q['new_read'] != 1 else ''}"]
        if q["new_read"]:
            bits.append(f"found {q['found']} compan{'y' if q['found'] == 1 else 'ies'}")
        if q["already"]:
            bits.append("already sent: " + ", ".join(q["already"]))
        if q["rejected"]:
            more = q["n_rejected"] - len(q["rejected"])
            bits.append("rejected: " + ", ".join(q["rejected"])
                        + (f" +{more} more" if more > 0 else ""))
        out.append(f"{q['when']} — " + "; ".join(bits) + f". {q['head']}")
    older = len(busy) - 12
    if older > 0:
        out.append(f"… and {older} earlier quiet run{'s' if older > 1 else ''}.")
    return out


def render_text(rep: RunReport | None, stats: dict[str, Any],
                dead: list[dict[str, Any]] | None = None) -> str:
    if rep is None:
        return ""
    dead = dead or []
    head, checks, _ = overall(rep, stats, dead)
    when = (rep.finished or rep.started).astimezone(IST).strftime("%a %d %b, %I:%M %p IST")
    L = ["RUN DETAILS — " + when, "", "Overall: " + head, "", "WHAT HAPPENED"]
    L += _story_lines(rep, stats)
    if rep.alerts:
        kinds = Counter(k for _, _, k in rep.alerts)
        L += ["", f"WHERE THIS EMAIL'S COMPANIES CAME FROM: {kinds['individual']} from "
              f"individual funding stories, {kinds['roundup']} from weekly roundups"]
    L += ["", "BY SOURCE (stories seen / new / read by AI / companies found / sent to you)"]
    for name, s, n, r, c, a in _source_rows(rep):
        L.append(f"  {name:14} {s:>4} / {n:>3} / {r:>3} / {c:>3} / {a:>3}")
    L += ["", "HEALTH CHECKS"]
    for lvl, h, d in checks:
        L.append(f"  {_ICON[lvl]} {h}")
        if d:
            L.append(f"      {d}")
    if rep.rejected:
        foreign, other = _split_rejected(rep)
        L += ["", "REJECTED THIS RUN (tell me if any should have come through)"]
        for c, r in other[:30]:
            L.append(f"  - {c}: {r[:200]}")
        if len(other) > 30:
            L.append(f"  … and {len(other) - 30} more")
        if foreign:
            L.append(f"  - Not India-based ({len(foreign)}): " + ", ".join(foreign))
    if rep.already_sent:
        L += ["", "ALREADY SENT EARLIER (skipped): " + ", ".join(rep.already_sent[:20])]
    if rep.rejected_earlier:
        L += ["", "REJECTED EARLIER THIS WEEK (skipped, not re-checked): "
              + ", ".join(rep.rejected_earlier[:20])]
    if rep.quiet_runs:
        L += ["", f"RUNS SINCE YOUR LAST EMAIL ({len(rep.quiet_runs)} — nothing new to send)"]
        L += ["  " + x for x in _quiet_lines(rep)]
    return "\n".join(L)


def render_html(rep: RunReport | None, stats: dict[str, Any],
                dead: list[dict[str, Any]] | None = None) -> str:
    if rep is None:
        return ""
    dead = dead or []
    e = html.escape
    head, checks, probs = overall(rep, stats, dead)
    colour = "#cf222e" if probs else "#9a6700" if "⚠" in head else "#1a7f37"
    when = (rep.finished or rep.started).astimezone(IST).strftime("%a %d %b, %I:%M %p IST")
    td = 'style="padding:3px 10px;border-bottom:1px solid #eaeef2"'
    h = [f'<div style="margin-top:28px;border-top:2px solid #d0d7de;padding-top:12px;'
         f'font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1f2328">',
         f'<h3 style="margin:0">Run Details</h3><div style="color:#57606a">{e(when)}</div>',
         f'<p style="font-size:15px;font-weight:600;color:{colour}">{e(head)}</p>',
         '<b>What happened</b><div style="margin:4px 0 10px">']
    for line in _story_lines(rep, stats):
        pad = "18px" if line.startswith("  •") else "0"
        h.append(f'<div style="margin-left:{pad}">{e(line.strip())}</div>')
    h.append("</div>")
    if rep.alerts:
        kinds = Counter(k for _, _, k in rep.alerts)
        h.append(f"<p><b>Where this email's companies came from:</b> "
                 f"{kinds['individual']} from individual funding stories, "
                 f"{kinds['roundup']} from weekly roundups.</p>")
    h.append("<b>Health checks</b><table style='border-collapse:collapse;margin:4px 0 10px'>")
    for lvl, hd, d in checks:
        h.append(f"<tr><td {td}>{_ICON[lvl]}</td><td {td}><b>{e(hd)}</b>"
                 f"{'<br><span style=color:#57606a>' + e(d) + '</span>' if d else ''}"
                 f"</td></tr>")
    h.append("</table><b>By source</b><table style='border-collapse:collapse;margin:4px 0 10px'>"
             f"<tr><th {td} align=left>Source</th><th {td}>Stories seen</th><th {td}>New</th>"
             f"<th {td}>Read by AI</th><th {td}>Companies found</th><th {td}>Sent to you</th></tr>")
    for name, s, n, r, c, a in _source_rows(rep):
        h.append(f"<tr><td {td}>{e(name)}</td><td {td} align=center>{s}</td>"
                 f"<td {td} align=center>{n}</td><td {td} align=center>{r}</td>"
                 f"<td {td} align=center>{c}</td><td {td} align=center>{a}</td></tr>")
    h.append("</table>")
    if rep.rejected:
        foreign, other = _split_rejected(rep)
        h.append("<b>Rejected this run</b> <span style='color:#57606a'>— tell me if any "
                 "should have come through</span><ul style='margin:4px 0 10px'>")
        for c, r in other[:30]:
            h.append(f"<li><b>{e(c)}</b>: {e(r[:200])}</li>")
        if len(other) > 30:
            h.append(f"<li>… and {len(other) - 30} more</li>")
        if foreign:
            h.append(f"<li><b>Not India-based ({len(foreign)}):</b> "
                     f"{e(', '.join(foreign))}</li>")
        h.append("</ul>")
    if rep.already_sent:
        h.append("<p><b>Already sent earlier (skipped):</b> "
                 f"{e(', '.join(rep.already_sent[:20]))}</p>")
    if rep.rejected_earlier:
        h.append("<p><b>Rejected earlier this week (skipped, not re-checked):</b> "
                 f"{e(', '.join(rep.rejected_earlier[:20]))}</p>")
    if rep.quiet_runs:
        h.append(f"<b>Runs since your last email</b> <span style='color:#57606a'>— "
                 f"{len(rep.quiet_runs)} run{'s' if len(rep.quiet_runs) > 1 else ''} with "
                 f"nothing new to send</span><ul style='margin:4px 0 10px'>")
        h += [f"<li>{e(x)}</li>" for x in _quiet_lines(rep)]
        h.append("</ul>")
    h.append("</div>")
    return "".join(h)


def _li_search(name: str) -> str:
    from urllib.parse import quote
    return "https://www.linkedin.com/search/results/companies/?keywords=" + quote(name)


def india_unknown_text(rep: "RunReport | None") -> str:
    if not rep or not rep.india_unknown:
        return ""
    L = [f"INDIA PRESENCE UNKNOWN — YOUR CALL ({len(rep.india_unknown)})",
         "Funded, but the article doesn't show whether there is an India team (based abroad, "
         "or location not stated). Open LinkedIn → People → filter by location India."]
    for name, hq, src, url in rep.india_unknown:
        L.append(f"  - {name} — {hq} — {src}\n      article: {url}\n      LinkedIn: {_li_search(name)}")
    return "\n".join(L)


def india_unknown_html(rep: "RunReport | None") -> str:
    if not rep or not rep.india_unknown:
        return ""
    e = html.escape
    h = ['<div style="margin-top:22px;font-family:Segoe UI,Arial,sans-serif;font-size:13px">',
         f'<h3 style="margin:0 0 4px">India presence unknown — your call ({len(rep.india_unknown)})</h3>',
         '<div style="color:#57606a;margin-bottom:6px">Funded, but the article does not '
         "show whether there is an India team (based abroad, or location not stated). "
         'Open LinkedIn → People → filter by location India.</div><ul style="margin:0">']
    for name, hq, src, url in rep.india_unknown:
        h.append(f'<li><b>{e(name)}</b> — {e(hq)} — <a href="{e(url)}">article</a> · '
                 f'<a href="{e(_li_search(name))}">LinkedIn</a>'
                 f'<span style="color:#57606a"> ({e(src[:80])})</span></li>')
    h.append("</ul></div>")
    return "".join(h)
