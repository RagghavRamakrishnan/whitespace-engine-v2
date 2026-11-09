# Prompt design notes

The live prompt text is in `src/prompts.py`; the output contract is
`schemas/enrichment.schema.json`. This file explains *why* they are shaped that way, so
you can tune them without breaking the pipeline.

## 1. The model does not qualify accounts

Qualification (capital event, India footprint, ICP vertical, 1–50 headcount) is deterministic
Python in `src/gates.py`. The model is given only accounts that already passed and is told so
explicitly. Two reasons:

- **Consistency.** A gate that "usually" fires produces an inbox you stop trusting in week two.
- **Cost.** Enrichment runs on ~5–15 records/day instead of ~500.

The one place the model *does* classify is the vertical, and only because the keyword gate is
deliberately permissive: an unmatched vertical is passed through as `unclassified` rather than
dropped, and the model resolves it into the `industry_icp` enum.

## 2. Null beats a guess

Every extraction field is nullable and the prompt forbids inventing names, titles, LinkedIn
profile URLs, websites and funding amounts. A constructed profile URL that 404s — or worse,
resolves to the wrong person — costs more credibility than a blank field. The schema enforces
`linkedin_url: ["string","null"]` on decision makers for exactly this reason.

## 3. The fallback logic is priors, not improvisation

When an article has no hiring quote, the system still alerts (per the spec). The prompt carries
explicit sector priors so the inferred bottleneck is consistent across runs rather than
re-invented each time:

| Sector | Prior bottleneck |
| --- | --- |
| DeepTech / robotics / semiconductor / space | Passive hardware, embedded, controls, RF — tiny pool, employed at ISRO/DRDO/Qualcomm-class orgs |
| AI / ML | Applied research + MLE against Big Tech comp; response rate is the constraint |
| Enterprise B2B SaaS | Full-stack/platform + US-shift SDR/AE; the GTM hire is hardest |
| MedTech / BioTech | Regulatory, clinical affairs, QA/RA specialists |
| ClimateTech / materials | Process, materials, power electronics — often non-metro |
| D2C / consumer tech | Performance marketing, supply chain, multi-city ops leads |

Edit the table in `SYSTEM_PROMPT` when your own call data says a prior is wrong. That table is
the highest-leverage thing in this repo — it drives both the bottleneck and the package choice.

## 4. Package mapping is a rule, not a vibe

- **Recruiter Corporate** → scarcity problem (passive, senior, specialist).
- **Job Slots** → volume/rotation problem (many roles, moderate scarcity, budget-sensitive).
- **Career / Life Pages** → employer-brand problem (unknown name competing with Big Tech).

The model may name a secondary package inside `positioning`, but `recommended_package` is a
single enum value so column M stays filterable.

## 5. "Headcount" means the LinkedIn member count

The model is asked for `estimated_headcount` defined as **associated members on
LinkedIn** — the people page number — not true headcount and not the self-reported
size band. Three consequences encoded in the prompt:

- Gig and frontline workers (riders, drivers, pickers, floor staff) are largely absent
  from LinkedIn and are excluded from the estimate.
- Salaried corporate, engineering, product and ops staff are present and counted.
- An operations-heavy business is sized from its operations footprint, not its round.

This also explains why the LinkedIn size band is never trusted to disqualify: it is a
self-reported dropdown, routinely stale, and a "51-200" company is often 30 people.

## 6. InMail hook constraints

Two sentences, <55 words, second person, ends on a question, no statistics, no feature pitch,
no "hope this finds you well". Sentence 1 anchors on the specific funding milestone; sentence 2
asks about the hiring constraint. The hook is a **draft** — the AE edits and sends it. Never
wire this system to send InMails automatically.

## 7. Failure handling

`enrich()` validates against the JSON schema and retries up to `llm.max_retries` times, feeding
the validation errors back into the prompt. A permanent failure sends the record to
`needs_review` rather than emitting a half-filled alert. If you see the same field failing
repeatedly, the schema and the prompt have drifted apart — fix the prompt, not the schema.

## 8. Tuning loop

1. Read the weekly `drops` table by stage. Wrong drops → fix `config.yaml` keyword lists.
2. Read the `needs_review` queue. Mostly headcount? Move off `estimate` to a licensed provider.
3. Track which hooks you actually sent unedited. Low edit rate is the only real quality metric —
   feed the ones you rewrote back into the prompt as counter-examples.
