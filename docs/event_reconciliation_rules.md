# Event Reconciliation Rules

Stage 4 of the pipeline ([flow.md](flow.md)). Input: the `RequestContext` (typed events in home
currency) and the `ValidatedEvidence` for each message and image. Output: one reconciled event list
in which every row has a cash classification and a provenance tag.

Reconciliation decides **which numbers to believe**. It does not forecast, and it is the only stage
where evidence from the model changes a number.

Sources of authority, in order: `problem_statement.md` (conflict rules, what to ignore),
`AGENTS.md` §6.3, `CLAUDE.md` §6–§8, and the measured dataset facts in
[ARCHITECTURE.md](ARCHITECTURE.md) §2.2.

---

## 1. Base classification by status

Applied to every event of the user with effective date ≤ horizon end. The effective date is
`settlement_date`, or `event_date` when settlement is blank.

| Row | Class | Cash effect in the forecast |
|---|---|---|
| `settled`, dated before the request | **history** | none directly; feeds recurrence detection |
| `settled`, dated on/after the request | **confirmed** | counted on its effective date (none exist in the supplied data; kept for hidden inputs) |
| `pending` or `scheduled`, direction `debit` | **reserved** | subtracted on its effective date; if that date is already past, on day 0 |
| `scheduled`, direction `credit` (the "Next confirmed salary" rows) | **confirmed** | added on its effective date |
| `pending`, direction `credit` (refunds in flight) | **excluded** | never counted until a settlement is evidenced |
| `failed`, `cancelled` | **excluded** | never counted |
| `unrealized` / `non_cash` | **excluded** | never cash |

Rules from the spec enforced here: reserve pending debits; do not count pending credits, bonuses,
commissions, refunds, lottery proceeds, or investment gains until they settle; do not treat
unrealized value as cash.

## 2. Linked events

`linked_event_id` is informational. It never adds or removes cash by itself.

- Settled refund → settled expense: both are real rows already in history. No adjustment.
- Settled expense → cancelled expense (a re-attempt): the cancelled row is excluded by §1; the
  settled row counts once.
- Unrealized valuation → settled investment purchase: the purchase is history, the valuation is
  excluded by §1.

## 3. Blank amounts

A blank `amount` is **never zero**. Resolution order:

1. A usable `amount_extraction` item (high confidence, currency equal to the event's currency,
   target is this event) fills the amount. The row then follows §1 by its status.
2. Otherwise the row is marked `unresolved` and treated the financially safer way:
   - a debit is estimated as the **largest** settled amount in the same category for the same user
     within the history window (if none exists, the largest settled debit of that user);
   - a credit is treated as **zero** (not counted).
   The estimate is flagged so the explanation can say the amount was not visible.

## 4. Applying evidence items

Each `ValidatedItem` with `usable == True` is applied as one concrete edit. Items are applied in
this precedence (the spec's conflict order), and within the same precedence in message `sent_at`
order (newest last, so newest wins):

1. **Explicit cancellation / settlement / amendment** of a named event.
2. **Newer record from the same source** (a later message from the same `source_type` about the
   same event overrides an earlier one).
3. **Settled event over an estimate**: a `settled` row is never overwritten by an item that would
   make it an estimate; only `duplicate` can exclude a settled row.
4. **Financially safer interpretation** when nothing above resolves it.

| Kind | Requires | Edit |
|---|---|---|
| `cancellation` | target | class → excluded |
| `amendment` | target, `new_amount` and/or `new_date` | replace amount (in the event's currency, then re-convert) and/or effective date |
| `settlement` | target | reserved debit → confirmed on `new_date` (or its recorded date); pending credit → confirmed on `new_date` |
| `delay` | target, `new_date` | effective date → `new_date`; class unchanged |
| `confirmation` | target | no edit; row tagged confirmed-by-evidence (wins ties in rule 2) |
| `duplicate` | target | class → excluded for the named row only |
| `pending_credit` | optional target | class stays excluded (no-op; recorded for the explanation) |
| `non_cash` | optional target | class stays excluded (no-op) |
| `income_change` | `new_amount` and/or `new_date`, `effective_from` (defaults to `new_date`) | amount: projected recurring income = `new_amount` for pay dates ≥ `effective_from`, converted to home currency on each projected date. Date only (`new_date` without `new_amount`, e.g. "salary now expected on 2024-09-23"): projected pay dates on/after `effective_from` move to that day-of-month. |
| `income_end` | `effective_from` | no recurring income projected on/after `effective_from`; scheduled salary rows dated ≥ `effective_from` → excluded |
| `amount_extraction` | target with blank amount, `new_amount`, `currency` | fill the blank (§3) |
| `unresolved` | — | no edit; safer reading (§3 for blanks, otherwise the row keeps its §1 class) |

Items that fail validation never reach this table; they arrive as `unresolved`.

## 5. Conflicts between items

- Two usable items on the same event with different kinds: apply by precedence in §4; if equal
  precedence, the later `sent_at` wins.
- `income_change` and `income_end` from the same source: the later `sent_at` wins.
- An `amendment` and a `cancellation` on the same event: cancellation wins (excluding is safer).
- Any unresolved conflict: safer interpretation. Debit stays counted at the larger amount and
  earlier date; credit is dropped.

## 6. Provenance

Every reconciled row carries:

- `source`: `dataset` or `evidence:<message_id|image_id>`
- `class`: history / confirmed / reserved / excluded
- `edits`: the list of kinds applied, in order
- `flags`: `estimated_amount`, `unresolved_evidence`, `fx_missing`

The forecast reads only `class`, amount, and date. The explanation and the tests read the rest.

## 7. What reconciliation does not do

- It does not detect recurrence (stage 5).
- It does not decide affordability (stages 6–9).
- It does not read messages or images itself; it only applies validated items.
- It never invents an event, an amount, or a date that no row or usable item supplies.

## 8. Test cases the implementation must pass

cancellation of a reserved debit; amendment of amount; amendment of date; settlement of a pending
credit; delay of a scheduled debit; duplicate exclusion of one row of a pair; income change with an
effective date in the middle of the horizon; income end; blank amount filled from a high-confidence
extraction; blank amount with medium-confidence extraction → safer estimate; blank credit → zero;
conflicting amendment vs cancellation; two messages from the same source, newer wins; a `settled`
row that an item tries to downgrade is left as settled; every edit shows the right provenance.
