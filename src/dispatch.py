"""Dispatch: ONE digest email per run + tracker rows.

One email per run, not one per company. A run that qualifies six accounts sends a
single message listing all six; a run that qualifies none sends nothing at all.

Uses plain SMTP and a CSV rather than Microsoft Graph: Graph would send as the AE and
write to their work OneDrive, both of which need corporate IT approval. This path needs
permission from nobody.
"""
from __future__ import annotations

import csv
import html
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Sequence

from .models import QualifiedAccount, SHEET_HEADERS

log = logging.getLogger(__name__)


# ------------------------------------------------------- verification links
def people_page_url(a: QualifiedAccount) -> str:
    """Direct link to the company's LinkedIn people tab — where the AE confirms the
    real headcount in about ten seconds. The engine never scrapes this page; it just
    points a human at it."""
    if a.linkedin_url:
        return a.linkedin_url.rstrip("/") + "/people/"
    query = a.company_name.replace(" ", "%20")
    return f"https://www.linkedin.com/search/results/companies/?keywords={query}"


_SOURCE_LABEL = {"google_cse": "the LinkedIn page, via Google", "diffbot": "Diffbot",
                 "pdl": "People Data Labs",
                 "partner_api": "data provider", "internal": "internal data",
                 "press_statement": "team size stated in the article",
                 "stage_estimate": "inferred from stage"}


def _confidence_note(a: QualifiedAccount) -> str:
    """The number that matters is the one on the LinkedIn people page, so even a
    confirmed figure is described by its source rather than presented as fact."""
    label = _SOURCE_LABEL.get(a.headcount_source, a.headcount_source)
    if a.headcount_verified:
        return f"from {label}"
    if a.headcount_note.startswith("LinkedIn band "):
        return ("LinkedIn's self-reported band — often stale or overstated. "
                "The people page shows the real member count")
    return f"UNCONFIRMED ({label}) — open the people page"


def split_by_confidence(accounts: Sequence[QualifiedAccount]):
    """Confirmed accounts first; unconfirmed ones are still shown, never dropped."""
    return ([a for a in accounts if a.headcount_verified],
            [a for a in accounts if not a.headcount_verified])


# -------------------------------------------------------------- rendering
def digest_subject(accounts: Sequence[QualifiedAccount]) -> str:
    n = len(accounts)
    if n == 1:
        a = accounts[0]
        tag = " [Follow-on]" if a.is_follow_on else ""
        return f"[Whitespace] {a.company_name} | {a.round_type} | HC: {a.headcount}{tag}"
    names = ", ".join(a.company_name for a in accounts[:3])
    more = f" +{n - 3} more" if n > 3 else ""
    return f"[Whitespace] {n} new accounts — {names}{more}"


def render_account_text(a: QualifiedAccount, idx: int) -> str:
    dms = a.decision_makers or []
    dm_lines = "\n".join(
        f"   {i + 1}. {d.get('name', '—')} — {d.get('title', '—')} | "
        f"{d.get('linkedin_url') or 'profile not stated in source'}"
        for i, d in enumerate(dms[:2])) or "   Not identified in source"
    return f"""
{'=' * 68}
{idx}. {a.company_name}{' [FOLLOW-ON ROUND]' if a.is_follow_on else ''}
{'=' * 68}
• Capital trigger : {a.funding_amount} ({a.round_type})
• Headcount       : {a.headcount_display()} — {_confidence_note(a)}
  Confirm here    : {people_page_url(a)}
• Base            : {a.india_city}
• Segment         : {a.industry_icp}
• Website         : {a.website or '—'}
• LinkedIn        : {a.linkedin_url or '—'}

DECISION MAKERS
{dm_lines}

HIRING FRICTION
• Bottleneck      : {a.bottleneck}
• Trigger         : {a.expansion_trigger or 'Post-funding scale-up (inferred from stage and sector)'}

LTS FIT
• Package         : {a.recommended_package}
• Positioning     : {a.positioning}

INMAIL DRAFT
"{a.inmail_hook}"

Source: {a.source_url}
"""


def render_digest_text(accounts: Sequence[QualifiedAccount]) -> str:
    confirmed, unconfirmed = split_by_confidence(accounts)
    out = [f"{len(accounts)} account(s) passed the whitespace filters.\n"]
    n = 0
    if confirmed:
        out.append(f"\n### HEADCOUNT CONFIRMED ({len(confirmed)}) — "
                   "from a LinkedIn-derived source\n")
        for a in confirmed:
            n += 1
            out.append(render_account_text(a, n))
    if unconfirmed:
        out.append(f"\n### HEADCOUNT NOT CONFIRMED ({len(unconfirmed)})\n"
                   "Not necessarily too large: LinkedIn size bands are self-reported "
                   "and often stale, so a '51-200' company may really have 30 people. "
                   "Open the confirm link to see the real member count.\n")
        for a in unconfirmed:
            n += 1
            out.append(render_account_text(a, n))
    return "".join(out)


def _section_heading(title: str, note: str, colour: str) -> str:
    return (f'<div style="margin:22px 0 10px;padding-left:10px;border-left:4px solid {colour}">'
            f'<div style="font-weight:700;font-size:15px">{title}</div>'
            f'<div style="color:#57606a;font-size:12px">{note}</div></div>')


def _render_cards(accounts: Sequence[QualifiedAccount], start: int = 1) -> str:
    cards = []
    for i, a in enumerate(accounts, start):
        dms = a.decision_makers or []
        dm_html = "".join(
            f'<li>{html.escape(d.get("name", "—"))} — {html.escape(d.get("title", "—"))}'
            + (f' · <a href="{html.escape(d["linkedin_url"])}">profile</a>'
               if d.get("linkedin_url") else ' · <i>profile not in source</i>') + '</li>'
            for d in dms[:2]) or "<li><i>Not identified in source</i></li>"
        hc_style = ("color:#1a7f37;font-weight:600" if a.headcount_verified
                    else "color:#9a6700;font-weight:600")
        follow = ('<span style="background:#fff8c5;padding:2px 6px;border-radius:3px;'
                  'font-size:12px">FOLLOW-ON</span>') if a.is_follow_on else ""
        cards.append(f"""
<div style="border:1px solid #d0d7de;border-radius:8px;padding:16px;margin-bottom:16px">
  <div style="font-size:17px;font-weight:700;color:#0a66c2">{i}. {html.escape(a.company_name)} {follow}</div>
  <div style="color:#57606a;font-size:13px;margin:2px 0 12px">
    {html.escape(a.industry_icp)} · {html.escape(a.india_city)} · {html.escape(a.funding_amount)} {html.escape(a.round_type)}
  </div>
  <p style="margin:6px 0"><b>Headcount:</b> <span style="{hc_style}">{html.escape(a.headcount_display())}</span>
    <span style="color:#57606a">({html.escape(_confidence_note(a))})</span> —
    <a href="{html.escape(people_page_url(a))}">confirm on LinkedIn</a></p>
  <p style="margin:6px 0"><b>Decision makers:</b></p>
  <ul style="margin:4px 0 10px">{dm_html}</ul>
  <p style="margin:6px 0"><b>Bottleneck:</b> {html.escape(a.bottleneck)}</p>
  <p style="margin:6px 0"><b>Trigger:</b> {html.escape(a.expansion_trigger or 'Post-funding scale-up (inferred)')}</p>
  <p style="margin:6px 0"><b>Package:</b> {html.escape(a.recommended_package)} —
     <span style="color:#57606a">{html.escape(a.positioning)}</span></p>
  <div style="background:#f6f8fa;border-left:3px solid #0a66c2;padding:10px 12px;margin-top:10px">
    <div style="font-size:12px;color:#57606a;margin-bottom:4px">INMAIL DRAFT — edit before sending</div>
    {html.escape(a.inmail_hook)}
  </div>
  <div style="font-size:12px;margin-top:10px">
    <a href="{html.escape(a.website or '#')}">website</a> ·
    <a href="{html.escape(a.linkedin_url or '#')}">company page</a> ·
    <a href="{html.escape(a.source_url)}">source article</a>
  </div>
</div>""")
    return "".join(cards)


def render_digest_html(accounts: Sequence[QualifiedAccount]) -> str:
    confirmed, unconfirmed = split_by_confidence(accounts)
    body = ""
    if confirmed:
        body += _section_heading(
            f"Headcount confirmed ({len(confirmed)})",
            "Checked against a LinkedIn-derived data source. Still worth a glance.",
            "#1a7f37")
        body += _render_cards(confirmed, 1)
    if unconfirmed:
        body += _section_heading(
            f"Headcount not confirmed ({len(unconfirmed)})",
            "Not necessarily too large — LinkedIn size bands are self-reported and often "
            "stale. Open the confirm link to see the real member count.",
            "#9a6700")
        body += _render_cards(unconfirmed, len(confirmed) + 1)
    return (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1f2328;'
        'max-width:760px">'
        f'<h2 style="margin:0 0 4px">{len(accounts)} new whitespace account'
        f'{"s" if len(accounts) != 1 else ""}</h2>'
        + body + '</div>')


# ----------------------------------------------------------------- sending
def _recall_footer(stats: dict[str, int] | None,
                   dead_sources: Sequence[dict[str, Any]] | None = None) -> str:
    """Surface how much was actually read, and anything that stopped working.

    A quiet inbox because nothing qualified and a quiet inbox because a source died
    look identical from the outside. This is the only thing that tells them apart.
    """
    if not stats:
        return ""
    lines = [f"Read {stats.get('articles_read', 0)} articles, "
             f"found {stats.get('companies_found', 0)} companies with funding events."]
    if stats.get("expanded"):
        lines.append(f"{stats['expanded']} article(s) had hidden text — opened in a browser "
                     f"and expanded with 'read more' to read them in full.")

    warnings = []
    if stats.get("possible_misses"):
        warnings.append(f"{stats['possible_misses']} article(s) looked like funding "
                        f"stories but yielded no company")
    if stats.get("paywalled"):
        warnings.append(f"{stats['paywalled']} article(s) were behind a paywall — only the "
                        f"free part could be read, so companies further down may be missing")
    if stats.get("short_lists"):
        warnings.append(f"{stats['short_lists']} roundup(s) state more deals than were "
                        f"found even after a second full read — see the review queue")
    if stats.get("thin_body"):
        warnings.append(f"{stats['thin_body']} article(s) would not download — only "
                        f"the feed summary was available, so companies inside them "
                        f"may be missing")
    if stats.get("quota_blocked"):
        warnings.append(f"{stats['quota_blocked']} article(s) deferred: the AI hit its "
                        f"daily limit. They will be re-read on the next run")
    if stats.get("gave_up"):
        warnings.append(f"{stats['gave_up']} article(s) failed repeatedly and were "
                        f"given up on — see the review queue")
    if warnings:
        lines.append("")
        lines.append("NEEDS ATTENTION:")
        lines.extend(f"  - {w}" for w in warnings)

    if dead_sources:
        lines.append("")
        lines.append("SOURCES RETURNING NOTHING:")
        for d in dead_sources:
            last = d.get("last_ok_at") or "never"
            lines.append(f"  - {d['source_id']}: {d['empty_streak']} runs empty "
                         f"(last worked: {last[:10] if last != 'never' else 'never'})")
        lines.append("  A dead source means whole categories of companies are not "
                     "being seen at all.")
    return "\n".join(lines)


def send_digest(accounts: Sequence[QualifiedAccount], cfg: dict[str, Any],
                dry_run: bool = False, stats: dict[str, int] | None = None,
                dead_sources: Sequence[dict[str, Any]] | None = None) -> bool:
    """One email for the whole run. No accounts means no email."""
    if not accounts:
        log.info("nothing qualified this run — no email sent")
        return True

    e = cfg["dispatch"]["email"]
    to_addr = os.getenv("ALERT_TO") or e.get("to", "")
    subject = digest_subject(accounts)

    if dry_run:
        print("=" * 72)
        print("TO:", to_addr)
        print("SUBJECT:", subject)
        print(render_digest_text(accounts))
        if _recall_footer(stats, dead_sources):
            print("---\n" + _recall_footer(stats, dead_sources))
        return True

    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    if not user or not password:
        log.error("SMTP_USER / SMTP_PASSWORD not set — cannot send alert")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{e.get('from_name', 'Whitespace Engine')} <{user}>"
    msg["To"] = to_addr
    footer = _recall_footer(stats, dead_sources)
    msg.set_content(render_digest_text(accounts) + ("\n\n---\n" + footer if footer else ""))
    if e.get("format", "html") == "html":
        html_footer = (f'<p style="color:#57606a;font-size:12px;border-top:1px solid '
                       f'#d0d7de;padding-top:10px">{html.escape(footer)}</p>'
                       if footer else "")
        msg.add_alternative(render_digest_html(accounts) + html_footer, subtype="html")

    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST", e.get("smtp_host", "smtp.gmail.com")),
                          int(os.getenv("SMTP_PORT", e.get("smtp_port", 587))),
                          timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        log.info("digest sent with %d account(s)", len(accounts))
        return True
    except Exception as exc:                                    # noqa: BLE001
        log.error("smtp send failed: %s", exc)
        return False


def send_heartbeat(cfg: dict[str, Any], stats: dict[str, int], quiet_hours: float,
                   dry_run: bool = False,
                   dead_sources: Sequence[dict[str, Any]] | None = None) -> bool:
    """'Still running, nothing qualified' email.

    Silence is ambiguous: no alerts could mean a quiet news day or a dead engine.
    This removes the ambiguity without adding noise, by reporting only after a set
    number of quiet hours and summarising what was actually looked at.
    """
    e = cfg["dispatch"]["email"]
    subject = f"[Whitespace] All quiet — nothing qualified in the last {quiet_hours:.0f} hours"
    body = (
        f"No new accounts passed the filters in the last {quiet_hours:.0f} hours.\n"
        f"The engine is running normally — this note confirms it.\n\n"
        f"Most recent scan:\n"
        f"  • stories seen           : {stats.get('ingested', 0)}\n"
        f"  • articles read in full  : {stats.get('articles_read', 0)}\n"
        f"  • companies extracted    : {stats.get('companies_found', 0)}\n"
        f"  • possible missed deals  : {stats.get('possible_misses', 0)}\n"
        f"  • already seen           : {stats.get('seen', 0)}\n"
        f"  • filtered out           : {stats.get('gated', 0)}\n"
        f"  • rejected by AI review  : {stats.get('ai_veto', 0)}\n"
        f"  • too large (confirmed)  : {stats.get('headcount', 0)}\n"
        f"  • already alerted        : {stats.get('dedup', 0)}\n\n"
        f"If 'stories seen' is 0, the news sources are unreachable — that IS a fault.\n"
        f"If 'articles read' is high but 'companies extracted' is 0, extraction is\n"
        f"failing — also a fault, and the one that silently loses accounts.\n"
        f"Anything else means the filters simply found nothing worth your time.\n")
    extra = _recall_footer(stats, dead_sources)
    if extra:
        body += "\n" + extra + "\n"
    if dry_run:
        print("=" * 72)
        print("HEARTBEAT:", subject)
        print(body)
        return True

    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    if not user or not password:
        log.error("SMTP credentials missing — cannot send heartbeat")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{e.get('from_name', 'Whitespace Engine')} <{user}>"
    msg["To"] = os.getenv("ALERT_TO") or e.get("to", "")
    msg.set_content(body)
    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST", e.get("smtp_host", "smtp.gmail.com")),
                          int(os.getenv("SMTP_PORT", e.get("smtp_port", 587))),
                          timeout=30) as srv:
            srv.starttls()
            srv.login(user, password)
            srv.send_message(msg)
        log.info("heartbeat sent after %.1f quiet hours", quiet_hours)
        return True
    except Exception as exc:                                    # noqa: BLE001
        log.error("heartbeat send failed: %s", exc)
        return False


# ------------------------------------------------------------------ tracker
def append_row(a: QualifiedAccount, cfg: dict[str, Any], dry_run: bool = False) -> bool:
    """Append one row to the master CSV. Opens directly in Excel; the workflow commits
    it back to the repo so it survives between runs."""
    t = cfg["dispatch"]["tracker"]
    if not t.get("enabled", True):
        return True
    if dry_run:
        log.info("[dry-run] tracker row: %s", a.to_row())
        return True
    path = Path(t.get("csv_path", "master_tracker.csv"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            if is_new:
                w.writerow(SHEET_HEADERS)
            w.writerow(a.to_row())
        return True
    except Exception as exc:                                    # noqa: BLE001
        log.error("tracker append failed: %s", exc)
        return False


def append_row_gsheets(a: QualifiedAccount, spreadsheet_id: str, sheet: str = "Master") -> bool:
    """Optional upgrade: write live to Google Sheets instead of the CSV."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_SA_JSON"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    svc = build("sheets", "v4", credentials=creds)
    svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range=f"{sheet}!A:O",
        valueInputOption="USER_ENTERED", body={"values": [a.to_row()]}).execute()
    return True
