# ARCHITECTURE.md — Buy or Wait? (HackerRank Orchestrate, September 2026)

Phase 0 deliverable. This document records what was inspected, what was found, and the
proposed architecture. Nothing in `code/` has been implemented yet.

Golden rule (from CLAUDE.md): **AI understands messy evidence, deterministic code calculates
financial safety, deterministic policy chooses the safest valid plan, and AI explains the
resulting decision.**

---

## 1. Current Repository Assessment

### 1.1 What exists

| Path | State |
|---|---|
| `code/main.py` | Empty file (0 bytes). Entry point per README: `python3 code/main.py`. |
| `code/evaluation/main.py` | Empty file. |
| `code/evaluation/usage_report.md` | Empty file. Must be generated from the final run. |
| `requirements.txt` | Lists `pandas`, `numpy`, `picoagents`, `picoagents[rag]`, `python-dotenv`, `ipykernel`, `dotenv`, `fastapi`, `uvicorn`. Nothing is installed in the active interpreter. |
| `.env` | Present, untracked. Keys: `OPENAI_API_KEY`, `OPENAI_MODEL` (values redacted). |
| `.env.example` | `OPENAI_API_KEY`, `OPENAI_MODEL="gpt-5.6"`, `OPENAI_PROJECT`, `OPENAI_ORGANIZATION`. |
| `.gitignore` | Was corrupted (the `.env` line contained UTF-16 null bytes). Rewritten cleanly this session; now ignores `.env`, `log.txt`, `*.csv`, `*.pdf`, venvs, caches. |
| `log.txt` | Created this session per AGENTS.md; gitignored. |
| `README.md` | States the final `output.csv` must be written to the **repository root**, not `dataset/`. |
| `dataset/` | Complete: 9 CSVs plus 16 PNGs in `media/images/`. |

### 1.2 Environment findings

- Python 3.12.10 (Windows 11). `pip show picoagents` → not found. `import pandas` → fails.
  **`requirements.txt` has never been installed into this interpreter.**
- Already installed and usable: `openai 2.45.0`, `anthropic 0.116.0`, `pydantic 2.13.4`,
  `pillow 12.3.0`, `python-dotenv 1.2.2`, `httpx 0.28.1`, `numpy 1.26.4`.
- `picoagents 0.5.0` (latest on PyPI, matches the GitHub source) **pins `openai==1.107.1`**.
  Installing it into the current interpreter will downgrade the installed `openai 2.45.0`.
  Recommendation: create a dedicated `.venv` for this project so the pin does not disturb
  other work.
- `picoagents[rag]` pulls `chromadb` and `sentence-transformers` (large, slow to install).
  Nothing in this challenge needs vector retrieval; all retrieval is exact-key joins.
  Recommendation: drop `[rag]`, `ipykernel`, `fastapi`, `uvicorn` from the runtime
  requirements and keep them, if at all, in a separate dev extras file.

### 1.3 Time budget

Session started 2026-09-12 13:10 UTC. Deadline is 2026-09-13 18:00 IST (12:30 UTC).
Roughly 22 hours remained when Phase 0 completed. Phases 1–7 must fit inside that.

---

## 2. Data Model

### 2.1 Files and cardinality (measured)

| File | Rows | Key | Notes |
|---|---|---|---|
| `financial_profiles.csv` | 275 | `user_id` | Currencies: INR 67, EUR 62, IDR 55, ZAR 51, USD 40. `max_installment_months` blank for 119 users. |
| `financial_events.csv` | 25,342 | `event_id` | 56–129 events per user, median 98. Dates 2019-03-09 → 2026-09-03. |
| `exchange_rates.csv` | 134 | (`rate_date`,`from`,`to`) | Pairs: USD→INR, USD→IDR, USD→EUR, EUR→USD, EUR→ZAR. Dates 2023-10-15 → 2026-11-15, roughly monthly on the 15th. |
| `requests.csv` | 250 | `request_id` | One request per user, no user repeats. 9 request types, ~28 each. `allows_partial_payment` true for 80, false for 170. Request dates 2023-01-20 → 2026-09-04. |
| `sample_requests.csv` | 25 | `request_id` | Same input columns plus the 7 solved output columns. Users `user_01`–`user_25`. |
| `request_payment_options.csv` | 790 | `payment_option_id` | 2–4 per request (3 is most common). 275 `full_payment` rows (fee 0) and 515 `installments` rows (all with fee > 0). Frequencies 28, 30, 31 days. Payment counts 1, 2, 3, 4, 6, 15, 18, 21, 24. |
| `messages.csv` | 215 | `message_id` | 128 tied to a request, 39 tied to an event, 76 user-level only. At most 1 message per request. Sources: employer 126, service_provider 31, financial_service 23, bank 18, merchant 17. Mixed English and Bahasa Indonesia. |
| `images.csv` | 16 | `image_id` | Every row has `user_id`, `request_id`, and `related_event_id`. All 16 PNGs exist on disk. |
| `output.csv` | 250 | `request_id` | Blank template in the same order as `requests.csv`. |

### 2.2 `financial_events.csv` semantics (measured)

Columns: `event_id, user_id, event_type, description, category, direction, amount, currency,
event_date, settlement_date, status, linked_event_id, flexibility, minimum_allowed_amount`.

Observed `event_type × direction × status` combinations:

| event_type | direction | statuses seen | Cash treatment |
|---|---|---|---|
| `expense` | debit | settled 20,410 · pending 63 · scheduled 16 · cancelled 22 · failed 14 | settled = history; pending/scheduled = reserve; cancelled/failed = ignore |
| `subscription` | debit | settled 2,488 | recurring history; the only flexible rows live here and in `expense` |
| `debt_payment` | debit | settled 553 · scheduled 7 · failed 7 | scheduled = reserve; failed = ignore |
| `income` | credit | settled 1,649 · scheduled 47 | scheduled = "Next confirmed salary" rows, count on settlement date |
| `refund` | credit | settled 14 · pending 8 | pending credit = do not count |
| `investment_purchase` | debit | settled 29 | historical cash out, not recurring |
| `investment_sale` | credit | settled 5 | historical cash in |
| `investment_valuation` | non_cash | unrealized 10 | never cash |

Other facts:

- `flexibility` ∈ {`fixed`, `reducible`, `stoppable`, `reducible_or_stoppable`}. Flexible rows are
  only `expense` (2,505 reducible) and `subscription` (177 reducible, 1,297 stoppable, 225 both).
- `minimum_allowed_amount` is populated on 2,907 rows and is the floor for `reduce_to`.
- `linked_event_id` populated on 58 rows. Observed lifecycles: refund → original expense;
  re-attempted expense → cancelled expense; valuation → investment purchase.
- **16 events have a blank `amount`; all 16 have a matching image.** 15 are expenses (groceries,
  utilities, rent, transport, healthcare, housing, shopping, dining), 1 is a settled salary.
  Statuses: settled, pending, scheduled. Currencies: 14 INR, 1 IDR, 1 USD.
- 140 events are in a currency other than the user's home currency (needs `exchange_rates`).
- 178 events have `settlement_date ≠ event_date`; 10 have blank `settlement_date`.
- No exact duplicate rows exist (same user, description, amount, date, direction).
  "Duplicate" in the challenge text therefore means semantic duplicates surfaced by messages
  (e.g. the bank message about "matching debit and credit from a transfer between your two
  accounts").
- 22 categories including `windfall` (prize proceeds), `salary`, `investment`.
- Income descriptions: "Payroll credit", "Base salary", "Primary household salary",
  "International employer payroll", "Delivery platform payout", "Driver platform payout",
  "Weekly app earnings", "Next confirmed salary".

### 2.3 Message content patterns (measured)

Keyword scan across 215 messages: confirmed 67, updated 24, dikonfirmasi 16, increase 16,
refund 12, paid 11, changed 11, bonus 8, commission 7, new amount 6, naik 4, tunda 4, failed 4.

Message archetypes seen:

- Employer payroll change: salary increased/reduced to X effective date D.
- Employer contract ended: no further income confirmed.
- Service-provider invoice: client approved payment X, settlement expected D, other invoices
  still pending.
- Gig platform: payout still pending, not withdrawable until completed.
- Bank: matching debit and credit are an internal transfer (semantic duplicate).
- Merchant: refund initiated but not yet credited.
- Financial service: portfolio value rose, no units sold, no cash proceeds.

No message contains a literal "ignore the rules" injection, but the safety boundary is still
required by the spec and by CLAUDE.md §8.

### 2.4 Image content

Images are consumer app screenshots (bills, payslips, order summaries). `image_04.png` is a
grocery delivery order where the visible "Item Bill ₹2854.00" is followed by a delivery-fee line
that is cropped off screen, so the true total is not fully visible. Extraction must therefore
return a confidence and the specific label it read, and the pipeline must treat ambiguity as
unresolved rather than confidently picking one number.

### 2.5 Sample decision style (25 solved rows)

Status distribution: affordable_with_plan 9, not_affordable 7, affordable_later 6,
affordable_now 3. Method distribution: not_recommended 7, full_payment 6, wait 6,
installments 5, partial_payment 1. Three rows use spending changes.

Patterns that constrain the implementation:

1. `wait` → `payment_plan` is a single `<earliest_date>:<amount>` entry and status is
   `affordable_later`.
2. `affordable_with_plan` + `full_payment` occurs when a spending change makes today's full
   payment safe (request_06, request_11, request_21). The safe amount is still reported
   *before* the change.
3. `affordable_with_plan` + `installments` occurs even when `amount_safe_to_pay ==
   requested_amount` if the user does not accept `full_payment` (request_12). Then
   `earliest_date_for_full_payment == request_date`.
4. `not_affordable` rows have an empty `earliest_date_for_full_payment` and a non-zero safe
   amount is still reported.
5. Earliest dates cluster on the 15th (salary settlement day) or the 1st.
6. Amount formatting: `payment_plan` amounts use two decimals when fractional (`620.40`,
   `996.60`) and no decimals when whole (`25256`). `amount_safe_to_pay` prints the raw
   number (`603.3`, `17229139.2`).
7. Explanation style: two short sentences, currency code + thousands separators, dates as
   "8 August 2025", and always cites the minimum balance being protected.

---

## 3. Input / Output Flow

```
dataset/*.csv, media/images/*.png
        │
        ▼
[1] Load + validate (typed Pydantic rows, enum checks, FK checks)
        │
        ▼
[2] Per-request context builder (exact-key joins, deterministic filtering)
        │
        ├──────────────► [3a] Structured facts (profile, events, options, FX)
        │
        └──────────────► [3b] EvidenceAgent (messages + images → StructuredEvidence)
                                │
        ┌───────────────────────┘
        ▼
[4] Event reconciliation (apply cancellations/amendments/duplicates/settlements, fill blank amounts)
        ▼
[5] Financial state (home-currency ledger, recurrence model, reserved debits, confirmed income)
        ▼
[6] 90-day forecast  ──► safe_amount, earliest_full_payment_date
        ▼
[7] Plan generator (full, wait, partial, installments × options, + spending-change variants)
        ▼
[8] Plan evaluator (safety, deadline, eligibility, preference filters)
        ▼
[9] Policy engine (rank → status, method, plan, changes)
        ▼
[10] ExplanationAgent (fact sheet → decision_explanation)
        ▼
[11] Consistency validator (repair or safe fallback)
        ▼
output.csv (repo root, input order preserved) + evaluation/usage_report.md
```

Stages 1, 2, 3a, 4–9, 11 are pure Python. Stages 3b and 10 are the only LLM calls.

---

## 4. Proposed Module Layout

```
code/
  main.py                      # CLI entry: python code/main.py [--sample] [--limit N] [--no-llm]
  config.py                    # env loading, paths, model name, concurrency, cache dir
  data/
    models.py                  # Pydantic row models + enums
    loader.py                  # CSV → typed rows, validation report
    fx.py                      # dated exchange-rate lookup
    context.py                 # per-request RequestContext builder
  agents/
    evidence_agent.py          # PicoAgents Agent, output_format=StructuredEvidence
    explanation_agent.py       # PicoAgents Agent, output_format=Explanation
    schemas.py                 # StructuredEvidence, EvidenceItem, Explanation
    prompts/                   # evidence_system.md, explanation_system.md (versioned)
  finance/
    reconciliation.py          # raw events + evidence → reconciled events
    recurrence.py              # recurring pattern detection
    state.py                   # FinancialState (home currency, reserved, confirmed income)
    forecast.py                # 90-day daily balance path
    safe_amount.py
    earliest_date.py
    plans.py                   # plan generation + validation (dataclasses)
    spending_changes.py
  policy/
    ranking.py                 # 6-key comparator
    decision_policy.py         # status/method mapping
    consistency.py             # final validator + repair + fallback
  middleware/
    retry.py, validation.py, safety.py, observability.py
  infra/
    cache.py                   # content-hash JSON cache
    usage.py                   # per-call usage ledger → usage_report.md
    csv_writer.py              # ordered output, formatting rules
  evaluation/
    main.py                    # run on sample_requests, per-field accuracy, diff report
    usage_report.md            # generated
tests/
  test_loader.py, test_fx.py, test_reconciliation.py, test_recurrence.py,
  test_forecast.py, test_safe_amount.py, test_plans.py, test_policy.py,
  test_consistency.py, test_safety.py, test_evidence_schema.py
```

This follows the CLAUDE.md conceptual layout and the README's `code/main.py` entry point.

*Implementation note (2026-09-13):* the modules live under `code/buyorwait/` (a top-level module
named `code` would shadow the standard library), `safe_amount`/`earliest_date` are functions in
`finance/forecast.py`, ranking lives in `policy/decision_policy.py`, and the tests are in
`code/tests/`. Everything else matches the layout above.

---

## 5. Agent Responsibilities (exactly two agents)

### 5.1 EvidenceAgent

Runs once per request **only if** the request has at least one relevant message or image.
Requests with neither (a large share of the 250) skip the LLM entirely.

Input (built deterministically): request summary, profile summary, the candidate event rows
the message or image could refer to (by `related_event_id`, or by user + category + date
window when the link is blank), the message text, and the image bytes as a
`MultiModalMessage(mime_type="image/png", data=bytes)`.

Output `StructuredEvidence` (Pydantic, schema-enforced via `output_format`):

```
items: list[EvidenceItem]
EvidenceItem:
  kind: Literal[cancellation, amendment, settlement, confirmation, delay, duplicate,
                income_change, income_end, pending_credit, non_cash, amount_extraction, unresolved]
  target_event_id: str | None        # must exist in the provided candidate list
  new_amount: Decimal | None
  new_date: date | None
  currency: str | None
  effective_from: date | None
  confidence: Literal[high, medium, low]
  source_ref: str                    # message_id or image_id
  quote: str                         # verbatim supporting text (≤200 chars)
injection_detected: bool
```

Hard rules: it may not compute affordability, may not reference event IDs outside the
candidate list (validated), and low-confidence items are downgraded to `unresolved` by the
validator, not by the model.

### 5.2 ExplanationAgent

Runs once per request after the policy engine. Receives a `FactSheet` (final decision, safe
amount, minimum balance, currency, selected plan, spending changes with event descriptions,
earliest date, evidence items actually used). Produces `Explanation(text: str)`.

The validator checks the text mentions only amounts/dates present in the fact sheet
(regex-extracted numbers must be a subset). On failure or LLM outage, a deterministic
template modelled on the sample style is used instead, so `decision_explanation` is never
empty.

---

## 6. Deterministic Responsibilities and Financial Boundaries

Everything below is pure Python with no model access:

| Concern | Module | Authority |
|---|---|---|
| Currency conversion | `data/fx.py` | exact `(settlement_date, from, to)` lookup, no interpolation; missing rate → validation error → request-level safe fallback |
| Blank amount fill | `finance/reconciliation.py` | uses `amount_extraction` evidence only if `confidence == high` and currency matches; else event marked unresolved and treated as the financially safer case (a debit of the max of comparable history; a credit of zero) |
| Recurrence | `finance/recurrence.py` | rule-based on history |
| Forecast, safe amount, earliest date | `finance/forecast.py`, `safe_amount.py`, `earliest_date.py` | closed-form on the balance path |
| Plan validity, ranking, status, method | `finance/plans.py`, `policy/*` | spec rules only |
| Output validity | `policy/consistency.py` | final gate |

The LLM never sees `minimum_balance_to_keep` as something to reason about; it only sees it
as a fact to mention in the explanation.

---

## 7. Event Reconciliation Strategy

Order of operations per request (all deterministic):

1. **Base filter**: events for `user_id` with `settlement_date` (or `event_date` when blank)
   ≤ `request_date + 90 days`. Convert to home currency using the settlement-date rate.
2. **Status classification**:
   - `settled` → historical fact (for recurrence) or, if dated after `request_date`,
     a confirmed future cash flow.
   - `scheduled` / `pending` debit → reserved on its settlement date (or on `request_date`
     if the date is past).
   - `pending` credit, `failed`, `cancelled`, `unrealized`, `non_cash` → excluded from cash.
3. **Link handling**: `linked_event_id` is informational. A settled refund linked to a settled
   expense is a real credit already in history; a re-attempted expense linked to a cancelled one
   counts once (the settled row). Valuations linked to purchases stay non-cash.
4. **Evidence application** (from `StructuredEvidence`, in the spec's precedence):
   1. explicit cancellation / settlement / amendment of a specific event → apply;
   2. newer record from the same source (message `sent_at` later than event date) → apply;
   3. settled event beats any estimate → keep settled row;
   4. unresolved conflict → financially safer reading (debit stays, credit dropped).
   Income changes (`income_change`, `income_end`) modify the projected salary from
   `effective_from` onward. `duplicate` marks one of a matching debit/credit pair as excluded.
5. **Provenance**: every reconciled event carries `source` (`dataset`, `evidence:<id>`) so the
   explanation and the tests can trace decisions.

---

## 8. 90-Day Forecast Strategy

Horizon: `request_date` (day 0) through `request_date + 90 days` inclusive.

Starting balance: `current_available_balance` from the profile, assumed to be as of
`request_date` (see Assumptions).

Daily balance path `B(t)`:

```
B(t) = B0
     - Σ reserved pending/scheduled debits with settlement ≤ t
     + Σ confirmed income with settlement ≤ t        (scheduled "Next confirmed salary", settled future credits)
     - Σ projected recurring expenses with date ≤ t
```

Recurrence detection (per user, on settled history in the 180 days before `request_date`):

- Group by (`category`, normalized `description`, `event_type`).
- Monthly: ≥ 3 occurrences with 28–31-day gaps → project on the same day-of-month.
- Weekly / biweekly: ≥ 4 occurrences with 6–8 / 13–15-day gaps → project at that cadence.
- Essential variable categories (groceries, transport, utilities, healthcare) are grouped by
  category alone and projected at their recent cadence using a conservative amount
  (the larger of mean and median of the last 3 months).
- One-time rows (investment purchases, windfalls, refunds, shopping spikes) are not projected.
- Recurring salary beyond the explicit "Next confirmed salary" row is an open question (§19);
  the default is to project it only when the history shows ≥ 3 settled monthly payroll credits
  and no `income_end` evidence exists. This must be calibrated on the samples.

Safety predicate: `safe(plan) ⇔ ∀ t ∈ [0, 90]: B_plan(t) ≥ minimum_balance_to_keep`.

Closed forms:

- `headroom(d) = min_{t ≥ d} B(t) − minimum_balance_to_keep`
- `amount_safe_to_pay = clamp(headroom(request_date), 0, requested_amount)`
- `earliest_date_for_full_payment = min { d ∈ [0, 90] : headroom(d) ≥ requested_amount }`,
  empty if none.

Both are computed **without** spending changes, per spec.

---

## 9. Payment-Plan Strategy

Candidate plans (all generated, then filtered):

| Plan | Construction | Eligibility gate |
|---|---|---|
| full_now | `request_date : requested_amount` | user accepts `full_payment` |
| wait | `earliest_date : requested_amount` | user accepts `full_payment`, earliest exists |
| partial | `request_date : safe` + `earliest_date : requested − safe` | `allows_partial_payment`, user accepts `partial_payment`, `0 < safe < requested`, earliest ≤ desired_completion_date |
| installments(option) | dates `first_payment_date + k·frequency_days`, k = 0..n−1, amount `payment_amount` | user accepts `installments`, option is `installments`, duration fits `max_installment_months` |
| any of the above + spending changes | same schedule, forecast recomputed with ≤ 3 changes | only if the base plan is unsafe and the user's reduce/stop categories permit |

Installment validation: `n × payment_amount ≈ total_payable_amount` (tolerance 0.01),
chronological, last date ≤ desired_completion_date, safe under the forecast. The output plan
string reproduces the option's amounts exactly (no rounding of our own).

`max_installment_months` interpretation: `ceil(n × frequency_days / 30) ≤ max_months`
is the default; verify against samples (request_12 has 3 × 31-day payments; check the
user's limit) and record the final rule as a documented constant.

Spending changes: candidates are recurring flexible events whose category is in
`expense_categories_user_is_willing_to_stop` (stop) or `..._to_reduce` (reduce to
`minimum_allowed_amount`) and not in `expense_categories_to_protect`. Search order: fewest
changes first (1, then 2, then 3), stops before reductions, largest monthly saving first,
and never both actions on one event. The first combination that makes the plan safe wins.

---

## 10. Policy Strategy

Ranking comparator over safe, eligible plans (lexicographic):

1. completes by `desired_completion_date` (True first)
2. no spending changes (True first)
3. total amount paid (ascending)
4. first payment date (ascending)
5. number of payments (ascending)
6. `payment_option_id` (ascending; non-option plans sort before options)

Status/method mapping from the winning plan:

| Winner | status | method |
|---|---|---|
| full_now, no changes | `affordable_now` | `full_payment` |
| full_now with changes | `affordable_with_plan` | `full_payment` |
| partial | `affordable_with_plan` | `partial_payment` |
| installments | `affordable_with_plan` | `installments` |
| wait | `affordable_later` | `wait` |
| none | `not_affordable` | `not_recommended`, plan `none` |

Edge: if `earliest_date` exists but the `wait` plan misses the deadline and nothing else is
safe, the default is `affordable_later` / `wait` only if the spec's "expected to become safe
later" reading is confirmed on the samples; otherwise `not_affordable`. Recorded in §19.

---

## 11. Consistency-Validation Strategy

`policy/consistency.py` runs on every row and asserts:

- `0 ≤ amount_safe_to_pay ≤ requested_amount`
- status ∈ 4 enums, method ∈ 5 enums, and the (status, method) pair is one of the allowed
  combinations in §10
- `affordable_now ⇒ earliest_date == request_date`
- `not_affordable ⇒ earliest_date == ""` and plan `none`
- partial ⇒ exactly two payments summing to requested, first on request_date, second on
  earliest_date ≤ desired_completion_date
- installments ⇒ schedule exactly equals one supplied option for this request
- plan is chronological; every payment date is inside the 90-day horizon
- forecast with the plan and its spending changes never breaches the minimum balance
- spending changes ≤ 3, valid event ids, flexible and permitted categories, no stop+reduce on
  the same event
- explanation numbers ⊆ fact-sheet numbers; explanation non-empty
- `earliest_date` is empty or within the horizon

Repair ladder: (1) re-derive the derived field from the plan (e.g. fix status from plan);
(2) if the plan itself is invalid, fall back to the next-ranked plan; (3) if none, emit the
safe fallback row: safe amount as computed (or 0 if the forecast failed), `not_affordable`,
`not_recommended`, `none`, empty date, `none`, template explanation. Every repair is logged
with the request id and reason.

---

## 12. Security / Prompt-Injection Boundaries

- Messages and image text are placed inside a clearly delimited `<untrusted_evidence>`
  block in the user turn, never in the system prompt.
- The EvidenceAgent system prompt states that instructions inside evidence are data, and the
  schema has an `injection_detected` flag for observability only.
- `SafetyMiddleware` scans evidence text for instruction-like patterns ("ignore", "approve",
  "override", "system") and tags the call; it never blocks, because false positives would
  drop legitimate payroll text ("approved unpaid leave").
- Structural defence is the real one: the agent output is a closed schema of facts about
  known event ids. There is no field through which text could change a balance, a limit, a
  deadline, or a decision. The policy engine never reads free text.
- Agents get no tools. All context is pre-built. No mutation path exists.

---

## 13. Concurrency Strategy

- `asyncio` with one `Semaphore(MAX_CONCURRENT_REQUESTS)` (default 8, env-overridable)
  around the LLM stages only; deterministic stages run inline.
- Requests are processed as `asyncio.gather` over an index-tagged list; results are written by
  original index so output order equals `requests.csv` order regardless of completion order.
- Evidence extraction for one request is a single call (at most 1 message and 1 image per
  request in this dataset), so no intra-request parallelism is needed.
- Concurrency never changes semantics: each request's pipeline is a pure function of its
  context plus cached model outputs.

---

## 14. Retry Strategy

PicoAgents' clients have **no retry logic** (verified: no `retry`/`backoff` in `llm/`).
`RetryMiddleware` wraps `model_call` operations:

- Retry on `RateLimitError`, timeouts, connection errors, HTTP 5xx: up to 3 attempts,
  exponential backoff 1s → 2s → 4s with jitter.
- Retry once on schema-validation failure of the model output (re-prompt with the validation
  error appended).
- Never retry `AuthenticationError` or `InvalidRequestError`.
- After exhaustion: record failure, return `StructuredEvidence(items=[], unresolved=True)` or
  the template explanation; the request continues.

---

## 15. Caching Strategy

`infra/cache.py`: JSON files under `code/.cache/` keyed by
`sha256(prompt_version | schema_version | model | canonical_context_json | image_bytes_sha256)`.

- Cached: EvidenceAgent outputs, ExplanationAgent outputs.
- Not cached: anything deterministic (it is cheaper to recompute than to key correctly).
- Cache hits are counted in the usage ledger and reported separately so `usage_report.md`
  can state real calls for the final run. The final evaluation run will be executed with the
  cache cleared so the report reflects actual model calls (CLAUDE.md §26: never fabricate).

---

## 16. Observability Strategy

- `ObservabilityMiddleware` (subclass of PicoAgents `BaseMiddleware`) records per model
  call: request id (passed via `MiddlewareContext.metadata`), agent name, model, latency,
  `Usage.tokens_input/tokens_output`, retry count, cache hit, error.
- Structured JSONL log at `code/.runs/<timestamp>/calls.jsonl` plus a per-request stage log
  (stage reached, fallback used, validator repairs).
- `infra/usage.py` aggregates the ledger into `evaluation/usage_report.md`: provider, model,
  calls, tokens in/out, totals, per-request averages, estimated cost from an explicit pricing
  table in `config.py` (PicoAgents' built-in `_estimate_cost` only knows gpt-4-era prices and
  would be wrong for `gpt-5.6`).

---

## 17. Testing Strategy

`pytest` with no network. Fixtures build small synthetic users.

- **Data layer**: required columns, enum validation, FK integrity, FX lookup hit/miss,
  blank-amount detection, image path resolution.
- **Reconciliation**: cancellation, amendment, settlement, semantic duplicate, income change
  with effective date, unresolved evidence → safer reading, blank amount high/low confidence.
- **Recurrence**: monthly detection with 28–31-day jitter, weekly, non-recurring spikes
  ignored, salary projection on/off.
- **Forecast**: pending debit reserved, pending credit ignored, unrealized ignored, minimum
  balance boundary (equal is safe, one unit below is not).
- **Safe amount / earliest date**: closed-form vs brute force on random paths.
- **Plans**: each plan type, installment schedule mismatch, deadline violation, partial
  conditions each toggled off, ranking tie-breaks including `payment_option_id`.
- **Spending changes**: stop, reduce, protected category rejected, > 3 rejected, stop+reduce
  same event rejected.
- **Policy/consistency**: contradictory states from CLAUDE.md §33 must fail validation and be
  repaired or fall back.
- **Safety**: injection-style message, image-text instruction, missing image file, malformed
  model JSON, simulated timeout and API failure, unknown event id in evidence.
- **Golden**: the 25 sample rows are a regression suite for the deterministic core, with the
  evidence stage replayed from cached outputs.

---

## 18. Evaluation Strategy

`code/evaluation/main.py`:

1. Run the pipeline on `sample_requests.csv` inputs.
2. Report per-field exact-match accuracy (safe amount within 0.01, status, method, plan string,
   earliest date, spending changes) and a side-by-side diff for every miss.
3. Keep a `--no-llm` mode that uses only deterministic stages, so financial-rule regressions
   are separable from evidence-interpretation regressions.
4. Development loop: fix the layer that failed, add a regression test, rerun.
5. Freeze prompts, model name, thresholds, and ranking, then run the full 250-request set
   once with a cleared cache to produce `output.csv` and `usage_report.md`.

---

## 19. Implementation Phases (time-boxed for the remaining window)

| Phase | Scope | Exit criterion |
|---|---|---|
| 1 Data layer | venv, trimmed requirements, models, loader, FX, context builder, tests | all CSVs load typed with zero validation errors |
| 2 Deterministic core | reconciliation (evidence stubbed), recurrence, forecast, safe amount, earliest date, plans, changes, policy, consistency, tests | `--no-llm` run on samples; target ≥ 18/25 on status and method |
| 3 PicoAgents | schemas, two agents, middleware, model client, cached image extraction | evidence extracted for all 16 images with confidence |
| 4 Integration | `main.py` end-to-end, error isolation, ordered CSV, root `output.csv` | full 250-row run completes |
| 5 Reliability | retries, cache, observability, usage report | report generated from a real run |
| 6 Evaluation | sample diffs, adversarial tests, fixes | regression suite green |
| 7 Freeze + final | clear cache, final run, package `code.zip`, README | submission artifacts ready |

---

## 20. Risks

1. **Calibration risk (highest)**: the hidden ground truth was produced by an unknown forecast
   convention (how variable spending is projected, whether recurring salary counts, day-of-month
   handling). Mitigation: derive rules from the 25 samples early in Phase 2, test hypotheses
   against sample safe amounts and earliest dates.
2. **Dependency conflict**: `picoagents` pins `openai==1.107.1`; the machine has 2.45.0.
   Mitigation: dedicated venv.
3. **Model availability**: `.env` names `gpt-5.6`; if that model is unavailable or lacks
   vision, image extraction fails. Mitigation: model name from env, smoke test in Phase 3,
   Anthropic client as a documented alternative.
4. **Cropped or ambiguous images** (image_04): wrong extracted amount silently shifts a
   forecast. Mitigation: confidence gating, label capture, unresolved → safer fallback.
5. **Language**: many messages are in Bahasa Indonesia. Mitigation: the prompt states evidence
   may be in any language; tests include one Indonesian message.
6. **Time**: ~22 hours total. Mitigation: Phase 2 is the priority; Phase 5 items are
   implemented minimally first.
7. **Floating point**: use `Decimal` for all money; format at the edge only.

---

## 21. Assumptions (to be confirmed against samples or documented)

- `current_available_balance` is the balance as of `request_date`.
- Events dated after `request_date` with status `settled` exist in the data (dataset ends
  2026-09-03, some requests are dated earlier); they are treated as confirmed future cash flows
  only if they are income or reserved debits; historical rows after the request date are not
  used for recurrence detection (no look-ahead beyond confirmed rows).
- A pending or scheduled debit with a settlement date before `request_date` is reserved on
  day 0.
- A blank `settlement_date` falls back to `event_date`.
- The first forecast day is `request_date` itself and obligations on that day are paid before
  the request.
- `payment_frequency_days` × `number_of_payments` measured in 30-day months is the
  installment duration compared against `max_installment_months`.
- Output amounts are in home currency, formatted as in the samples (§2.5 item 6).

---

## 22. Open Questions

1. Should recurring salary be projected beyond the single "Next confirmed salary" row? The
   spec says "count confirmed salary on its settlement date" and "do not invent unsupported
   future income", but sample request_03 (earliest 2019-11-15, two salary cycles after the
   request) suggests some projection is expected. Resolve by testing both hypotheses on all 25
   samples in Phase 2.
2. When `earliest_date` exists but is after `desired_completion_date`, and no other plan is
   safe: `affordable_later`/`wait` or `not_affordable`? Samples 14 and 24 hint at the
   latter ("cannot be completed safely within 90 days") but their earliest dates are blank.
3. How is essential variable spending (groceries, transport) projected: last month's total,
   3-month average, or the maximum week? Sample request_19 (safe 28,820 with a 199,545 balance
   and a 92,800 minimum) can be back-solved to pick the convention.
4. For a blank-amount event whose image is ambiguous (item subtotal vs total), which value did
   the ground truth use? Default: the largest clearly labelled total; record as unresolved if
   no total is visible.
5. Exact `max_installment_months` comparison rule.
6. Whether the explanation should be an LLM output at all given how templated the samples
   are. Current plan: LLM with strict grounding validation and a template fallback; if
   evaluation shows the template is closer to the sample style, the LLM step can be made
   optional by config without touching the decision path.
