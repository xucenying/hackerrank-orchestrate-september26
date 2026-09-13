# Payment Plan Rules

Stages 7, 8, and 9 of the pipeline ([flow.md](flow.md)): generating every candidate plan,
evaluating each against the gates, and choosing the winner with the spec's ranking. Also the
spending-change search and the mapping to the output columns.

Authority: `problem_statement.md` ("Allowed values", "Choosing Between Safe Plans"),
`AGENTS.md` §6.2–§6.3, `CLAUDE.md` §12–§17. Inputs: the base balance path and its derived
`safe` and `earliest` values ([forecast_rules.md](forecast_rules.md)), the profile, the request,
and the seller options.

---

## 1. Vocabulary

| Term | Meaning |
|---|---|
| `safe` | `amount_safe_to_pay` from the base path |
| `earliest` | `earliest_date_for_full_payment` from the base path, or none |
| `deadline` | `desired_completion_date` |
| plan | an ordered list of `(date, amount)` payments plus a method label, an optional option id, and a list of spending changes |
| eligible | passes every gate in §3 |

## 2. Candidate generation (stage 7)

All candidates are generated; none is judged here.

| Plan | Payments | Total paid | Source |
|---|---|---|---|
| `full_now` | `request_date : requested_amount` | requested | the request's `full_payment` option row |
| `wait` | `earliest : requested_amount` | requested | constructed; only if `earliest` exists |
| `partial` | `request_date : safe` then `earliest : requested − safe` | requested | constructed; only if `0 < safe < requested` and `earliest` exists |
| `installments(option)` | `first_payment_date + k × frequency_days : payment_amount` for `k = 0..n−1` | `total_payable_amount` | one per `installments` option row, amounts copied exactly |

Spending-change variants are generated in a second pass (§5) only for base plans that fail the
safety gate.

## 3. Gates (stage 8), applied in this order

A candidate stops at the first failing gate and records the reason.

1. **Preference.** The method must appear in `payment_methods_user_will_consider`:
   `full_now` and `wait` need `full_payment`; `partial` needs `partial_payment`; installments need
   `installments`.
2. **Request and limit gates.**
   - `partial`: the request has `allows_partial_payment = true`.
   - installments: the option's duration fits `max_installment_months`. Default rule:
     `ceil(n × frequency_days / 30) ≤ max_installment_months` (*to calibrate* against sample
     request_12, which accepted 3 payments 31 days apart).
3. **Deadline.** The last payment date is on or before `deadline`. For `partial` this means
   `earliest ≤ deadline`.
4. **Horizon.** Every payment date is within the 90-day horizon.
5. **Safety.** With the plan's payments overlaid on the path (base path, or the modified path for
   change variants), the balance never drops below `minimum_balance_to_keep`.

Installment sanity checks (a failure is a data error, logged, and the option is rejected):
`n × payment_amount = total_payable_amount` and `total_payable_amount = requested + financing_fee`
within 0.01; dates strictly increasing.

## 4. Ranking (stage 9)

Among eligible plans, sort by this tuple, ascending:

1. completes by deadline: `False` sorts after `True` (all eligible plans satisfy this by §3.3; kept for the validator)
2. number of spending changes (0 first)
3. total amount paid
4. first payment date
5. number of payments
6. `payment_option_id` numeric suffix; constructed plans sort before any option

The first plan wins. If no plan is eligible, the result is `not_affordable`.

## 5. Spending changes

Only tried for a base plan that failed **only** the safety gate. Search space: the flexible
catalogue from the financial state, filtered to what the user permits:

| Action | Allowed when | Effect on the path |
|---|---|---|
| `stop:<event_id>` | pattern `flexibility` allows stop **and** category ∈ `expense_categories_user_is_willing_to_stop` **and** category ∉ `expense_categories_to_protect` | every projected occurrence removed |
| `reduce_to:<event_id>:<amount>` | pattern allows reduce **and** category ∈ `..._willing_to_reduce` **and** not protected | every projected occurrence set to `amount` = the pattern's `minimum_allowed_amount` |

Search order, first success wins:

1. single changes, stops before reductions, larger monthly saving first;
2. pairs of changes, same ordering, never the same event twice;
3. triples.

Hard limits: at most 3 changes; `stop` and `reduce_to` never on the same event; `event_id` must be
the most recent settled occurrence of the pattern. `safe` and `earliest` are **not** recomputed
with changes.

Sample evidence: request_06 (`stop:event_476` then full payment today), request_11
(`reduce_to:event_989:665950`), request_21 (one stop plus one reduce).

## 6. Mapping the winner to the output columns

| Winner | `affordability_status` | `recommended_payment_method` | `payment_plan` | `spending_changes_needed` |
|---|---|---|---|---|
| `full_now`, no changes | `affordable_now` | `full_payment` | `date:amount` | `none` |
| `full_now` with changes | `affordable_with_plan` | `full_payment` | `date:amount` | the changes |
| `partial` | `affordable_with_plan` | `partial_payment` | two entries | `none` or changes |
| installments | `affordable_with_plan` | `installments` | option schedule | `none` or changes |
| `wait` | `affordable_later` | `wait` | `earliest:amount` | `none` |
| none | `not_affordable` | `not_recommended` | `none` | `none` |

`amount_safe_to_pay` and `earliest_date_for_full_payment` come from the base path regardless of
the winner. `earliest` equals `request_date` for `affordable_now`; it is left empty when no day in
the horizon has enough headroom (all seven `not_affordable` samples).

Open case (*to calibrate*): `earliest` exists but is after the deadline and nothing else is
eligible. Default: `not_affordable` with `earliest` still reported, since the spec defines
`wait` as eligible only when the full payment "becomes safe later" and the ranking puts
deadline completion first.

## 7. Formatting

- Plan entries: `YYYY-MM-DD:amount`, joined by `|`, chronological.
- Plan amounts: two decimals when fractional (`620.40`, `15952906.67`), none when whole
  (`25256`), matching the samples. Installment amounts are the option's `payment_amount` verbatim.
- `spending_changes_needed`: `stop:event_14|reduce_to:event_21:100`, at most three entries, or `none`.

## 8. Worked checks from the samples

| Sample | Situation | Outcome |
|---|---|---|
| request_19 | safe 28,820 < 39,660; earliest 09-15 ≤ deadline; user accepts partial + installments; request allows partial; option 53 (2 × 20,623.20) also safe | partial wins on total cost (39,660 < 41,246.40) |
| request_12 | safe = requested; user accepts only installments | `affordable_with_plan` / installments; `earliest` = request date |
| request_04 | safe < requested; user accepts only full payment; request allows partial | `wait` on 06-15 (partial blocked by preference) |
| request_15 | user accepts only partial; request disallows partial | no eligible plan → `not_affordable` |
| request_06 | full today unsafe; stopping one streaming subscription makes it safe; earliest 01-15 is after deadline 01-14 | `affordable_with_plan` / `full_payment` with `stop:event_476` |

## 9. Test cases the implementation must pass

each plan type generated with correct dates and totals; installment schedule from a 28-, 30-, and
31-day option; option rejected for preference, for `max_installment_months`, for deadline, for
safety; partial rejected for each of its four conditions individually; `wait` rejected when the
user does not accept full payment; ranking tie-breaks at every level including the option-id
suffix (`payment_option_2` before `payment_option_10`); spending changes: single stop, single
reduce, pair, triple, more than three rejected, protected category rejected, same event twice
rejected, reduce below floor rejected; the five worked checks above reproduce the sample columns.
