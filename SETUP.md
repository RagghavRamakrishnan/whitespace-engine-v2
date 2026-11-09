# Setup — plain-English walkthrough

You need three things, none of which cost money and none of which need a credit card:

1. A **GitHub** account — where the engine lives and runs on a timer.
2. A **Gemini API key** — the AI that writes the analysis. Free tier.
3. A **Gmail app password** — the mailbox the alert is sent *from*.

Alerts arrive at your work inbox. Your work account is never connected to anything,
so no IT approval is needed anywhere in this setup.

---

## Step 1 — GitHub account
github.com → Sign up → use your personal Gmail.

## Step 2 — Create the repository
GitHub → **+** (top right) → **New repository**.
- Name: `whitespace-engine`
- **Private** (important — your config lives here)
- Tick **Add a README file**
- Create repository

## Step 3 — Upload the code
In the repo: **Add file → Upload files**. Drag in every file and folder from the
`whitespace-engine` folder. Commit changes.

> The `.github/workflows/whitespace.yml` file is what makes it run on a timer.
> If GitHub hides the `.github` folder during upload, use **Add file → Create new file**,
> type `.github/workflows/whitespace.yml` as the name, and paste the contents in.

## Step 4 — Get a Gemini API key (the AI)
1. Go to **aistudio.google.com** → sign in with your personal Gmail.
2. Click **Get API key** → **Create API key**.
3. Copy it immediately and keep it somewhere safe. It starts with `AQ.` — that is the
   current format. (Older guides mention `AIza` keys; those are being retired and are
   rejected from September 2026. If you have an old `AIza` key, generate a new one.)

`AQ.` keys work on Google's native endpoint, which is what the `google-genai` SDK used
here calls. They are rejected on OpenAI-compatible endpoints — so if you ever switch
`llm.provider` in config.yaml to `groq` or `openai`, do not reuse this key; those paths
need that provider's own.

Note: a Google AI Pro/Ultra *subscription* does not include API access — that plan
covers the Gemini app, not code. The free API key above is separate and is what you
need. Its free allowance is far above this engine's ~10–15 calls a day.

## Step 5 — Create a Gmail app password (the sender)
A normal Gmail password will not work here; Google requires a dedicated one.

1. **myaccount.google.com** → Security → turn on **2-Step Verification** if it is off.
2. Search that page for **App passwords**.
3. Create one named `whitespace-engine`.
4. Copy the 16-character password. Remove the spaces.

This lets the engine send mail from your personal Gmail to your work inbox.

## Step 6 — Store the secrets in GitHub
In your repo: **Settings → Secrets and variables → Actions → New repository secret**.

Add these, one at a time:

| Name | Value |
| --- | --- |
| `GOOGLE_API_KEY` | the `AQ....` key from step 4 |
| `GOOGLE_CSE_KEY` | recommended — reads LinkedIn's published size band, see below |
| `GOOGLE_CSE_CX` | recommended — goes with the above |
| `DIFFBOT_TOKEN` | optional — second headcount source |
| `PDL_API_KEY` | optional — third headcount source, used only as a last resort |
| `SMTP_USER` | your personal Gmail address |
| `SMTP_PASSWORD` | the 16-character app password from step 5 |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |

Secrets are write-only — nobody, including you, can read them back afterwards. That is
intentional. If you lose one, delete it and add it again.

## Step 7 — Test it manually before trusting the timer
Repo → **Actions** tab → **Whitespace Alert Engine** → **Run workflow**.

Wait about a minute, then open the run. A green tick means it worked. Check your work
inbox — if any Indian startup funding news in the last few hours passed all the gates,
an alert will be there. **No email is not a failure**: it usually means nothing qualified,
which is the filter doing its job. Click into the run log to see the counts.

## Step 8 — Let it run
It now runs every 15 minutes on its own. Nothing else to do.

---

## Where things are afterwards

- **Alerts** → your work inbox. Make an Outlook rule to file them into a folder.
- **Master tracker** → `master_tracker.csv` in the repo, updated automatically. Download
  and open it in Excel any time.
- **Run history** → the Actions tab shows every run and what it filtered out.

## Two things to know

- **The 60-day rule.** GitHub pauses schedules on repos with no activity for 60 days. The
  engine commits the tracker on every alert, which resets that clock, so it only matters
  during a long dry spell. If alerts stop, check the Actions tab for a "re-enable" button.
- **Headcount is currently estimated**, not verified — see the README. Sanity-check the
  number on LinkedIn before you spend real outreach effort on an account.

## If something breaks

Actions tab → click the failed (red) run → click the failed step. The last few lines say
what went wrong. Most common causes:

| Symptom | Cause |
| --- | --- |
| `SMTP_USER / SMTP_PASSWORD not set` | A secret is missing or misspelled in step 6 |
| `smtp send failed ... authentication` | Used the normal Gmail password, not the app password |
| `GOOGLE_API_KEY` error | Key not saved, or copied with a trailing space |
| `401 invalid_api_key` | Usually means the request went via an OpenAI-compatible path. Check `llm.provider` is `google` in config.yaml — an `AQ.` key only works on the native endpoint |
| `429` / quota error | Free-tier daily limit hit — rare at this volume; it resumes next day |
| Runs are green but no email ever arrives | Nothing is passing the gates — loosen the keyword lists in `config.yaml` |


---

## Optional: confirmed headcount via Diffbot

Without this, every headcount in the digest is an estimate and lands in the
"not confirmed" section for you to check by hand. That is safe — nothing is ever
dropped on a guess — but it is more clicking.

Diffbot has the strongest free tier available: ~10,000 credits/month with full API
access, rate-limited to 5 calls/minute. This engine needs roughly 300/month.

1. Sign up at **diffbot.com** and copy your API token.
2. Add it as a GitHub secret named `DIFFBOT_TOKEN` (same place as the others).
3. In `config.yaml`, `verification.provider` is already set to `diffbot`.

Coverage of very small Indian startups is unproven. A miss is handled gracefully:
the engine falls back to the article text, then to a stage estimate, and the account
still reaches you — flagged as unconfirmed rather than dropped.

### Recommended: Google Custom Search (primary)

This reads the size band LinkedIn itself publishes — "11-50 employees" — out of
**Google's** index, using Google's official Custom Search API. It is not a scrape of
LinkedIn and circumvents nobody's terms, which matters given where you work. The band
maps straight onto the 1-50 gate, so no exact number is needed.

1. **console.cloud.google.com** → create a project → APIs & Services → Library →
   enable **Custom Search API** → Credentials → Create credentials → API key.
   Save it as the `GOOGLE_CSE_KEY` secret.
2. **programmablesearchengine.google.com** → Add → name it, and set it to
   **Search the entire web** → Create. Copy the **Search engine ID**.
   Save it as the `GOOGLE_CSE_CX` secret.

100 queries/day free (~3,000/month), comfortably above this engine's volume.

Note on parsing: LinkedIn snippets carry follower counts as well as employee counts,
and they differ by 10-100x. The parser strips follower text before reading any number
and only accepts an explicit employee phrasing, so a follower count can never be
mistaken for headcount.

### Important: the band is not the member count

A LinkedIn company page carries **two different numbers**:

| | What it is | Where |
| --- | --- | --- |
| **Size band** | Self-reported. An admin picks it from a dropdown, often once, often aspirationally. | About section — this is what Google indexes |
| **Associated members** | Actual profiles listing that employer. | The people page — this is the number that matters |

A 30-person company showing "51-200" is routine, and the associated-members count is
not reliably indexed by Google. So the band is used **asymmetrically**:

- Band entirely within range ("11-50", "2-10") → **confirmed**, green section.
- Band starting at or above `band_disqualify_floor` (201) → **dropped**. No amount of
  optimistic self-reporting explains "201-500" for a 40-person company.
- Anything between ("51-200") → **shown, flagged**. Never dropped. It may genuinely be
  30 people with a stale dropdown, and dropping it would silently lose exactly the
  accounts this tool exists to find.

The plausibility ceiling is deliberately not applied to that middle band either: "200"
is the top of a dropdown option, not a measurement, and treating it as one would
re-introduce the same failure.

### A note on data vendors

Vendors that resell scraped LinkedIn data (Bright Data and similar) are deliberately
not used here. The general legality of public-web scraping is genuinely unsettled, but
that is beside the point: as a LinkedIn employee, buying scraped LinkedIn data to do
LinkedIn sales work is a professional risk regardless of how the case law lands.

Apollo is also avoided: its free tier requires registration with a **work** email, and
that would mean signing up for a Sales Navigator competitor under a linkedin.com
identity.

People Data Labs is largely LinkedIn-derived and sits in a grey area. It is enabled
here only as a last-resort step, for accounts no other source could resolve. Still
worth a quick word with your compliance team, since it is a judgment call rather than
a clear line.

### The two providers do different jobs

Their free tiers differ by about 100x, so they are not interchangeable:

| Step | Free tier | Runs when |
| --- | --- | --- |
| **Google Custom Search** | ~3,000/month | Always. Returns LinkedIn's own published size band. |
| **Diffbot** | ~10,000/month | Google found nothing, returned an unusable band like "51-200", or a number near the 50 line. |
| **People Data Labs** | ~100/month | Neither of the above produced a usable answer. Capped at 90/month. |

A counted figure always beats a self-reported band, so Diffbot or PDL will override a
"51-200" when they actually know. A conclusive band ("11-50", "201-500") stops the
chain immediately — no further lookups are spent.

Real traffic is roughly 300-600 lookups a month, so both tiers hold comfortably. The
tiebreaker is only called when it would change the answer — a miss, or a result in the
40-70 range — and a monthly cap (`tiebreaker_monthly_cap`) stops it running away.

Both are optional. With neither, every account arrives flagged for a manual check —
which is the design, not a degraded mode. To run that way, set
`verification.primary: estimate` in `config.yaml`.

### What "headcount" means here

The target number is **the count on the company's LinkedIn people page** — not true
headcount. Those differ, and only the LinkedIn one matters for this job.

This shapes the whole design. Providers are ordered by how closely their number tracks
LinkedIn, not by general data quality: People Data Labs is largely LinkedIn-derived
(usually listed as a criticism of PDL — here it is the point), so it runs first;
Diffbot is web-crawl derived, so it is broader coverage but a looser match, and runs
second.

It also splits two decisions that look like one:

- A company's own "we are a 300-strong team" is reliable enough to **exclude** it —
  nobody inflates their size in a funding release.
- That same statement at "18-member team" is NOT enough to mark it **confirmed**,
  because it is a claim about real headcount, not about LinkedIn, and under-counting is
  the direction that costs a wasted InMail.

So press statements can disqualify but never confirm.

### How a Swish-sized mistake is prevented

A 367-person company reaching your inbox is a failure even if it is clearly labelled.
Four independent things have to fail before that happens now:

1. **Diffbot**, then **PDL** — a confirmed count above 50 drops the account outright.
2. **Cumulative capital** — set deliberately high ($150M) because total raised is a
   weak proxy in this ICP: a DeepTech or AI company can raise $30-75M with 30 engineers.
   It only catches the obviously-scaled (`gates.headcount.max_total_raised_usd`).
3. **Plausibility ceiling** — an unconfirmed account estimated above 80 is dropped
   rather than shown for you to reject (`gates.headcount.unconfirmed_ceiling`).
4. **The AI**, which is told explicitly that quick-commerce, delivery, logistics and
   other operations-heavy businesses run to hundreds of frontline staff on a modest
   round, and must be sized from their operations footprint rather than their funding.

The useful asymmetry: companies these data sources have never heard of are
overwhelmingly the *small* ones — which are the ones you want.

## The "all quiet" email

After 6 hours with no alert, you get a short note confirming the engine is alive,
with a summary of what it read and filtered. Change `dispatch.heartbeat.quiet_hours`
in `config.yaml`, or set `enabled: false` to turn it off.

If that email ever shows **stories read: 0**, the news sources are unreachable and
something is genuinely wrong. Any other number means the filters just found nothing.

---

## Why MCA and X are switched off

**MCA filings** — the data is public but there is no free or open bulk API. The adapter
is written and reads a JSON file at `MCA_FEED_PATH`; if you ever get filings access
(licensed vendor, or an authorised export) drop the file there and set `enabled: true`.
Worth having: PAS-3 allotments surface a raise 1-3 weeks before the press release.

**X / Twitter** — X discontinued free API access in February 2026. It now bills **per
post returned**, not per call, at $0.005 each. A search returning 20 posts costs $0.10:

| Polling frequency | Rough monthly cost |
| --- | --- |
| Every 30 minutes | ~$144 |
| Every 2 hours | ~$36 |
| Twice a day | ~$6 |

To enable: add an `X_BEARER_TOKEN` secret, set `enabled: true` on the `x_social` source,
and pick the interval deliberately — cost scales directly with it.

**Google News RSS covers most of this for nothing.** Free, keyless, unlimited, indexed
within minutes. When a founder announces on X or LinkedIn and Indian tech media picks it
up, this catches it. You lose the first few minutes against the X firehose; the
whitespace race is measured in hours. Five queries ship by default — add more freely,
they cost nothing. It also catches stealth exits, which are prime 1-50 territory.

---

## How roundup articles are handled

Most small seed rounds never get their own article. They appear as one line inside a
weekly roundup — "Indian startups raised $142M across 21 deals this week" — and that is
where a lot of the whitespace actually is.

So an article is not treated as one signal about one company. Every sentence that says
a named company raised money becomes **its own signal**, with its own company name,
amount and round type. One Entrackr weekly can produce eight or more prospects.

The rule that makes this safe: **the company and the funding verb must come from the
same sentence, and the company is taken from that sentence.** That is exactly what the
Microsoft false alert violated — a funding verb from a "related stories" sidebar was
attached to the company named in the headline. Under this rule the same sidebar now
produces a signal for the company it actually names, which deduplication then collapses
against that company's own article.

Sentences supply the deal; the article headline supplies context (geography, sector)
for signals whose own sentence omits it. Context can say who a company is. It can never
manufacture a funding event.

Investors are excluded automatically, so "Blume Ventures raised Rs 500 crore for its
maiden fund" inside a roundup does not become a prospect.

The roundup article itself is still blocked as a listicle — only its extracted
companies proceed.

---

## Recall: making sure nothing is missed

This is the requirement the whole design turns on. Ten other AEs read the same sources
by hand; being first only matters if the account was found at all.

**An LLM reads every article in full.** Not pattern-matching — reading, the same job
done manually. It lists every company the article says received capital, in whatever
phrasing: "announced a round", "has closed its Series A", "picked up an undisclosed
sum", "came out of stealth with backing from", bullet tables, or a company named one
sentence before its amount. The extraction prompt is explicit that missing a company
is the only unacceptable error, and that borderline mentions should be INCLUDED.

**Two extractors run, and the union is used.** The regex net is kept alongside the
model. A company disappears only if both missed it.

**The pre-filter is deliberately permissive.** It only skips articles with no financial
content whatsoever. Its one job is to save quota, never to make judgments — anything
that could conceivably be about money is read.

**Misses are made visible.** When an article looks like a funding story but yields no
company, it is counted and queued for review, and the count appears at the foot of
every digest and in the all-quiet email:

```
Read 31 articles, found 14 companies with funding events.
2 article(s) looked like funding stories but yielded no company — queued for review.
```

Watch that second line. Persistent non-zero values mean extraction is degrading, which
is the failure that silently costs accounts. `articles read` high with
`companies extracted` at zero means extraction is broken outright.

**Quota.** Articles are batched (4 per call), so roughly 30 new articles a day costs
about 8 extraction calls plus around 15 enrichment calls — comfortably inside the
Gemini free tier. Raise `run.extraction_batch_size` to use fewer calls, lower it if
long roundups appear truncated.

Setting `run.llm_extraction: false` falls back to regex alone. It will miss companies.

---

## Verifying it works — without reading ten sources yourself

The hardest failure to notice is the silent one: a feed moves, a page changes markup,
and a whole category of companies stops arriving. You cannot detect an article you
never saw. Four things exist specifically for this.

### 1. `--check-sources`

```
python -m src.run --check-sources
```

Polls every source once and prints what each returned. Run it after any config change,
and whenever alerts go unusually quiet. An `EMPTY` line is silent data loss.

### 2. Dead-source alerts in every email

Any source returning nothing for 20 consecutive runs (about five hours) is named at the
foot of the digest and in the all-quiet email:

```
SOURCES RETURNING NOTHING:
  - entrackr: 24 runs empty (last worked: 2026-09-12)
```

### 3. Failed reads are retried, not lost

An article is marked done only after it has actually been read. If the AI hits its
daily limit, or the call times out, those articles stay queued and are re-read on the
next run. Quota exhaustion stops the reading loop cleanly instead of recording
"no companies found" for everything remaining. After four failed attempts an article is
given up on and surfaced in the review queue rather than disappearing.

### 4. Every run reports its own recall

```
Read 31 articles, found 14 companies with funding events.

NEEDS ATTENTION:
  - 2 article(s) looked like funding stories but yielded no company
  - 3 article(s) would not download — only the feed summary was available
  - 8 article(s) deferred: the AI hit its daily limit
```

**The line to watch is "would not download".** Some sites block automated fetches. When
that happens only the feed summary is available, and a weekly roundup reduced to its
summary has no companies left to find. Persistent counts there mean a source needs a
different fetch approach, not that the week was quiet.

### Sources currently OFF, and why

| Source | Why |
| --- | --- |
| iDEX, BIRAC, TDB, YC India, Antler | Their CSS selectors in `config.yaml` are **unverified guesses**. A wrong selector returns zero rows forever. Fix with `--check-sources`, then enable. |
| MCA filings | No free or open bulk API. Adapter ready; needs a data source. |
| X / Twitter | Paid since Feb 2026, billed per post returned. |

Eight RSS/Google News sources are on. Those URLs are also unverified — `--check-sources`
is the first thing to run after setup.

---

## Limits: what is capped, and when you would hit it

Five things are rate-limited. Only one of them is likely to bite.

| What | Free allowance | Your usage | Headroom |
| --- | --- | --- | --- |
| **GitHub Actions minutes** | 2,000 min/month (private repo) | ~1,200 min/month | **1.7x — the binding constraint** |
| Gemini (AI) — per day | varies by model; tens to hundreds | ~30-60 calls/day | comfortable |
| Gemini (AI) — per minute | ~10-15 on Flash models | spaced automatically | handled in code |
| Google Custom Search | 100 queries/day | ~15-25/day | 4-6x |
| Diffbot | 10,000/month, 5/min | a few hundred/month | large |
| People Data Labs | ~100/month | capped at 90 | at the limit by design |
| Gmail sending | ~500/day | 1-5/day | irrelevant |

### The one that actually matters: GitHub Actions minutes

A private repo gets 2,000 free minutes a month, and **every run is billed rounded up to
the next whole minute**. Polling every 15 minutes would cost roughly 5,760 minutes —
you would run out around day 10 and jobs would stop silently (the default spending
limit is $0, so there is no bill and no alert).

The schedule is therefore budgeted, not arbitrary:

- **Every 30 minutes, 07:30–23:00 IST** — the window when Indian funding news publishes
- **Hourly overnight** — so a 6am Bengaluru announcement is still caught early

That is ~40 runs/day, ~1,200 minutes/month, about 60% of the allowance. The remaining
40% absorbs busy days, where a roundup pushes a run to 2 billable minutes.

Two things keep runs under a minute: article bodies are fetched **in parallel** (this
alone halves run time), and LLM calls are capped at 10 per run with the remainder
deferred to the next run rather than dropped.

**If you want 15-minute polling**, the options are: make the repo public (unlimited
minutes — but the tracker CSV with your prospect list would be public too, so do not
do this without moving it out), or pay roughly $4/month for extra minutes.

**Check your usage** at GitHub → Settings → Billing. GitHub emails you at 75% and 90%.

### Why the AI limits will not bite

Calls are spaced automatically to respect the per-minute cap, batched 6 articles at a
time, and capped per run. A typical run makes 3 calls; a heavy roundup day makes 8.
If the logs ever show `429` or `quota`, lower `llm.max_requests_per_minute` to 5 —
and note that quota exhaustion **defers** articles for retry rather than losing them.

---

## Running on a PUBLIC repo (current setup — supersedes the minutes budget above)

Public repos get unlimited free Actions minutes, so the engine runs every 15 minutes.

What changed to make public safe:

- **Your email address** is no longer in `config.yaml`. Add it as a secret named
  `ALERT_TO`.
- **The memory database** (what has been read and alerted) lives in GitHub's private
  Actions cache, not in the repo. Committing it would publish every company you have
  been alerted on.
- **The tracker CSV is off.** The digest email is the record.
- **Secrets stay secret.** Repository secrets are never visible, public repo or not.
- **Keep-alive.** GitHub disables schedules on public repos after 60 days without a
  commit; the workflow makes one tiny timestamp commit a day to prevent that.

Make it public: repo → Settings → scroll to Danger Zone → Change visibility → Public.
Do this only AFTER pushing the new code — the old code commits the tracker and
database into the repo.
