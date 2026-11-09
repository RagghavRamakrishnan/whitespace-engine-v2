# LTS Whitespace Alert Engine

Automated capital-inflow → ICP-gate → enrichment → alert pipeline for LinkedIn Talent
Solutions SMB whitespace prospecting (India, 1–50 headcount).

Signal in → hard gates → dedup → headcount verification → LLM enrichment → Outlook email +
master spreadsheet row.

---

## 0. Read this first: the headcount-verification constraint

The spec asks for a "programmatic LinkedIn scrape" of `/company/<slug>/people/`. **Do not build
that.** Scraping LinkedIn is prohibited by the LinkedIn User Agreement, is actively blocked
(auth walls, rate limits, IP bans), and for an employee of the company it is a policy problem
before it is an engineering problem. Scrapers of that page also break every few weeks, which is
the worst property for a gate that every alert depends on.

This implementation keeps the *gate* exactly as specified (strict 1–50, hard fail) but makes the
*source* of the number a pluggable provider behind one interface (`src/verify.py`):

| Provider | How it works | When to use |
| --- | --- | --- |
| `estimate` | Round type + sector + funding size → banded estimate, confidence-scored | Default cold-start; free |
| `manual` | Queues the record for a 20-second human check, resumes on answer | Highest accuracy, low volume |
| `partner_api` | Any licensed firmographic API you already pay for (ZoomInfo, Apollo, Crunchbase, Clearbit, Tracxn) | Recommended for scale |
| `internal` | LinkedIn's own sanctioned internal/first-party data surface, if your role has access | Best if approved |

Everything downstream is source-agnostic. Swap providers in `config.yaml`, no code change.

Records whose headcount cannot be verified are **not** dropped silently — they land in the
`needs_review` queue with the reason, so no signal is lost.

---

## 1. Recommended stack

**Recommendation: Python on a scheduled serverless runner + Postgres (Supabase) + one LLM API +
Microsoft Graph.** Not n8n.

| Layer | Choice | Why |
| --- | --- | --- |
| Compute | Python 3.11, one container, `run.py` invoked on a 15-min schedule (Azure Container Apps Job, AWS Lambda + EventBridge, or Cloud Run Job) | Ingestion is a cron-shaped batch, not an event mesh. One process = one place to debug. |
| State/DB | Supabase Postgres (SQLite for local dev — both supported) | Dedup needs a real unique index; 180-day cool-off is a query, not a spreadsheet scan. |
| LLM | One structured-output model (Claude / GPT / Gemini) with strict JSON schema enforcement | Enrichment is the only step that needs reasoning. Everything else is deterministic code — keep it that way. |
| Email | Microsoft Graph `sendMail` (app registration, `Mail.Send`) | Native Outlook, no SMTP passwords, works with your tenant. |
| Sheet | Microsoft Graph Excel API against a workbook in OneDrive | Live, and the AE can filter/edit it in the Excel they already use. Google Sheets adapter included as a fallback. |
| Secrets | Azure Key Vault / AWS Secrets Manager / `.env` locally | — |
| Observability | Structured JSON logs + a daily digest email of `dropped` and `needs_review` counts | You need to see *what was filtered out*, or you will never trust the gates. |

**Why not n8n:** the fragile parts here are HTML parsing, entity resolution, and dedup semantics.
Those are code. A visual workflow adds a hosting dependency and makes the one thing you will
iterate on daily (prompts + gates) harder to version and diff. Use n8n only if you want zero
infrastructure and accept manual node editing.

**Cost at ~500 signals/day:** LLM enrichment runs *after* the gates, so only ~5–15 records/day
reach it. Expect **< $15/month** total (LLM ~$3, Supabase free tier, serverless ~$2).

---

## 2. Pipeline

```
run.py
 ├─ 1. ingest.py      poll RSS/HTML/grant/accelerator sources → articles
 ├─ 2. extract.py     an LLM READS each article and lists every company that raised
 │                    (regex net runs alongside; the union is used)
 ├─ 3. db.py          normalize domain + LI slug + name → dedup, 180-day cool-off
 ├─ 4. gates.py       hard gates: operating company, capital event, India, ICP
 ├─ 5. verify.py      headcount chain → strict 1–50 gate (or surfaced for a check)
 ├─ 6. enrich.py      LLM → decision makers, bottleneck, package, InMail hook + VETO
 └─ 7. dispatch.py    one digest email + tracker row
```

**Step 2 is the one that decides whether anything is missed.** Competing AEs read these
sources by hand; so does this. Pattern-matching for "Company + verb + amount" cannot
anticipate "announced a round", "has closed its Series A", "picked up an undisclosed
sum", bullet tables, or a company named a sentence before its amount — and every such
miss is silent. An LLM reads the article instead. The regex net is kept as a safety
net and the UNION is taken, so a company is lost only if both extractors missed it.

Order still matters: **dedup before verification, verification before enrichment.**
Extraction is batched (several articles per call) to keep that step inside a free tier.

---

## 3. Repository layout

```
whitespace-engine/
├── README.md                  this file — architecture + runbook
├── requirements.txt
├── config.yaml                sources, gates, provider selection, dispatch targets
├── .env.example
├── src/
│   ├── models.py              RawSignal / QualifiedAccount dataclasses
│   ├── ingest.py              RSS + HTML + grant/accelerator adapters
│   ├── db.py                  SQLite/Postgres store, dedup, cool-off, state
│   ├── gates.py               deterministic hard gates (India / ICP / capital)
│   ├── verify.py              pluggable headcount verification
│   ├── enrich.py              LLM call + schema validation + retry
│   ├── prompts.py             the exact system/user prompts
│   ├── dispatch.py            Graph sendMail + Excel row append
│   └── run.py                 orchestrator entrypoint
├── schemas/enrichment.schema.json
├── docs/PROMPTS.md            prompt design notes + failure modes
└── tests/test_gates.py        gate regression tests (run these before every deploy)
```

---

## 4. Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in Graph + LLM credentials
python -m src.run --once --dry-run     # no email, no sheet write, prints what would fire
python -m src.run --once               # live
pytest tests/                          # gate regressions
```

Schedule: every 15 minutes for media/social, hourly for MCA, daily 07:00 IST for grants and
accelerator directories (`config.yaml → sources[].interval_minutes`).

### Microsoft Graph app registration

1. Azure Portal → App registrations → New. Note **client ID** and **tenant ID**.
2. Certificates & secrets → new client secret → `GRAPH_CLIENT_SECRET`.
3. API permissions → Application → `Mail.Send`, `Files.ReadWrite.All` → grant admin consent.
4. Put the master workbook in OneDrive, copy its item ID into `config.yaml → dispatch.excel`.

---

## 5. Deduplication rules (as implemented)

- Keys: `normalized_domain` (strip scheme/`www.`/path, lowercase) **and** `linkedin_company_slug`.
  Either match is a hit — companies rebrand domains more often than they rebrand LI slugs.
- A hit inside **180 days** → dropped, logged with the prior alert date.
- A hit **after** 180 days → alerted and tagged `[Follow-on Round]` in subject and column I.
- Alert insert and dispatch are wrapped so a crash mid-dispatch cannot double-send: the row is
  marked `dispatched` only after both outputs succeed, and re-runs skip `dispatched` rows.

---

## 6. Operational guardrails

- **Every gate decision is logged with a reason string.** Weekly, read the drop log. If the ICP
  gate is dropping companies you would have wanted, the vertical keyword lists in `config.yaml`
  are wrong — fix data, not code.
- **The LLM never decides qualification.** Gates are deterministic Python. The model only writes
  the enrichment (bottleneck, package, hook). This keeps false positives out of your inbox.
- **The hook is a draft.** Never auto-send InMails from this system; the AE edits and sends.
- **MCA / iDEX / BIRAC scraping:** respect `robots.txt` and rate limits, cache aggressively, and
  prefer official downloads or the data.gov.in feeds where they exist.
