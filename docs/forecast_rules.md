# Forecast Rules

Stages 5 and 6 of the pipeline ([flow.md](flow.md)): building the financial state from the
reconciled events, then walking the 90-day balance path and deriving `amount_safe_to_pay` and
`earliest_date_for_full_payment`.

Everything here is deterministic arithmetic on already-trusted rows. No model is involved.

Authority: `problem_statement.md` ("90-Day Safety Check"), `AGENTS.md` §6.3, `CLAUDE.md` §9–§11.
Constants are documented per `CLAUDE.md` §41. Items marked *to calibrate* must be settled against
the 25 solved samples before the configuration is frozen.

---

## 1. Windows

| Name | Value | Why |
|---|---|---|
| Horizon | `request_date` (day 0) through `request_date + 90` days, inclusive | challenge requirement |
| History window | the 180 days before `request_date` | six monthly cycles: enough to see a monthly pattern at least three times with one missed month; a weekly pattern ~26 times |
| Recent window | the 90 days before `request_date` | used for the amount estimate of variable spending (*to calibrate*) |

## 2. Financial state (inputs to the walk)

From the profile:

- `B0 = current_available_balance`, assumed to be the balance at the start of `request_date`.
- `M = minimum_balance_to_keep`.

From the reconciled events:

- **Reserved debits**: class `reserved`, each `(date, amount)`. A date before day 0 becomes day 0.
- **Confirmed income**: class `confirmed`, each `(date, amount)`.
- **History**: class `history`, used only by §3.

From recurrence detection (§3):

- **Projected recurring expenses**: `(date, amount, event_id, category, flexibility, floor)`.
- **Projected recurring income**: `(date, amount)`, subject to `income_change` / `income_end`.

From the profile and history together:

- **Flexible catalogue**: recurring expense patterns whose `flexibility` is not `fixed`, with the
  most recent `event_id`, category, `minimum_allowed_amount`, and whether the user's
  reduce / stop lists permit the category and the protect list does not forbid it. Used only by
  the spending-change search ([payment_plan_rules.md](payment_plan_rules.md) §5).

## 3. Recurrence detection

Run on **history** rows only (settled, before the request, inside the history window).

**Grouping key.** `(event_type, category, normalized description)` where normalization lowercases
and strips digits and punctuation. Variable essential categories (`groceries`, `transport`,
`utilities`, `healthcare`) are additionally grouped by `(event_type, category)` alone, because
their descriptions vary per purchase ("Bulk pantry shop", "Local market purchase").

**Cadence.** Sort occurrences by effective date and take the gaps between consecutive ones.

| Cadence | Detected when | Projection |
|---|---|---|
| monthly | ≥ 3 occurrences and every gap in 27–32 days | same day-of-month as the last occurrence, clamped to month length |
| biweekly | ≥ 4 occurrences and every gap in 13–15 days | last date + 14k |
| weekly | ≥ 4 occurrences and every gap in 6–8 days | last date + 7k |
| none | otherwise | not projected (one-time purchase, windfall, refund, irregular spike) |

**Amount per projected occurrence.**

- Fixed-amount patterns (all historical amounts equal): that amount.
- Variable patterns: **mean of the occurrences inside the recent window** (*to calibrate*: the
  request_19 back-solve in [ARCHITECTURE.md](ARCHITECTURE.md) §22 matched a 3-month mean to
  within 0.1%; alternatives to test are the median and the maximum, which is the most
  conservative).

**Stable-salary rescue.** The generic cadence test drops two real payroll shapes: a missed month
(unpaid leave, e.g. March, April, then July, a 91-day gap) and a new job with only two payslips.
Dropping them leaves the user with **no income at all** for 90 days, which produced impossible
negative balances (samples 08, 14, 15 projected the account below zero). A salary is therefore
also projected when every occurrence has the same home-currency amount on the same day-of-month,
category `salary`, at least two occurrences, and no income-ending keyword
(`stable_salary_rescue`, `_stable_salary_pattern`). Measured effect across all 275 requests:
requests with zero projected income 77 → 55, self-breaching base paths 43 → 22; on the samples,
earliest date 18 → 19 and safe amount within 10% 14 → 15, with status and method unchanged.

**Recurring income.** Payroll-like patterns (`event_type = income`, category `salary`, monthly
cadence, ≥ 3 occurrences) are projected like expenses. Evidence: sample user_19 has no scheduled
"Next confirmed salary" row, yet the solved answer's earliest date is the salary day, so
projection is required. Rules:

- If a scheduled "Next confirmed salary" row exists, it is used for its date and the projected
  amount for later months is that row's amount (*to calibrate*).
- `income_change` replaces the amount from `effective_from`; `income_end` stops projection from
  `effective_from`.
- Gig-style income (`Weekly app earnings`, platform payouts) is projected only if it meets the
  cadence rule above; a message saying a payout is pending never adds income.

**Never projected.** `investment_purchase`, `investment_sale`, `refund`, `windfall`, anything with
fewer occurrences than the threshold, and any row excluded by reconciliation.

## 4. The balance path

For each day `t` from 0 to 90:

```
B(t) = B0
     − Σ reserved debits with date ≤ t
     + Σ confirmed income with date ≤ t
     + Σ projected recurring income with date ≤ t
     − Σ projected recurring expenses with date ≤ t
```

Conventions:

- All amounts are home-currency `Decimal`. No rounding inside the walk.
- Obligations dated on day 0 are paid before the request (the request's own payment is applied
  after them).
- A projected occurrence that would fall on the same date as a matching reserved/scheduled row of
  the same pattern is **not** double-counted: the reserved row wins and the projection skips that
  occurrence (*to calibrate* how the ground truth treats the scheduled salary vs projection).
- The path is computed once per request without any request payment. Plans are overlaid on it.

## 5. Safety predicate

```
safe(plan) ⇔ for every day t in 0..90:  B(t) − Σ plan payments with date ≤ t ≥ M
```

Equality is safe. One unit below is unsafe.

## 6. Derived quantities (no spending changes)

Because a payment on day `d` lowers every later day by the same amount:

- `headroom(d) = min over t ≥ d of B(t) − M`
- `amount_safe_to_pay = clamp(headroom(0), 0, requested_amount)`
- `earliest_date_for_full_payment = the smallest d in 0..90 with headroom(d) ≥ requested_amount`,
  empty when no such day exists.

Both are computed on the base path. Spending changes are evaluated separately and never alter these
two numbers ([payment_plan_rules.md](payment_plan_rules.md) §5).

## 7. Paths with spending changes

A spending change produces a **modified** path used only to test plans that need it:

- `stop:<event_id>`: every projected occurrence of that pattern inside the horizon is removed.
- `reduce_to:<event_id>:<new_amount>`: every projected occurrence is set to `new_amount`, which must
  be ≥ the pattern's `minimum_allowed_amount`.

Changes apply to projected occurrences only. Reserved (already scheduled) rows are commitments and
are not touched.

## 8. Output formatting of forecast numbers

- `amount_safe_to_pay`: the `Decimal` printed without trailing zeros (`603.3`, `17229139.2`,
  `25256`), matching the samples.
- Dates: `YYYY-MM-DD`.

## 9. Worked check (sample request_19)

Balance 199,545, minimum 92,800, request 39,660 on 2024-09-04. History shows monthly rent 36,100
on the 4th, loan 11,850 on the 13th, subscription 395 on the 14th, family support 12,650 on the
15th, variable utilities / healthcare / shopping, weekly groceries, biweekly transport, and salary
131,000 on the 15th. Obligations between day 0 and the salary on 09-15 total ≈ 77,925, so
`headroom(0) = 199,545 − 77,925 − 92,800 = 28,820` = the sample's safe amount, and the first day
with headroom ≥ 39,660 is 2024-09-15 = the sample's earliest date.

## 10. Test cases the implementation must pass

monthly pattern with 28/30/31-day jitter detected; weekly and biweekly detected; three settled
rows of different amounts and irregular gaps not projected; pending debit reserved on its date;
past-dated pending debit reserved on day 0; pending credit ignored; failed and cancelled ignored;
unrealized ignored; salary projected when history supports it and suppressed after `income_end`;
`income_change` applies from its effective date only; boundary: balance exactly at `M` is safe;
closed-form `headroom` equals brute force on random paths; the request_19 worked check within the
calibrated tolerance.

---

## 11. Calibration record (2026-09-13)

Settings frozen after comparing the deterministic pipeline with the 25 solved samples
(`code/buyorwait/finance/recurrence.py`, `CALIB`):

| Knob | Value | Reason |
|---|---|---|
| `income_mode` | `fixed_only` | Only monthly payroll patterns with a stable level are projected: all amounts equal, or a step change whose last two amounts are equal (the new level is projected, e.g. a reduced payroll), or relative std-dev ≤ 10% (median). Variable weekly platform payouts (user_10), twice-monthly freelance invoices, and variable secondary income (user_13) are not "confirmed salary"; projecting them contradicted the samples. The step-change rule was added after a review found 94 of 275 users left with no income at all; it brought that to 77 (freelance/gig/ended) and improved sample earliest dates to 18/25. |
| `final_keyword_ends_income` | `True` | user_05's last payroll row is labelled "Final employer payroll"; the sample treats income as ended. |
| scheduled salary projection | on | A scheduled "Next confirmed salary" row is projected forward monthly when history shows no fixed pattern (user_01: prorated first salary then a confirmed amount; the sample needs three salaries). |
| `amount_stat` | `median` | Best on every measure among median, mean of the recent 90 days, mean of all history, last occurrence and max. Re-tested 2026-09-13 with the full pipeline: status and method 20/25 for median and both means, but earliest date 19 (median) vs 18 (either mean), safe within 5% 11 vs 9 (mean-recent), within 10% 14 vs 13 (mean-all), median relative error 8.5% vs 9.4-9.6%. Neither mean changes any status; they are simply slightly less accurate. `last` and `max` are worse still. |
| `include_submonthly` | `True` | Weekly/biweekly essentials are projected; excluding them improved a few safe amounts but lost earliest dates. |
| safety check start | first payment date | A payment only affects days on or after its date, so a projected breach before the first payment is not attributed to the plan. |

Result on the samples (no model calls): status 20/25, method 22/25, both 20/25, earliest date
17/25, spending changes 22/25, safe amount within 0.5% 5/25 and within 10% 13/25.

The exact convention the organizers used for variable spending could not be recovered from the
samples (subset-sum reconstruction of several evidence-free cases produced only coincidental
matches). Safe amounts therefore carry a systematic deviation of roughly 5–30% on some users
while the categorical fields agree in most cases. See `../VALIDATION.md` Part 3.

## 12. Convention review against the specification (2026-09-13)

Each rule was re-read against `problem_statement.md` ("90-Day Safety Check") and `AGENTS.md` §6.3,
implemented as a switchable knob, and scored on the 25 samples with the full deterministic
pipeline (48 combinations). Outcome:

| Point reviewed | Change | Kept? | Sample effect |
|---|---|---|---|
| Salary with a step change or small variation was discarded (94 users with no income) | `_salary_level`: latest level after a step change; median when CV ≤ 10% | yes | earliest 17→18, safe within 10% 13→14 |
| Safety judged from the first payment date, spec says the balance must never fall below the minimum | `SAFETY_FROM_DAY_ZERO = True` (whole horizon) | yes (spec-literal) | none on samples |
| Category-level de-duplication suppressed real projections next to unrelated reserved rows | match on category **and** normalized description (salary exempt) | yes | none on samples |
| Evidence income overrides in a foreign currency dropped when no rate exists on the projected date | latest rate on or before the date, future flows only | yes | none on samples |
| Blank-amount history rows (safer estimates) inflated pattern amounts | excluded from the amount statistic | yes | none on samples |
| "Forecast essential variable spending conservatively" read as: do not project discretionary variable spending | `project_discretionary_variable=False` | **no** | both 20→19, earliest 18→15, safe within 10% 14→11 |
| Weekly/biweekly patterns as one monthly lump | `submonthly_as_monthly=True` | **no** | earliest 18→15, safe within 10% 14→7 |
| Maximum instead of median for variable amounts | `amount_stat=max` | **no** | both 20→17, earliest 18→14 |
| Monthly anchor day = most common day of the last four occurrences, so a moved payroll (user_07: 15th → 23rd, announced by message and visible in history) was still projected on the 15th | `monthly_anchor=last` (newest record wins, per the conflict rules) | yes | earliest 18→19 (request_07 now exact), nothing lost |
| Most conservative estimates: variable spending at its historical **maximum** and a wobbling salary at its **minimum** | `amount_stat=max`, `salary_wobble_stat=min` | **no** | status and method 20→17, earliest 19→15, safe within 10% 14→11; three users (04, 22, 23) flip to `not_affordable` where the sample says otherwise. Max spending overshoots the organizers' figures, which are already below ours. |
| Round the variable-spending estimate toward a "nicer" number, on the theory that the generator held round targets | rounding to the nearest half-magnitude, 2 significant digits, or 1 significant digit | **no** | the generator's observable fixed targets are not round (18% carry cents, e.g. 1821.60, 235.40, 306.90), and rounding scored the same or worse: 2 significant digits gave status+method 20 (unchanged), earliest 18 vs 19, median error 8.2% vs 8.5%; coarser rounding was clearly worse |
| Generic-interval cadence: project any consistently spaced series, not just weekly/biweekly/monthly. The dataset really does contain 10-day grocery and 21-day dining/transport rhythms that the named buckets drop, affecting 158 of 275 requests | `generic_interval` (implemented, `_interval_days`, `INTERVAL`) | **no** | at description level it fragments a category into rotating shop names (status+method 20→16); restricted to category level it is still worse (20→18, safe within 10% 15→13). The organizers' figures are apparently *lower* than ours already, so adding the missing categories moves the wrong way. Kept off; the code stays for hidden-set experimentation. |
| Uniform expense scale 0.85–1.20 as a stand-in for the unknown convention | `expense_scale` | **no** (kept at 1) | none of 08/11/13/21 flips at any scale; only 06 flips at 0.95 at the cost of four safe-amount matches |
| Charge a month of every recurring expense at the start of each 30/31-day block, before that block's income (suggested by the user_21 and user_13 back-solves) | `front_load=block_all` / `block_one` | **no** | block_all: both 20→15, earliest 18→12; block_one: both 20→18, earliest 18→15, safe within 10% 14→8 |

Frozen settings: `income_mode=fixed_only` (with `_salary_level`), `amount_stat=median`,
`include_submonthly=True`, `final_keyword_ends_income=True`, `project_discretionary_variable=True`,
`submonthly_as_monthly=False`, `exclude_estimated_amounts=True`, `SAFETY_FROM_DAY_ZERO=True`.
