"""LLM enrichment: structured output, schema-validated, with a bounded retry.

Runs LAST in the pipeline — only on accounts that already cleared dedup, the hard
gates and headcount verification. That ordering is what keeps this cheap.
"""
from __future__ import annotations

import json
import re
import logging
import os
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .models import HeadcountResult, QualifiedAccount, RawSignal
from .prompts import SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger(__name__)
SCHEMA = json.loads((Path(__file__).parent.parent / "schemas" / "enrichment.schema.json")
                    .read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


# --------------------------------------------------------------- LLM clients
# Providers send the system prompt EXACTLY as given. Each caller attaches its own
# output format. (Previously every call had the enrichment schema appended — including
# article-reading calls, which need a different shape — giving the model two
# contradictory instructions and risking an empty result for a real roundup.)
#
# Output is capped generously: a roundup listing 20+ companies produces a long JSON
# answer, and a cut-off answer must never be mistaken for "nothing found".
MAX_OUTPUT_TOKENS = 16000


def _call_openai(system: str, user: str, cfg: dict[str, Any]) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=cfg["llm"]["model"],
        temperature=cfg["llm"].get("temperature", 0.3),
        max_tokens=MAX_OUTPUT_TOKENS,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or "{}"


def _call_groq(system: str, user: str, cfg: dict[str, Any]) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["GROQ_API_KEY"],
                    base_url="https://api.groq.com/openai/v1")
    resp = client.chat.completions.create(
        model=cfg["llm"]["model"],
        temperature=cfg["llm"].get("temperature", 0.3),
        max_tokens=MAX_OUTPUT_TOKENS,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or "{}"


def _call_anthropic(system: str, user: str, cfg: dict[str, Any]) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=cfg["llm"]["model"], max_tokens=MAX_OUTPUT_TOKENS,
        temperature=cfg["llm"].get("temperature", 0.3),
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def _call_google(system: str, user: str, cfg: dict[str, Any]) -> str:
    """Gemini via the current Google GenAI SDK (`google-genai`)."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    resp = client.models.generate_content(
        model=cfg["llm"]["model"],
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=cfg["llm"].get("temperature", 0.3),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
        ),
    )
    # A response stopped by the length limit is incomplete, not empty.
    try:
        reason = str(resp.candidates[0].finish_reason).upper()
        if "MAX_TOKENS" in reason:
            raise RuntimeError("model output truncated at the length limit")
    except (IndexError, AttributeError, TypeError):
        pass
    return resp.text or "{}"


PROVIDERS = {"groq": _call_groq, "openai": _call_openai,
             "anthropic": _call_anthropic, "google": _call_google}


# Rejection reasons that are really "I couldn't read the page", not facts about the
# company.
# The AI sometimes rejects a funded company only because a roundup gave one line about
# it ("mentioned only in a monthly funding roundup with no details") while letting its
# neighbours on the same list through. Thin coverage is not a disqualifier — the AE
# judges these. A reason that ALSO names a clear disqualifier still rejects.
_ROUNDUP_THIN = re.compile(
    r"round[- ]?up|(?:mentioned|named|listed) only|only (?:mentioned|named|listed)|"
    r"no (?:specific |distinct |standalone |further )?(?:corporate |company )?details|"
    r"without (?:sufficient|enough|any) (?:\w+ ){0,3}details|funding (?:list|digest)", re.I)
_HARD_NO = re.compile(
    r"well[- ]known|far (?:more|above|exceed)|more than 50|over 50|hundreds of|"
    r"not an operating company|not a company|venture fund|investment firm|accelerator|"
    r"government|association|publisher|award|acquisition|acquired|rumou?r|in talks|"
    r"no (?:genuine )?new funding|not a (?:genuine )?(?:new )?funding|rights issue|"
    r"parent[- ]company|india|abroad|foreign", re.I)

_UNREADABLE = re.compile(
    r"error page|no (?:actual |article |real )?content|could ?n[o']?t (?:be )?"
    r"(?:read|load|access|resolve)|unresolvable|insufficient|sparse|"
    r"lack(?:s|ing)? (?:of )?(?:detail|content|information|text)|"
    r"not (?:enough|sufficient) (?:detail|information|text)|page (?:is )?unavailable|"
    r"only (?:a |the )?headline|"
    # Sites that block automated readers serve a security-check page instead of the
    # story. The AI then "rejects" the company for having no article — wrongly.
    r"cloudflare|captcha|bot[- ](?:protection|check|detection|challenge)|"
    r"security (?:check|challenge|verification|page)|access denied|"
    r"verify (?:that )?(?:you are|you're) (?:a )?human|just a moment", re.I)

_last_call_at = 0.0


def call_llm(system: str, user: str, cfg: dict[str, Any]) -> str:
    """Raw LLM call, rate-limited, shared with the article-extraction layer.

    Free tiers cap requests per MINUTE as well as per day (Gemini's free tier is as
    low as 5/min on some models). A run that reads ten batches back-to-back would
    burst straight through that and start failing mid-run, so calls are spaced out.
    """
    global _last_call_at
    rpm = float(cfg["llm"].get("max_requests_per_minute", 5))
    if rpm > 0:
        min_gap = 60.0 / rpm
        wait = min_gap - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
    _last_call_at = time.monotonic()
    # "Model is experiencing high demand" (503) and similar server hiccups are
    # temporary. Wait and try again rather than writing the articles off. Daily-limit
    # errors are NOT retried here; they are handled by deferring to the next run.
    waits = [10, 30, 60]
    for attempt in range(len(waits) + 1):
        try:
            return PROVIDERS[cfg["llm"]["provider"]](system, user, cfg)
        except Exception as exc:                                # noqa: BLE001
            msg = str(exc).lower()
            transient = any(m in msg for m in ("503", "unavailable", "high demand",
                                                "overloaded", "500 internal", "deadline",
                                                "timed out", "timeout", "502", "504"))
            if not transient or attempt == len(waits):
                raise
            log.warning("AI temporarily unavailable (%s) — retrying in %ss",
                        str(exc)[:80], waits[attempt])
            time.sleep(waits[attempt])
            _last_call_at = time.monotonic()


def _parse(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    return json.loads(raw)


def enrich(sig: RawSignal, hc: HeadcountResult, detail: dict[str, Any],
           is_follow_on: bool, cfg: dict[str, Any]
           ) -> tuple[QualifiedAccount | None, str]:
    """Returns (account, reason). account is None when the model vetoed the record or
    the call failed; `reason` explains which, for the drop log."""
    system = (SYSTEM_PROMPT + "\n\nReturn JSON conforming to this schema:\n"
              + json.dumps(SCHEMA))
    user = build_user_prompt(sig, hc.count, hc.source, detail, is_follow_on)
    call = call_llm

    last_err = ""
    for attempt in range(cfg["llm"].get("max_retries", 2) + 1):
        try:
            data = _parse(call(system, user if not last_err else
                               f"{user}\n\nYour previous output was rejected: {last_err}\n"
                               f"Return corrected JSON only.", cfg))
            if isinstance(data, dict) and SCHEMA.get("additionalProperties") is False:
                # The model sometimes adds harmless extra fields ("sources",
                # "suppressed"). Drop them rather than spend a retry on it.
                allowed = set(SCHEMA.get("properties", {}))
                data = {k: v for k, v in data.items() if k in allowed}
            errors = sorted(VALIDATOR.iter_errors(data), key=lambda e: e.path)
            if errors:
                last_err = "; ".join(f"{'/'.join(map(str, e.path))}: {e.message}"
                                     for e in errors[:4])
                log.warning("enrichment schema failure (attempt %d): %s", attempt + 1, last_err)
                continue

            # The model has final say — it read the whole article, the gates did not.
            unreadable = False
            if data.get("disqualify"):
                reason = data.get("disqualify_reason") or "disqualified by AI review"
                if _UNREADABLE.search(reason):
                    # The page did not load properly — that says nothing about the
                    # company. Send it through for the AE to check rather than drop it.
                    log.info("not rejecting %s — article could not be read (%s)",
                             sig.company_name, reason[:80])
                    unreadable = True
                    sig.raw = sig.raw or {}
                    sig.raw["ai_unreadable"] = True     # for the Run Details download check
                    data["disqualify"] = False
                    if not data.get("bottleneck"):
                        data["bottleneck"] = ("The article could not be read in full — "
                                              "check the source link before outreach.")
                elif _ROUNDUP_THIN.search(reason) and not _HARD_NO.search(reason):
                    log.info("not rejecting %s — only thin roundup coverage (%s)",
                             sig.company_name, reason[:80])
                    data["disqualify"] = False
                    if not data.get("bottleneck"):
                        data["bottleneck"] = ("Only a one-line mention in a funding "
                                              "roundup — check the source before outreach.")
                    # A disqualified reply leaves the write-up blank; fill the gaps so
                    # the email never shows an empty draft.
                    name = data.get("company_name") or sig.company_name
                    if not data.get("expansion_trigger"):
                        data["expansion_trigger"] = "Named as a recent raise in a funding roundup."
                    if not data.get("recommended_package"):
                        data["recommended_package"] = "Job Slots"
                    if not data.get("inmail_hook"):
                        data["inmail_hook"] = (f"Congratulations on the recent funding at {name}. "
                                               "How are you planning the next few hires as the "
                                               "team grows?")
                elif (_INDIA_REASON.search(reason)
                      and (data.get("india_presence") or "unknown") != "no"):
                    # Rejected only for India, and the AI is not SURE: the AE decides.
                    return None, f"india_unknown: {data.get('hq_location') or ''}"
                else:
                    log.info("AI veto: %s — %s", sig.company_name, reason)
                    return None, f"ai_veto: {reason}"

            # The model's own headcount figure is a GUESS and is never used to drop an
            # account — that is reserved for counted figures. It only informs the email.
            presence = (data.get("india_presence") or "unknown").lower()
            hq = (data.get("hq_location") or "").strip()
            if presence == "no" and not unreadable:
                return None, ("ai_veto: not India-based (confirmed)" +
                              (f" — based in {hq}" if hq else ""))

            # India must be SHOWN, not assumed. A profile of an Indian-origin investor
            # once let a San Jose and a Paris company through as "India".
            verdict = _india_verdict(data, sig, detail or {})
            if verdict == "foreign" and not unreadable:
                return None, f"india_unknown: {hq}"
            link = str((detail or {}).get("india_link") or "")
            if verdict == "india" and link and not (data.get("india_evidence") or "").strip():
                hq = data.get("hq_location") or "HQ abroad"
                data["india_city"] = f"Borderline — {hq}; India link: {link}"[:90]
            if verdict == "none" and not unreadable:
                if (sig.raw or {}).get("source_thin"):
                    # Article could not be read in full: say so, never guess.
                    data["india_city"] = "Location not confirmed — check"
                else:
                    return None, f"india_unknown: {hq}"

            return _to_account(data, sig, hc, is_follow_on), "ok"
        except Exception as exc:                                # noqa: BLE001
            last_err = str(exc)[:200]
            log.warning("enrichment call failed (attempt %d): %s", attempt + 1, last_err)
    log.error("enrichment permanently failed for %s: %s", sig.company_name, last_err)
    return None, f"enrichment failed: {last_err}"


_INDIA_REASON = re.compile(r"india", re.I)

_INDIA_PLACE = re.compile(
    r"\b(india|indian|bengaluru|bangalore|mumbai|delhi|ncr|gurugram|gurgaon|noida|"
    r"hyderabad|chennai|pune|kolkata|ahmedabad|jaipur|kochi|chandigarh|indore|"
    r"coimbatore|thiruvananthapuram|trivandrum|bhubaneswar|lucknow|nagpur|surat|"
    r"vadodara|visakhapatnam|mysuru|mysore|goa|mohali|kanpur|bhopal|patna|ranchi)\b",
    re.I)


def _india_verdict(d: dict[str, Any], sig: RawSignal, detail: dict[str, Any]) -> str:
    """'india' | 'foreign' (based abroad, no India link) | 'none' (nothing either way)."""
    evidence = (d.get("india_evidence") or "").strip()
    link = str(detail.get("india_link") or "")
    gate_city = str(detail.get("city") or "")
    ai_city = str(d.get("india_city") or "")
    hq = str(d.get("hq_location") or "")
    if evidence or link:
        return "india"
    if gate_city and gate_city not in ("Unknown",) and _INDIA_PLACE.search(gate_city):
        return "india"
    if _INDIA_PLACE.search(hq):
        return "india"
    if ai_city and ai_city.lower() != "india" and _INDIA_PLACE.search(ai_city):
        return "india"
    if hq:
        return "foreign"
    return "none"


def _to_account(d: dict[str, Any], sig: RawSignal, hc: HeadcountResult,
                is_follow_on: bool) -> QualifiedAccount:
    return QualifiedAccount(
        company_name=d["company_name"] or sig.company_name,
        website=d.get("website") or sig.website or "",
        linkedin_url=d.get("linkedin_url") or sig.linkedin_url or "",
        headcount=int(hc.count or 0),
        headcount_source=hc.source,
        india_city=d.get("india_city") or "India",
        industry_icp=d.get("industry_icp") or "Unclassified",
        funding_amount=d.get("funding_amount") or "Undisclosed",
        round_type=d.get("round_type") or "Undisclosed",
        decision_makers=d.get("decision_makers") or [],
        bottleneck=d["bottleneck"],
        expansion_trigger=d.get("expansion_trigger") or "",
        recommended_package=d["recommended_package"],
        positioning=d.get("positioning") or "",
        inmail_hook=d["inmail_hook"],
        source_url=sig.url,
        is_follow_on=is_follow_on,
    )
