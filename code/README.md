# Buy or Wait? — solution package

An AI-assisted financial affordability agent for the HackerRank Orchestrate (September 2026)
challenge. For every request in `dataset/requests.csv` it decides whether the user should pay in
full, pay partially, use a seller installment plan, wait, or not proceed, and writes `output.csv`.

Design principle: **AI understands messy evidence, deterministic code calculates financial
safety, deterministic policy chooses the safest valid plan, and AI explains the resulting
decision.** Two model calls at most per request (evidence extraction from a message or image, and
the explanation sentence). Every financial number and every decision field is computed by
deterministic, unit-tested code and re-validated before it is written.

## Layout

```
code/
  main.py                    entry point (CLI)
  README.md                  this file
  requirements.txt           -> ../requirements.txt (repo root)
  buyorwait/
    config.py                paths, env loading, model client factory
    data/                    stage 1-3a: typed models, loader + validation, FX, request context
    agents/                  stage 3b + 10: EvidenceAgent, ExplanationAgent, schemas, prompts/
    finance/                 stage 4-8: reconciliation, recurrence, state, forecast, plans, spending changes
    policy/                  stage 9 + 11: ranking/decision, consistency validator + fallback
    infra/                   formatting, JSON cache, usage ledger + report, CSV writer
    pipeline.py              per-request orchestration, error isolation, retries, bounded concurrency
  evaluation/
    main.py                  development evaluation on dataset/sample_requests.csv
    usage_report.md          generated from the final full run (required deliverable)
    regenerate_usage_report.py  rebuild usage_report.md from a run's calls.jsonl (e.g. after setting prices)
  tests/                     pytest suite (no network)
```

Design documents: `../docs/ARCHITECTURE.md`, `../docs/flow.md`, `../docs/event_reconciliation_rules.md`,
`../docs/forecast_rules.md`, `../docs/payment_plan_rules.md`. Validation record: `../VALIDATION.md`.

## Setup

Python 3.10+ (developed on 3.12).

```
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

Secrets and model settings are read from the environment or a `.env` file in the repository root
(see `.env.example`). Never commit `.env`.

```
OPENAI_API_KEY=...                   required for model stages
OPENAI_MODEL=gpt-5.6                 default model (ExplanationAgent); must support strict JSON schema output
EVIDENCE_MODEL=gpt-5.6-terra         model for the EvidenceAgent (vision + strict JSON); defaults to OPENAI_MODEL
EVIDENCE_REASONING_EFFORT=medium     reasoning effort sent with every EvidenceAgent request (low|medium|high)
EXPLANATION_REASONING_EFFORT=        optional reasoning effort for the ExplanationAgent
OPENAI_PRICE_INPUT_PER_1M=...        optional, USD per 1M input tokens, enables cost estimates
OPENAI_PRICE_OUTPUT_PER_1M=...       optional, USD per 1M output tokens
```

Each agent gets its own PicoAgents `OpenAIChatCompletionClient`. Because PicoAgents' `Agent`
does not forward per-call parameters, `buyorwait/config.py` wraps the client so the configured
`reasoning_effort` is injected into every chat-completion request for that agent.

## Run

From the repository root:

```
python code/main.py                       # full run -> ./output.csv and code/evaluation/usage_report.md
python code/main.py --no-llm              # deterministic only, template explanations, no API key needed
python code/main.py --sample              # run the 25 solved samples -> code/evaluation/sample_output.csv
python code/main.py --limit 20            # first N requests
python code/main.py --clear-cache         # discard cached model outputs first (used for the final run)
python code/main.py --concurrency 4       # bound parallel model calls (default 8)
python code/main.py --evidence-batch 1 --explanation-batch 1   # disable batching (default 4 and 8 per call)
```

Model outputs are cached under `code/.cache/` keyed by prompt version, schema version, model,
and the exact input (including image bytes). Each run writes a call ledger to
`code/.runs/<timestamp>/calls.jsonl` and per-request stage notes to `requests.json`.

## Evaluate against the samples

```
python code/evaluation/main.py            # with model stages (uses the cache)
python code/evaluation/main.py --no-llm   # deterministic only
```

Prints per-field exact-match counts and writes `code/evaluation/sample_evaluation.md` with a
side-by-side diff for every miss.

## Tests

```
python -m pytest code/tests -q
```

77 tests, no network: loader and validation, FX and context, evidence schema and validation
(with a stubbed model client), reconciliation, recurrence, forecast closed forms vs brute force,
plans and gates, policy and spending changes, consistency validator and repair ladder, pipeline
isolation and ordering, batching with per-item fallback.

## How a request is processed

1. Load and validate all CSVs (typed rows, enums, foreign keys, image files).
2. Build the request context: profile, events converted to home currency on their settlement
   date, seller options, messages sent on or before the request, images, candidate events.
3. EvidenceAgent (only if a message or image exists): returns schema-enforced facts about known
   event ids; code re-validates every item and discards anything but high-confidence, well-formed
   facts. Evidence text is wrapped as untrusted data; instructions in it are never followed.
4. Reconciliation: classify rows (history / confirmed / reserved / excluded), apply evidence edits
   in the spec's precedence, fill blank amounts (never zero), tag provenance.
5. Financial state: reserved debits, confirmed and projected income, recurring expense patterns,
   flexible catalogue.
6. 90-day forecast: daily balance path; `amount_safe_to_pay` and `earliest_date_for_full_payment`
   in closed form, without spending changes.
7. Plans: full now, wait, partial, each installment option; spending-change variants only for
   plans that fail solely on safety.
8. Gates: user preference, request/limit, deadline, horizon, safety.
9. Policy: the spec's six-key ranking, mapped to status/method/plan/changes.
10. ExplanationAgent writes the sentence from a fact sheet; a template is used when the model is
    off, fails, or cites a number that is not a fact.
11. Consistency validator re-checks every constraint and repairs or falls back. Output order equals
    input order.

## Usage report

`code/evaluation/usage_report.md` is generated from the actual call ledger of the final run. Cost
figures appear only when the price environment variables are set; otherwise the report states that
no price was configured rather than assuming one. To rebuild the report after setting prices:

```
python code/evaluation/regenerate_usage_report.py code/.runs/<timestamp>
```

## Determinism

All financial calculations use `Decimal`, fixed dated exchange rates, and documented thresholds
(`docs/forecast_rules.md`). With `--no-llm` the run is fully deterministic. With model stages on,
cached outputs make re-runs reproducible; the model's only influence is through validated evidence
items and the explanation text.
