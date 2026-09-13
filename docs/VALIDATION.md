# Validation Report

## Part 1 — Dataset validation (stage 1 loader)

Result of running the stage-1 loader (`code/buyorwait/data/loader.py`) against `dataset/`
on 2026-09-12. Produced with:

```
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'code'); from buyorwait.data import load_dataset; print(load_dataset(strict=False).report.summary())"
```

### Outcome

**0 errors, 0 warnings.** Every row in every participant-facing file validated, and every
cross-file relationship resolved. Load time: 0.25 s.

### Rows validated

| File | Rows | Model |
|---|---|---|
| `financial_profiles.csv` | 275 | `FinancialProfile` |
| `financial_events.csv` | 25,342 | `FinancialEvent` |
| `exchange_rates.csv` | 134 | `ExchangeRate` |
| `requests.csv` | 250 | `Request` |
| `sample_requests.csv` | 25 | `SampleRequest` |
| `request_payment_options.csv` | 790 | `PaymentOption` |
| `messages.csv` | 215 | `Message` |
| `images.csv` | 16 | `ImageRef` |
| `output.csv` (template) | 250 | header and order check only |
| **Total** | **27,297** | |

### Per-row checks that passed on every row

- Header contains exactly the expected columns (no missing, no extra).
- Every value parses into its declared type: `Decimal` money, `YYYY-MM-DD` dates, ISO-8601
  timestamps, integers, booleans.
- Every enumerated column is within its closed vocabulary: currency, event type, direction,
  status, flexibility, request type, payment method, message source, affordability status,
  output method.
- Row invariants: amounts non-negative; `non_cash` rows are `unrealized` and vice versa;
  `desired_completion_date` on or after `request_date`; multi-payment options carry a
  frequency; full-payment options have exactly one payment; seller options are never
  `partial_payment`; sample rows satisfy `0 <= amount_safe_to_pay <= requested_amount`.
- Primary ids unique within each file (`user_id`, `event_id`, `request_id`,
  `payment_option_id`, `message_id`, `image_id`); no `request_id` shared between
  `requests.csv` and `sample_requests.csv`.

### Cross-file checks and the rows they covered

| Check | Rows covered | Result |
|---|---|---|
| Event `user_id` exists in profiles | 25,342 | all resolve |
| Request `user_id` exists in profiles | 275 | all resolve |
| Foreign-currency cash event has a rate for its effective date and pair | 140 | all found |
| Blank-amount event has an `images.csv` row | 16 | all found |
| `linked_event_id` exists and belongs to the same user | 58 | all resolve |
| Message `request_id` / `related_event_id` exist and belong to the message's user | 167 | all resolve |
| Image `request_id` / `related_event_id` exist and belong to the image's user | 32 | all resolve |
| Image PNG present at `dataset/media/images/<image_id>.png` | 16 | all present |
| Each request has 2 to 4 options including exactly one `full_payment` row | 275 | all pass |
| Full-payment option equals request amount, dated on request date, zero fee | 275 | all pass |
| Option arithmetic: `n × payment_amount = total_payable_amount = requested + fee` (±0.01) | 790 | all pass |
| No option starts before its request date | 790 | all pass |
| Profile accepts installments ⇔ `max_installment_months` set | 275 | all consistent |
| Profile balance not already below `minimum_balance_to_keep` | 275 | none below |
| Exchange-rate `(date, from, to)` keys unique | 134 | all unique |
| `output.csv` template ids match `requests.csv` order | 250 | identical |

### Interpretation

The dataset is a clean, generated benchmark. No row needs repair, no relationship is
broken, and no exchange rate is missing. The challenge's difficulty lies in interpretation
(recurrence detection, evidence handling, the forecast convention), not in data cleaning.

The loader's error and warning paths remain in place and are covered by synthetic-data tests
in `code/tests/test_loader.py`, because the specification requires that one malformed input
must not crash the batch and hidden evaluation conditions may differ from this file set.

### Reproduce

```
.venv\Scripts\python.exe -m pytest code/tests -q
```

At the time of Part 1, 26 tests passed, including `test_real_dataset_loads_without_errors`.

---

## Part 2 — Context builder, FX facts, and EvidenceAgent (stages 2, 3a, 3b)

Recorded 2026-09-12 after implementing `code/buyorwait/data/fx.py`,
`code/buyorwait/data/context.py`, and `code/buyorwait/agents/`.

### Unit tests (no network)

```
.venv\Scripts\python.exe -m pytest code/tests -q
```

**55 passed** (26 loader, 10 context/FX, 19 evidence).

| Area | What is proven |
|---|---|
| FX | exact date and direction lookup; same-currency identity; no inversion; no nearest-date fallback |
| Context | horizon and history windows; home-currency conversion on settlement date; non-cash rows never converted; blank amounts stay `None`; events after the horizon excluded; missing rate recorded in `fx_issues`, not fatal; messages after the request date excluded; candidate events by explicit id plus linked rows, or by window with a deterministic cap; input order preserved |
| Context, real data | contexts build for all 275 requests with zero FX issues; all 16 image sources resolve to an existing PNG and include their explicit event; all 16 blank amounts are visible to their request |
| Evidence schema | strict-JSON-compatible (`additionalProperties: false`, enum kinds); unknown kind rejected |
| Prompt assembly | facts block carries request, user, candidates, and `BLANK` for missing amounts; evidence wrapped in `<untrusted_evidence>`; image attached as `image/png` multimodal message |
| Validation | good extraction accepted as `Decimal`; eleven bad-item cases each downgraded to `unresolved` with a reason (unknown id, missing id, wrong currency, missing amount, negative amount, bad date, missing effective date, medium/low confidence, wrong source ref, unknown currency); extraction on an event that already has an amount rejected; income change without an event accepted |
| Extractor | `NullEvidenceExtractor` skips; `PicoEvidenceExtractor` with a stubbed client returns validated items and usage on success, and an empty result with the error text on a simulated API outage |

### Live smoke test (real API, two calls)

Purpose: confirm the PicoAgents integration end to end (strict schema accepted, image transported,
output parsed, usage reported). Not an accuracy evaluation. Model resolved from `.env`:
`gpt-5.6-luna`.

| Request | Source | Result | Validation | Tokens in / out | Latency |
|---|---|---|---|---|---|
| request_19 | image_04 (grocery order, cropped) | `amount_extraction` → event_1700, 2854.0 INR, high; quote "Item Bill ₹2854.00" | accepted | 2,263 / 166 | 3.6 s |
| request_02 | message_01 (Indonesian payroll notice) | `income_change`, 42,750,000 IDR, effective 2025-08-15, high | accepted | 5,777 / 156 | 2.0 s |

Cross-check for request_02: user_02 has five settled payroll credits of 33,345,000 IDR on the
15th (March–July 2025) and no scheduled next-salary row, so the message is the only source of
the August amount. The sample's earliest full-payment date of 2025-09-15 is consistent with
salary landing on the 15th.

### Findings recorded for later phases

- **PicoAgents swallows client exceptions inside `Agent.run()`** and returns
  `finish_reason="stop"` with no assistant message. The extractor therefore uses `run_stream()`
  and treats an `ErrorEvent` as failure. Covered by the stub test.
- **Cropped image ambiguity.** For image_04 the model reported the visible item bill (2,854)
  with high confidence although the order total is cut off. Whether the ground truth used the
  item bill or a larger total is open question 4 in ARCHITECTURE.md; the prompt may need an
  explicit rule once sample comparison is possible.
- The two live calls are not cached; they will be repeated once caching exists (Phase 5).

---

## Part 3 — Stages 4–11, CLI, and full run

Recorded 2026-09-13 after implementing `code/buyorwait/finance/`, `code/buyorwait/policy/`,
`code/buyorwait/agents/explanation_agent.py`, `code/buyorwait/infra/`, `code/buyorwait/pipeline.py`,
`code/main.py`, and `code/evaluation/main.py`.

### Unit tests (no network)

```
.venv\Scripts\python.exe -m pytest code/tests -q
```

**72 passed** (26 loader, 10 context/FX, 19 evidence, 17 engine).

| Area | What is proven |
|---|---|
| Reconciliation | pending debit reserved; pending credit, failed, unrealized excluded; settled history kept; cancellation, settlement with date, income change applied from evidence; unresolved item applies nothing; a settled row is never cancelled by evidence; blank amount → safer estimate, or the image amount when a high-confidence extraction exists |
| Recurrence / state | monthly and weekly patterns detected and projected; investments and one-offs not projected; reserved, projected income and expense flows built; pending refund never a flow; flexible catalogue with stop/reduce permissions and floor; `income_end` stops projection from its date |
| Forecast | headroom, safe amount clamp, earliest date, equality-is-safe boundary, first breach; closed forms equal brute force on 30 random 91-day paths |
| Plans / gates / policy | full, wait, partial, installment plans generated with exact option schedules and totals; preference and deadline gates reject; decision maps to allowed status/method pairs; a plan that fails only on safety is rescued by a permitted `stop` change when partial and wait are unavailable |
| Consistency | a correct row passes; contradictory `affordable_now` + future earliest date, installment plan not in the options, more than three changes, and an explanation citing an unknown number are all rejected; the repair ladder replaces an ungrounded explanation with the template and falls back to the safe row on an invalid amount |
| Formatting | plan amounts `620.40` / `25256`; safe amounts `603.3` / `17229139.2` / `28820` |
| Pipeline | two requests, one forced to raise inside the policy stage: output order preserved, the failing request gets the fallback row with its stage and error recorded, the usage report shows zero model calls and one fallback |

### Stage smoke tests against the 25 solved samples

Each stage was exercised on the real data through the evaluation harness
(`python code/evaluation/main.py --no-llm`, then with model stages on).

**Batch 1 (reconciliation, recurrence, state, forecast) — first result, draft convention:**
safe amount within 0.5% on 3/25, earliest date exact on 14/25. Diagnosis on the misses showed the
draft convention was wrong about income: user_05's last payroll row is "Final employer payroll",
user_10's income is variable weekly gig payouts (and a message says the next payout is pending),
user_01 has only a prorated first salary plus a scheduled confirmed amount. A 60-combination
brute force over projection knobs plus per-user back-solving led to the settings recorded in
`docs/forecast_rules.md` §11. **After calibration:** earliest date exact 17/25, safe amount within
0.5% 5/25 and within 10% 13/25. The organizers' exact variable-spending convention could not be
recovered from the samples (subset-sum reconstruction of evidence-free users produced only
coincidental matches); safe amounts therefore carry a systematic deviation on some users.

**Batch 2 (plans, gates, spending changes, policy) and batch 3 (explanation, consistency, CLI):**

| Field | No-llm | With model stages |
|---|---|---|
| `affordability_status` | 20/25 | 20/25 |
| `recommended_payment_method` | 22/25 | 21/25 |
| status **and** method | 20/25 | 20/25 |
| `payment_plan` | 19/25 | 18/25 |
| `earliest_date_for_full_payment` | 17/25 | 16/25 |
| `spending_changes_needed` | 22/25 | 22/25 |
| `amount_safe_to_pay` within 0.5% / 10% | 5/25 · 13/25 | 5/25 · 11/25 |
| fallback rows / validator repairs | 0 / 0 | 0 / 0 |

Reports: `code/evaluation/sample_evaluation_nollm.md`, `code/evaluation/sample_evaluation_llm.md`.

Two defects were found and fixed by these smoke tests before the full run:

- `reduce_to` floors were quantised to cents only when rendered, so the validator re-checked a
  slightly different path than the policy had used and repaired one row. Floors are now quantised
  at generation time.
- The safe amount was printed half-up to cents, so a partial plan's first payment could exceed the
  exact headroom by a fraction of a cent and be flagged unsafe. The safe amount is now floored to
  cents before any plan is built.

The five remaining status/method misses are forecast-convention cases (request_06, 08, 11, 13 and
one of 03/07), not plan or policy errors: in each, the ground truth's safe amount or earliest date
differs from ours and the policy then correctly follows our numbers.

**Explanation grounding (model stages on):** all 25 model-written explanations passed the
numeric grounding check on the first attempt; no template fallbacks were needed.

### Full run (250 evaluation requests, model stages on, cache cleared first)

```
.venv\Scripts\python.exe code/main.py --clear-cache --concurrency 8
```

| Item | Result |
|---|---|
| Rows written to `output.csv` | 250, header plus one row per `requests.csv` row, same order |
| Fallback rows | 0 |
| Validator repairs | 0 |
| Per-request exceptions | 0 |
| Model calls | 459 (evidence: one per message/image source; explanation: one per request) |
| Failed model calls / retries | 0 / 0 |
| Explanations written by the model and accepted by the grounding check | 250 / 250 |
| Input / output tokens | 1,185,725 / 84,670 (total 1,270,395; 5,081.6 per request) |
| Wall time | 154 s at concurrency 8 |
| Model | `gpt-5.6-luna` (from `.env`), provider OpenAI, via PicoAgents 0.5.0 |

Output distribution: `not_affordable` 91, `affordable_with_plan` 65, `affordable_now` 54,
`affordable_later` 40; methods `not_recommended` 91, `full_payment` 61, `installments` 50,
`wait` 40, `partial_payment` 8; 19 rows carry spending changes; 92 rows have an empty earliest
date. Every row passed the consistency validator on the first attempt.

`code/evaluation/usage_report.md` was generated from this run's call ledger
(`code/.runs/20260912T231433Z/calls.jsonl`). Cost is reported as "not configured" because no
price for the model was known; setting `OPENAI_PRICE_INPUT_PER_1M` and
`OPENAI_PRICE_OUTPUT_PER_1M` and running `code/evaluation/regenerate_usage_report.py` on the run
directory fills the cost columns without re-running the model.

### Known limitations

- `amount_safe_to_pay` follows our documented forecast convention, which reproduces the
  organizer samples exactly on 5/25 and within 10% on about half; the categorical fields agree on
  20/25. The convention is frozen (`docs/forecast_rules.md` §11) and was not tuned after the full
  run began.
- Cropped images (e.g. image_04) yield the visible subtotal at high confidence; whether the
  ground truth used a larger total is unknown.

---

## Part 4 — EvidenceAgent moved to `gpt-5.6-terra`, reasoning effort `medium`

Recorded 2026-09-13. Configuration now lives in `.env` / `.env.example`:
`EVIDENCE_MODEL=gpt-5.6-terra`, `EVIDENCE_REASONING_EFFORT=medium`; the ExplanationAgent keeps
`OPENAI_MODEL` (`gpt-5.6-luna`). Each agent gets its own client; `buyorwait/config.py` injects the
reasoning effort into every request because PicoAgents' `Agent` does not forward per-call
parameters. 72 tests still pass.

### Smoke (two live sources)

| Source | Result with `gpt-5.6-terra` / medium |
|---|---|
| request_19 / image_04 | `amount_extraction` 2854.0 INR high (as before) plus a `confirmation` item; 2,263 in / 331 out tokens |
| request_11 / message_08 | The message states a confirmed base salary of IDR 38,760,000 with no effective date. The model returned `income_change` **without** `effective_from`; code validation downgraded it to `unresolved`, so nothing changed. With the previous model this message had produced an effective date and turned request_11 into `not_affordable`. |

### Sample evaluation (model stages on)

| Field | Result |
|---|---|
| `affordability_status` | 20/25 |
| `recommended_payment_method` | 22/25 |
| status and method | 20/25 |
| `payment_plan` | 19/25 |
| `earliest_date_for_full_payment` | 16/25 |
| `spending_changes_needed` | 22/25 |
| `amount_safe_to_pay` exact / within 10% | 5/25 · 12/25 |
| fallback rows / repairs | 0 / 0 |

request_11 returned to the deterministic result (`affordable_now`), which differs from the sample
only by the forecast convention. Report: `code/evaluation/sample_evaluation_llm.md`.

### Final full run (250 requests, evidence on `gpt-5.6-terra`/medium, explanations on `gpt-5.6-luna`, cache cleared first)

```
.venv\Scripts\python.exe code/main.py --clear-cache --concurrency 8
```

| Item | Result |
|---|---|
| Rows written to `output.csv` | 250, input order preserved |
| Fallback rows / validator repairs / exceptions | 0 / 0 / 0 |
| Model calls | 459 = 209 evidence (`gpt-5.6-terra`) + 250 explanation (`gpt-5.6-luna`) |
| Failed calls after retries / retries performed | 0 / 2 (two transient errors, both recovered on retry) |
| Explanations written by the model and accepted by the grounding check | 250 / 250 |
| Tokens | evidence 1,011,832 in / 30,759 out; explanation 173,954 in / 16,715 out; total 1,233,260 (4,933 per request) |
| Wall time | 104 s at concurrency 8 |

Output distribution is unchanged from the previous full run (`not_affordable` 91,
`affordable_with_plan` 65, `affordable_now` 54, `affordable_later` 40; 19 rows with spending
changes). `code/evaluation/usage_report.md` now lists both models separately and in total; cost
remains "not configured" pending prices. Ledger: `code/.runs/20260912T233453Z/`.

---

## Part 5 — Evidence model reverted to `gpt-5.6-luna`; forecast convention review

Recorded 2026-09-13. `EVIDENCE_MODEL=gpt-5.6-luna`, no reasoning override (the per-agent
settings remain available).

### Review finding fixed before the final run

A sweep over all 275 requests showed 94 users with salary history but **no income at all** in the
horizon, and 54 base paths that breached the minimum without any request payment. No sample user
has a zero safe amount, so this was a defect, not a convention choice. Cause: the salary rule
required every historical amount to be identical, which discarded payrolls with a step change
(users 31 and 49: three months at one level, then two at a lower level) and any small variation.
Fix in `finance/recurrence.py` (`_salary_level`): project the latest level after a step change,
the median when relative variation is at most 10%, and nothing for variable freelance or gig
income. After the fix: 77 users without projected income (all freelance/gig/ended/short-history
cases) and 43 self-breaching paths.

### Sample evaluation after the fix (no model calls)

| Field | Result |
|---|---|
| `affordability_status` | 20/25 |
| `recommended_payment_method` | 22/25 |
| status and method | 20/25 |
| `payment_plan` | 19/25 |
| `earliest_date_for_full_payment` | 18/25 (was 17) |
| `spending_changes_needed` | 22/25 |
| `amount_safe_to_pay` exact / within 10% | 5/25 · 14/25 (was 13) |
| fallback rows / repairs | 0 / 0 |

72 tests pass. Report: `code/evaluation/sample_evaluation_nollm.md`.

### Convention fixes and the 25-sample rerun

Six review points were implemented as switchable knobs and scored on the samples through the
full deterministic pipeline (48 combinations; details in `docs/forecast_rules.md` §12). Kept:
salary step-change handling, whole-horizon safety check (spec-literal), de-duplication on
category **and** description, latest-prior-rate fallback for foreign-currency income overrides,
exclusion of estimated blank amounts from pattern statistics. Rejected because they scored worse:
dropping discretionary variable spending, monthly lumps for weekly patterns, maximum instead of
median. 72 tests pass.

| Field | No-llm | With model stages |
|---|---|---|
| `affordability_status` | 20/25 | 20/25 |
| `recommended_payment_method` | 22/25 | 22/25 |
| status and method | 20/25 | 20/25 |
| `payment_plan` | 19/25 | 19/25 |
| `earliest_date_for_full_payment` | 18/25 | 17/25 |
| `spending_changes_needed` | 22/25 | 22/25 |
| `amount_safe_to_pay` exact / within 10% | 5/25 · 14/25 | 5/25 · 13/25 |
| fallback rows / repairs | 0 / 0 | 0 / 0 |

Reports: `code/evaluation/sample_evaluation_nollm.md`, `code/evaluation/sample_evaluation_llm.md`.

### Final full run after the convention fixes (250 requests, `gpt-5.6-luna` for both agents, cache cleared)

```
.venv\Scripts\python.exe code/main.py --clear-cache --concurrency 8
```

| Item | Result |
|---|---|
| Rows written to `output.csv` | 250, input order preserved |
| Fallback rows / validator repairs / exceptions | 0 / 0 / 0 |
| Model calls / failures / retries | 459 / 0 / 0 |
| Explanations written by the model and accepted by the grounding check | 250 / 250 |
| Tokens | 1,186,104 in / 83,946 out; total 1,270,050 (5,080 per request) |
| Wall time | 159 s at concurrency 8 |

Output distribution: `not_affordable` 83 (was 91), `affordable_with_plan` 66, `affordable_now` 59
(was 54), `affordable_later` 42 (was 40); 18 rows with spending changes; 83 empty earliest dates
(was 92); 39 rows with a zero safe amount (was 54 before the salary fix). Ledger:
`code/.runs/20260912T235233Z/`. `code.zip` rebuilt from this run.

### Structural fixes from the earliest-date review

- **Monthly anchor day.** Projection used the most common day-of-month of the last four
  occurrences; user_07's payroll moved from the 15th to the 23rd (message + history) and was still
  projected on the 15th. Now the latest occurrence's day is used (newest record wins). Sample
  earliest dates 18→19 (request_07 exact); nothing lost.
- **Date-only income changes.** `income_change` evidence may now carry just `new_date`; the
  projected salary dates on/after it move to that day-of-month (`evidence-v2` prompt, cache keys
  invalidated). Covered by a new test (73 tests).
- **Uniform expense scale** 0.85–1.20 was swept as a stand-in for the unknown convention: none of
  requests 08, 11, 13, 21 flips at any scale (they need corrections in opposite directions and of
  different sizes), so they are not correctable without the organizers' procedure. Kept at 1.
- Remaining earliest-date misses: request_03 (a month-end "net salary" row and a one-time
  adjustment; the organizers' salary stream cannot be inferred) and request_18 (a bank message
  about an internal transfer whose rows are not in the events).

---

## Part 6 — Token efficiency and batching

Recorded 2026-09-13. Changes: compact pipe-delimited evidence facts (`evidence-v3`); batched
evidence calls for message-only sources (4 per call) and batched explanations (8 per call) with
per-item fallback to single calls; batch sizes exposed as CLI flags. Pipeline restructured into
phases (contexts, evidence, deterministic stages, explanations, finalize) while keeping
per-request isolation and input order. 77 tests pass (4 new: batch success with partial
fallback, whole-batch failure falling back for every item, explanation batch with one missing
item, batch size 1 disabling batching).

### 25-sample live run, batching on, cache disabled

| Item | Before | After |
|---|---|---|
| Model calls | 47 (22 evidence + 25 explanation) | 14 (6 single evidence incl. images, 4 evidence batches, 1 single + 3 batched explanations) |
| Input / output tokens | ≈123,000 / ≈5,600 (estimated from the full run's per-call averages) | 52,659 / 8,079 |
| Tokens per request | ≈5,000 | 2,430 |
| Estimated cost (25 requests) | ≈USD 0.034 | USD 0.020 |
| Batch fallbacks to single calls | — | 2 items |
| Fallback rows / repairs / errors | 0 / 0 / 0 | 0 / 0 / 0 |

Decision fields are identical to the unbatched run (status 20/25, method 22/25, plan 19/25,
earliest 18/25, changes 22/25, safe amount within 10% 13/25). Explanations remain model-written
and grounded on all 25 rows.

---

## Part 7 — Final submission run (2026-09-13)

Configuration frozen before this run: evidence and explanation agents on `gpt-5.6-luna`,
no reasoning override; forecast knobs `income_mode=fixed_only` with the stable-salary rescue,
`amount_stat=median`, `include_submonthly=True`, `generic_interval=False`,
`project_discretionary_variable=True`, `front_load=none`, `expense_scale=1`,
`SAFETY_FROM_DAY_ZERO=True`; evidence batches of 4, explanation batches of 8.

```
.venv\Scripts\python.exe code/main.py --clear-cache --concurrency 8
```

| Item | Result |
|---|---|
| Rows written to `output.csv` | 250, exact columns, input order preserved |
| `0 <= amount_safe_to_pay <= requested_amount` | holds on all 250 |
| Fallback rows / validator repairs / exceptions | 0 / 0 / 0 |
| Empty or fallback-text explanations | 0 |
| Model calls | 93 (82 batched, covering 448 items) |
| Failed calls / retries / batch fallbacks | 0 / 0 / 0 |
| Tokens | 444,827 in / 84,831 out; 529,658 total, 2,119 per request |
| Estimated cost | USD 0.1908 total, USD 0.000763 per request |
| Wall time | 114 s at concurrency 8 |

Output distribution: `not_affordable` 74, `affordable_with_plan` 68, `affordable_now` 64,
`affordable_later` 44; methods `not_recommended` 74, `full_payment` 69, `installments` 55,
`wait` 44, `partial_payment` 8.

Compared with the pre-fix run of the same dataset, the stable-salary rescue moved 17 requests out
of `not_affordable` (91 → 74) and batching plus the compact prompt cut model calls from 459 to 93
and cost from USD 0.338 to USD 0.191.

Ledger: `code/.runs/20260913T085313Z/`. `code.zip` rebuilt from this run (55 files, 151 KB,
verified to contain `code/main.py`, both prompts, `code/README.md`, `requirements.txt` and
`code/evaluation/usage_report.md`, and to exclude `.env`).
