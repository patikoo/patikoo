# TypeSafe Jev notes

- Official skill: `typesafe-ai/skills` (MIT), copied unchanged to `.claude/skills/typesafe-ai/` at commit
  `302eea662c54bca6730873fe245bec1d69ebc23d` (plugin v0.6.2). In Claude Code you can also install it with
  `claude plugin marketplace add typesafe-ai/skills` then `claude plugin install typesafe@typesafe-ai`.
- Official SDK: `typesafe-sdk` 0.7.5 on PyPI (`pip install typesafe-sdk`). API surface was read from the installed package
  because `docs.typesafe.ai` and `api.typesafe.ai` were blocked by the build session's network policy:
  - `TypeSafeClient(api_key=...)` (or `TYPESAFE_API_KEY` env), default base URL `https://api.typesafe.ai`, default model `jev-latest`.
  - `client.system_one(state=..., questions={name: Noul|Choice|Score|Box})` → `POST /v1/systemone`.
  - Response: `.nouls[name].noul` (probability of yes), `.choices[name].choice/.confidence/.probabilities`, `.usage`, `.model`.
  - Errors are `TypeSafeError` subclasses; a failed call is an error, never an answer.
- Docs: https://docs.typesafe.ai/go/python-sdk · HTTP API https://docs.typesafe.ai/go/api · OpenAPI https://api.typesafe.ai/openapi.json

## How Vending Scout uses it (`scripts/jev.py`)

State: `{"ad": {"text": <≤300 char excerpt>, "employer": ..., "url": ...}}`. Six questions in one request (shared state):
`is_job_ad`, `mentions_schedule`, `night_work` (Nouls) and `arrangement`, `work_type`, `shift` (Choices). `mentions_schedule`
is the null-case Noul for `shift`.

Decision table in code: Noul ≥ 0.7 = yes, ≤ 0.3 = no, between = `review_reason`. Choice top probability < 0.6 = review.
`is_job_ad` ≤ 0.3 → `ad_status = not_job_ad`. `confidence` = weakest judgment used. Shift times are extracted by Claude,
not Jev (Jev answers typed questions; it does not extract text).
