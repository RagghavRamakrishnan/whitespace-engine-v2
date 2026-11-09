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
    (re.compile(r"503|UNAVAILABLE|overloaded", re.I),
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
        self.thin_titles: list[str] = []
        self.miss_titles: list[str] = []
        self.short_titles: list[str] = []
        self.oldest_new_hours: float | None = None
        self.newest_alert_hours: list[float] = []
        self.logs = LogCollector()
        self.time_up_reading = False
        self.gave_up: list[str] = []
        self.gn_error = ""
        self.carried_over: list[str] = []

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
        if stage == "ai_veto" and re.search(r"not india|no real india|india talent", reason, re.I):
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

    # 2. Did articles actually download in full?
    read = max(stats.get("articles_read", 0) - stats.get("quota_blocked", 0), 0)
    thin = stats.get("thin_body", 0)
    if read and thin:
        share = thin / read
        lvl = PROBLEM if share > 0.4 else WATCH if share > 0.15 else INFO
        ex = f" e.g. “{rep.thin_titles[0]}”" if rep.thin_titles else ""
        out.append((lvl, f"{thin} of {read} articles only partly downloaded",
                    "The engine saw only the summary, so a company named lower down "
                    f"could be missed. They are retried automatically next run.{ex}"))
    elif read:
        out.append((OK, f"All {read} funding-related articles downloaded in full", ""))

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
                        "weekly roundups",
                        "Roundups arrive days after the news. Occasional is fine; if most "
                        "emails look like this, individual stories are being missed — tell "
                        "me."))
        else:
            out.append((OK, f"{kinds['individual']} of {len(rep.alerts)} alerts came from "
                        "individual funding stories", ""))

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
            m = re.search(r"based in ([A-Z][\w .]+?)(?:[,;]|$)", r)
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
        f"  • {len(rep.rejected)} rejected (listed below — tell me if any look wrong)",
        f"  • {len(rep.already_sent)} already sent to you on an earlier run",
    ]
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
    h.append("</div>")
    return "".join(h)
