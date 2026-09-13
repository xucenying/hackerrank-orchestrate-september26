# Usage Report

Run: full run 20260913T085313Z (llm)  
Generated: 2026-09-13T08:53:13+00:00  
Requests processed: 250

## Per-model totals

| Provider | Model | Agents | Model calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---|---|---|---|---|---|---|
| openai | gpt-5.6-luna | evidence_agent, explanation_agent | 93 | 444,827 | 84,831 | 529,658 | 0.1908 |

## Overall

- Model calls: 93
- Input tokens: 444,827
- Output tokens: 84,831
- Total tokens: 529,658
- Average calls per request: 0.372
- Average tokens per request: 2118.6
- Estimated total cost: USD 0.1908
- Estimated cost per request: USD 0.000763
- Pricing source: USD 0.2 in / 1.2 out per 1M tokens; OpenAI list price for gpt-5.6-luna, September 2026 (openrouter.ai/openai/gpt-5.6-luna; layer3labs.io/guides/gpt-5-6-pricing)

## Reliability

- Cached model outputs reused: 0
- Failed model calls (after retries): 0
- Retries performed: 0
- Batched calls: 82 (covering 448 items)
- Requests that used a fallback: 0
- Requests with validator repairs: 0
- Cache hits / misses this run: 0 / 459

Notes: token counts come from the provider's usage fields via PicoAgents `Usage`. Requests
without messages or images make no evidence call. Cost is computed only when
`OPENAI_PRICE_INPUT_PER_1M` and `OPENAI_PRICE_OUTPUT_PER_1M` are set; no price is assumed.
No API keys or credentials are included.
