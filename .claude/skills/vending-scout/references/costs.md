# What a run costs (estimates, 2026-11)

Check the live pricing pages before relying on these numbers.

| Service | 10-site batch uses | Price | Typical cost |
|---|---|---|---|
| Claude (Opus 5.5) | 10 site researches, ~3–5 food-hour checks, ~6 contact searches + drafts | $4 / $20 per million input/output tokens; web search $10 per 1,000 | **$5–$12** |
| Google Places | ~14 queries × ≤3 Text Search pages, ~20 details, ~5–40 Nearby calls | Monthly free usage per SKU (about 10,000 Essentials / 5,000 Pro / 1,000 Enterprise) | **$0** within free caps; billing account required |
| TypeSafe Jev | ~20–40 excerpts × ~300 tokens | ~$0.042 per million input tokens, output free (third-party figure) | **< $0.01** |

Suggested limit: **$15 per run** for a 10-site batch (the first build guessed $10 for a lighter flow). The script guards each
paid call with an estimate (research $0.60, hours $0.40, contact $0.25, draft $0.05) and stops before the limit.

Sources: https://platform.claude.com/docs/en/about-claude/pricing ·
https://developers.google.com/maps/billing-and-pricing/pricing ·
https://github.com/danieluszta/jev-classification-guide (Jev price, unofficial)
