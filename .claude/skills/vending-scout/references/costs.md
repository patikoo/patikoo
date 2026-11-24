# What a run costs (estimates, 2026-11)

Check the live pricing pages before relying on these numbers.

| Service | What a 15-site run uses | Price | Typical cost |
|---|---|---|---|
| Claude (Opus 5.5) | ~15 research calls + extraction + a few email drafts; ~6 web searches per site | $4 / $20 per million input/output tokens; web search $10 per 1,000 | **$3–$9** |
| Google Places | ~25 Text Search pages, 15 Place Details, 15 Nearby Searches | Monthly free usage per SKU (about 10,000 Essentials / 5,000 Pro / 1,000 Enterprise calls) | **$0** within free caps; a billing account with a card is still required |
| TypeSafe Jev | ~50 job ads × ~400 tokens | ~$0.042 per million input tokens, output free (third-party figure; confirm on TypeSafe's site) | **< $0.01** |

Suggested spending limit: **$10 per run**. Anthropic API credit is prepaid, so set a monthly limit in the Claude Console too.
The script stops before the limit is passed, using about $0.60 per site as its pre-call estimate.

Sources: https://platform.claude.com/docs/en/about-claude/pricing ·
https://developers.google.com/maps/billing-and-pricing/pricing ·
https://github.com/danieluszta/jev-classification-guide (Jev price, unofficial)
