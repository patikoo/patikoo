# TypeSafe Jev notes

- Official skill: `typesafe-ai/skills` (MIT), copied unchanged to `.claude/skills/typesafe-ai/` at commit
  `302eea662c54bca6730873fe245bec1d69ebc23d` (plugin v0.6.2). Claude Code install alternative:
  `claude plugin marketplace add typesafe-ai/skills` then `claude plugin install typesafe@typesafe-ai`.
- Official SDK: `typesafe-sdk` 0.7.5 (`pip install typesafe-sdk`). Read from the installed package because `docs.typesafe.ai`
  and `api.typesafe.ai` were blocked in the build session:
  `TypeSafeClient(api_key=...)` (or `TYPESAFE_API_KEY`), base `https://api.typesafe.ai`, model `jev-latest`;
  `client.system_one(state=..., questions={name: Choice(...)})` → `POST /v1/systemone`;
  `resp.choices[name].choice / .confidence / .probabilities`. Errors are `TypeSafeError` subclasses.
- Docs: https://docs.typesafe.ai/go/python-sdk · https://docs.typesafe.ai/go/choice · OpenAPI https://api.typesafe.ai/openapi.json

## Questions (`scripts/jev.py`, one request per excerpt, state `{"job": {"text": excerpt}}`)

| Question | Options |
|---|---|
| arrangement | onsite · remote · hybrid · unknown |
| work_type | physical · desk · mixed · unknown |
| shift | day · evening (late work ending before midnight) · overnight (spans midnight) · rotating (explicitly changing shifts) · unknown |

Missing or contradictory evidence = `unknown`. The user asked for an explicit `unknown` option, so it replaces the
companion-Noul pattern the official skill suggests for the null case.

## Decision code

Chosen value = top option; `label_distributions` stores all probabilities. `confidence` = lowest top probability.
Review when: any label is `unknown`; top < 0.6 or margin < 0.15; the ad is stale; the ad-to-site match is ambiguous;
`overnight` with source times that do not span midnight, or source times spanning midnight labelled day/evening;
remote + physical. A failed call sets `label_error` and review. Labels never write shift times.
Unchanged excerpt + question version → labels reused; review reasons are recomputed from stored distributions.
