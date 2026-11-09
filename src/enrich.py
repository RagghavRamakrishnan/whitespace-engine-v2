"""LLM enrichment: structured output, schema-validated, with a bounded retry.

Runs LAST in the pipeline — only on accounts that already cleared dedup, the hard
gates and headcount verification. That ordering is what keeps this cheap.
"""
from __future__ import annotations

import json
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
            errors = sorted(VALIDATOR.iter_errors(data), key=lambda e: e.path)
            if errors:
                last_err = "; ".join(f"{'/'.join(map(str, e.path))}: {e.message}"
                                     for e in errors[:4])
                log.warning("enrichment schema failure (attempt %d): %s", attempt + 1, last_err)
                continue

            # The model has final say — it read the whole article, the gates did not.
            if data.get("disqualify"):
                reason = data.get("disqualify_reason") or "disqualified by AI review"
                log.info("AI veto: %s — %s", sig.company_name, reason)
                return None, f"ai_veto: {reason}"

            # The model's own headcount figure is a GUESS and is never used to drop an
            # account — that is reserved for counted figures. It only informs the email.
            if data.get("is_india_based") is False:
                return None, "ai_veto: not India-based"

            return _to_account(data, sig, hc, is_follow_on), "ok"
        except Exception as exc:                                # noqa: BLE001
            last_err = str(exc)[:200]
            log.warning("enrichment call failed (attempt %d): %s", attempt + 1, last_err)
    log.error("enrichment permanently failed for %s: %s", sig.company_name, last_err)
    return None, f"enrichment failed: {last_err}"


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
