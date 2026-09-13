# Input / Output Flow

End-to-end path from `dataset/` to `output.csv`, one request at a time. All eleven stages are
implemented in `code/buyorwait/`; the rule documents next to this file give the details.

Golden rule: *AI understands messy evidence, deterministic code calculates financial safety,
deterministic policy chooses the safest valid plan, and AI explains the resulting decision.*

```
dataset/*.csv, dataset/media/images/*.png
        │
        ▼
[1] Load + validate ......................................... implemented
        │
        ▼
[2] Per-request context builder ............................. implemented
        │
        ├──────────────► [3a] Structured facts (FX, windows) . implemented
        │
        └──────────────► [3b] EvidenceAgent (LLM #1) ......... implemented
                                │
        ┌───────────────────────┘
        ▼
[4] Event reconciliation .................................... implemented
        ▼
[5] Financial state ......................................... implemented
        ▼
[6] 90-day forecast → safe amount, earliest date ............ implemented
        ▼
[7] Plan generator .......................................... implemented
        ▼
[8] Plan evaluator .......................................... implemented
        ▼
[9] Policy engine → status, method, plan, changes ........... implemented
        ▼
[10] ExplanationAgent (LLM #2) .............................. implemented
        ▼
[11] Consistency validator → repair or safe fallback ........ implemented
        ▼
output.csv (repo root, input order) + code/evaluation/usage_report.md
```

Only stages 3b and 10 call a model. Everything else is plain Python and runs identically with
or without an API key (`--no-llm` mode uses the null extractor and the template explanation).

---

## Stage 1 — Load + validate

**What it does.** Reads the nine CSVs with the standard library, validates every row into a typed
Pydantic model, checks headers, unique ids, foreign keys, image files on disk, and a set of
semantic consistency rules. Bad rows are recorded and excluded; the load never crashes on one
bad line. Builds a `Dataset` with indexed lookups.

**Inputs.** `dataset/financial_profiles.csv`, `financial_events.csv`, `exchange_rates.csv`,
`requests.csv`, `sample_requests.csv`, `request_payment_options.csv`, `messages.csv`,
`images.csv`, `output.csv` (template), `media/images/*.png`.

**Output.** `Dataset` + `ValidationReport`. Current result on the real data: 0 errors, 0 warnings
(see [../VALIDATION.md](../VALIDATION.md)).

**Files.**
- `code/buyorwait/data/models.py` — one model per CSV, closed enums, `Decimal` money, blank → `None`
- `code/buyorwait/data/loader.py` — reading, per-row validation, cross-file checks, `Dataset`
- `code/buyorwait/config.py` — repo-relative paths, 90-day constant
- `code/tests/test_loader.py`, `code/tests/conftest.py`

---

## Stage 2 — Per-request context builder

**What it does.** For one request, joins by exact keys: the user's profile, the user's events up to
the end of the horizon, the request's seller options, the user's messages sent on or before the
request date, and the user's images. Picks, deterministically, the candidate events each message
or image may describe. Produces an immutable `RequestContext`.

**Filtering rules.** Events after `request_date + 90` days are dropped. Messages after the request
date are dropped. Candidates: the explicit `related_event_id` plus rows linked to it; otherwise
the user's events in the 90 days before the message up to the horizon end, capped at 40, always
including pending, scheduled, and blank-amount rows.

*Cap tested (2026-09-13):* raising `MAX_CANDIDATES` from 40 to unlimited grows the average
candidate list from 39.2 to 51.9 rows (max 72) and the evidence prompt by about 30%
(57,917 vs 52,659 input tokens on the 25 samples), and changes **no** output field. The extra
rows are the oldest settled routine history in the window, which no message describes; sources
that name a `related_event_id` (55 of 231) never use the window at all. Kept at 40.

**Files.**
- `code/buyorwait/data/context.py` — `build_context`, `RequestContext`, `EventView`, `EvidenceSource`
- `code/tests/test_context.py`

---

## Stage 3a — Structured facts

**What it does.** Converts every cash event to the user's home currency using the exchange-rate row
for the event's settlement date and stated direction. No inversion, no nearest-date fallback; a
missing rate is recorded on the context, not guessed. Computes the horizon (`request_date` to
`+90`) and the history window (`-180` days) used later for recurrence.

**Files.**
- `code/buyorwait/data/fx.py` — `lookup_rate`, `convert`, `event_home_amount`
- `code/buyorwait/data/context.py` — `EventView.home_amount`, `fx_rate`, `fx_missing`

---

## Stage 3b — EvidenceAgent (LLM call #1)

**What it does.** Runs once per message or image (requests with neither skip the model). The model
receives a trusted fact block (request, user, candidate events) and the evidence inside an
`<untrusted_evidence>` wrapper, and must return `StructuredEvidence`: a list of items with a fixed
`kind`, an optional target event id, amount, dates, confidence, and a verbatim quote. Code then
validates every item (id among candidates, currency matches, dates parse, `high` confidence) and
downgrades failures to `unresolved` with a reason. A failed model call yields empty evidence with
the error recorded; the request continues.

**What it must never do.** Calculate, decide, invent events, or follow instructions found in the
evidence.

**Files.**
- `code/buyorwait/agents/schemas.py` — `StructuredEvidence`, `EvidenceItem`, `ValidatedEvidence`
- `code/buyorwait/agents/prompts/evidence_system.md` — system prompt (`evidence-v1`)
- `code/buyorwait/agents/evidence_agent.py` — prompt assembly, validation, `NullEvidenceExtractor`, `PicoEvidenceExtractor`
- `code/buyorwait/config.py` — `make_openai_client`, env loading
- `code/tests/test_evidence.py`

---

## Stage 4 — Event reconciliation

**What it does.** Turns the raw event rows plus validated evidence into one trusted list. Classifies
each row by status into history / reserved / confirmed / excluded, applies each evidence item as a
concrete edit (exclude, replace amount or date, fill blank, change the salary stream) in the
spec's precedence order, and tags every row with its provenance.

**Rules.** [event_reconciliation_rules.md](event_reconciliation_rules.md).

**Files.** `code/buyorwait/finance/reconciliation.py`, `code/tests/test_engine.py`.

---

## Stage 5 — Financial state

**What it does.** Reads the reconciled list and produces exactly what the forecast needs: starting
balance and minimum, reserved debits with dates, confirmed and projected income with dates, the
recurrence model, and the catalogue of flexible expenses the user permits changing.

**Rules.** Recurrence detection is in [forecast_rules.md](forecast_rules.md).

**Files.** `code/buyorwait/finance/recurrence.py`, `code/buyorwait/finance/state.py`.

---

## Stage 6 — 90-day forecast

**What it does.** Walks day by day from the request date for 90 days, producing the balance path
`B(t)`. From it derives `headroom(d)`, `amount_safe_to_pay`, and `earliest_date_for_full_payment`,
both without spending changes.

**Rules.** [forecast_rules.md](forecast_rules.md).

**Files.** `code/buyorwait/finance/forecast.py` (path, headroom, `safe_amount`, `earliest_full_payment_date`).

---

## Stage 7 — Plan generator

**What it does.** Enumerates every candidate plan without judging it: full payment today, wait until
the earliest date, the two-payment partial plan, one plan per seller installment option, and
spending-change variants for plans that turn out unsafe.

**Rules.** [payment_plan_rules.md](payment_plan_rules.md).

**Files.** `code/buyorwait/finance/plans.py`, `code/buyorwait/finance/spending_changes.py`.

---

## Stage 8 — Plan evaluator

**What it does.** Runs each candidate through the gates in order — user preference, request and
limit gates, deadline, safety on the balance path — and tags it eligible or rejected with a reason.

**Rules.** [payment_plan_rules.md](payment_plan_rules.md).

**Files.** `code/buyorwait/finance/plans.py` (`evaluate_plan`).

---

## Stage 9 — Policy engine

**What it does.** Ranks the eligible plans with the six-key comparator from the spec and maps the
winner to `affordability_status`, `recommended_payment_method`, `payment_plan`, and
`spending_changes_needed`. No eligible plan → `not_affordable` / `not_recommended`.

**Rules.** [payment_plan_rules.md](payment_plan_rules.md).

**Files.** `code/buyorwait/policy/decision_policy.py` (`rank_key`, `decide`).

---

## Stage 10 — ExplanationAgent (LLM call #2)

**What it does.** Receives a fact sheet (final decision, safe amount, minimum balance, plan, changes,
earliest date, evidence used) and writes `decision_explanation` in the sample style. It cannot
change any decision field. If the text cites a number not in the fact sheet, or the call fails, a
deterministic template is used instead.

**Files.** `code/buyorwait/agents/explanation_agent.py` (template + `PicoExplainer`),
`code/buyorwait/agents/prompts/explanation_system.md`.

---

## Stage 11 — Consistency validator

**What it does.** Re-checks the final row against every spec constraint (bounds, enum pairs,
partial and installment rules, deadline, minimum balance, spending-change limits, explanation
grounding). Repairs derived fields if possible, otherwise falls back to the next-ranked plan,
otherwise writes the safe fallback row. Every repair is logged with the request id and reason.

**Files.** `code/buyorwait/policy/consistency.py`, `code/tests/test_engine.py`.

---

## Output

`output.csv` at the repository root with the eight required columns, one row per row of
`requests.csv`, in the same order regardless of concurrent execution. Amounts formatted as in the
samples (two decimals only when fractional). `code/evaluation/usage_report.md` generated from the
actual run's model calls, tokens, and cost.

**Files.** `code/main.py`, `code/buyorwait/pipeline.py`, `code/buyorwait/infra/csv_writer.py`,
`code/buyorwait/infra/usage.py`, `code/buyorwait/infra/cache.py`, `code/evaluation/main.py`.

---

## Batching and token efficiency

Two model calls per request was the original shape (one evidence call per message or image,
one explanation per request). Two changes reduced calls and tokens without touching any
decision logic:

- **Compact evidence facts** (`evidence-v3`): the candidate-event block is pipe-delimited rows
  instead of keyed JSON. Same fields, roughly half the input tokens.
- **Batched calls** (`code/buyorwait/pipeline.py`): message-only evidence sources are sent four
  per call (`StructuredEvidenceBatch`, one result per `source_id`); images always go alone.
  Explanations are sent eight fact sheets per call (`ExplanationBatch`, one text per
  `request_id`). Validation stays per item against that item's own candidates and fact sheet. A
  batch that fails, or omits an item, falls back to single calls for the affected items, so
  batching never changes which requests get an answer. Cache lookups happen per item before
  batching; only misses reach the model. Both sizes are CLI flags (`--evidence-batch`,
  `--explanation-batch`; 1 disables).

Measured on the 25 samples, no cache: 14 model calls instead of 47, 2,430 tokens per request
instead of about 5,000, identical decisions.

**Files.** `code/buyorwait/agents/schemas.py` (batch containers), `code/buyorwait/agents/evidence_agent.py`
(`facts_block`, `extract_batch`), `code/buyorwait/agents/explanation_agent.py` (`explain_batch`),
`code/buyorwait/pipeline.py` (phases, fallback), `code/tests/test_batching.py`.

---

## Usage report — how `code/evaluation/usage_report.md` is generated

The report is assembled from recorded model calls; no figure in it is estimated or assumed.

1. **Provider usage per call.** Every chat completion returns token counts. PicoAgents wraps
   them in a `Usage` object (input tokens, output tokens, call count, duration) on the agent
   response. `PicoEvidenceExtractor` and `PicoExplainer` copy those numbers into their results.
2. **One ledger line per call.** `pipeline.py` appends a `CallRecord` to a `UsageLedger` for
   every evidence extraction and every explanation: request id, agent, model, provider, tokens
   in/out, duration, cache hit or not, error text, retries, fallback. Requests with no message or
   image make no evidence call. Per-request stage notes (stage reached, repairs, fallback) are
   kept alongside.
3. **Ledger written to disk.** At the end of a run `code/main.py` saves
   `code/.runs/<timestamp>/calls.jsonl` (one JSON object per call) and `requests.json`. These are
   the raw evidence behind the report and are excluded from `code.zip`.
4. **Aggregation.** `build_usage_report` in `code/buyorwait/infra/usage.py` groups the
   non-cached, successful records by provider and model, sums calls and tokens, then computes
   overall totals, average calls per request, and average tokens per request. Cost is computed
   only when `OPENAI_PRICE_INPUT_PER_1M` and `OPENAI_PRICE_OUTPUT_PER_1M` are set; otherwise the
   cost cells read `n/a` and the report states that no price is configured. A reliability section
   lists cached outputs reused, failed calls after retries, retries performed, requests that used a
   fallback, and requests with validator repairs.
5. **Where it lands.** A full run writes `code/evaluation/usage_report.md` (the submission
   deliverable). A `--sample` run writes the report into its run folder instead, so development
   runs never overwrite the deliverable.
6. **Rebuilding without re-running.** `python code/evaluation/regenerate_usage_report.py
   code/.runs/<timestamp>` reads a saved `calls.jsonl` and rewrites the report, e.g. to add cost
   figures after prices become known.

**Files.** `code/buyorwait/infra/usage.py` (`CallRecord`, `UsageLedger`, `build_usage_report`),
`code/buyorwait/pipeline.py` (recording), `code/main.py` (writing), `code/evaluation/regenerate_usage_report.py`.
