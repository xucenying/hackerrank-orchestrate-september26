# Usage Report

Run: regenerated from 20260912T235233Z  
Generated: 2026-09-13T01:14:47+00:00  
Requests processed: 250

## Per-model totals

| Provider | Model | Agents | Model calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---|---|---|---|---|---|---|
| openai | gpt-5.6-luna | evidence_agent, explanation_agent | 459 | 1,186,104 | 83,946 | 1,270,050 | 0.3380 |

## Overall

- Model calls: 459
- Input tokens: 1,186,104
- Output tokens: 83,946
- Total tokens: 1,270,050
- Average calls per request: 1.836
- Average tokens per request: 5080.2
- Estimated total cost: USD 0.3380
- Estimated cost per request: USD 0.001352
- Pricing source: USD 0.2 in / 1.2 out per 1M tokens; OpenAI list price for gpt-5.6-luna, September 2026 (openrouter.ai/openai/gpt-5.6-luna; layer3labs.io/guides/gpt-5-6-pricing)

## Reliability

- Cached model outputs reused: 0
- Failed model calls (after retries): 0
- Retries performed: 0
- Requests that used a fallback: 0
- Requests with validator repairs: 0

Notes: token counts come from the provider's usage fields via PicoAgents `Usage`. Requests
without messages or images make no evidence call. Cost is computed only when
`OPENAI_PRICE_INPUT_PER_1M` and `OPENAI_PRICE_OUTPUT_PER_1M` are set; no price is assumed.
No API keys or credentials are included.
