# Claude notes

- SDK: official `anthropic` Python package (2.13.0 at build time). Client pinned to `https://api.anthropic.com` with the key from
  the secrets file.
- Model: `claude-opus-5-5` (config `claude_model`), adaptive thinking, streaming for the research step.
- Research step: server tools `web_search_20260209` and `web_fetch_20260209` (max 6 uses each), `pause_turn` handled.
- Extraction step: structured outputs (`output_config.format` JSON schema) → fixed fields, nulls with a reason.
- Refusal fallbacks are on: beta `server-side-fallback-2026-07-01` with `fallbacks="default"`; `stop_reason == "refusal"` is
  checked before reading content.
- Cost per call is computed from `usage` (tokens + web searches) and charged against the run's spending limit.

Sources: https://platform.claude.com/docs/en/api/messages ·
https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool ·
https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool ·
https://platform.claude.com/docs/en/build-with-claude/structured-outputs ·
https://platform.claude.com/docs/en/about-claude/pricing
