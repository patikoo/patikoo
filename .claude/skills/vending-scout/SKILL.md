---
name: vending-scout
description: >
  Research vending machine locations (workplaces with night shifts and no food open at night) within
  a set distance of the user's town or address. Uses Google Maps/Places to find sites, Claude to
  research and write, and TypeSafe Jev to label job ads, then scores each shift and writes a linked
  site report with Google Maps links into the "Vending Scout" tracker folder. Use when the user asks
  to find, update or report vending locations or leads.
---

# Vending Scout

Everything runs through `scripts/scout.py` (Python 3.10+, run from the folder that holds `Vending Scout/`).
Install the official SDKs once: `pip install anthropic typesafe-sdk`.

## 1. Ask only for what is missing

Run `python3 .claude/skills/vending-scout/scripts/scout.py access`. Ask the user **only** for the items it lists:

- **Location**: the town or address to center on. **Distance**: how far to search. If the user gives none, use **20 miles**.
  If they give km, keep km (`--unit km`).
- **Spending limit** in USD per run. If the user doesn't know, suggest $10 (see `references/costs.md`) and ask them to confirm.
- **Missing access**: API keys (`ANTHROPIC_API_KEY`, `GOOGLE_MAPS_API_KEY`, `TYPESAFE_API_KEY`). Tell the user to put them in
  the secrets file the script names (`~/.config/vending-scout/secrets.env`, mode 600). Never ask them to paste a key into chat,
  never print a key, and never write one into the tracker, a report, a manifest or git.

Time zone: use the user's local IANA zone (set once with `--timezone`; all timestamps are written in it).

```
python3 .claude/skills/vending-scout/scripts/scout.py init --center "<town or address>" --radius 20 --unit mi --timezone <Area/City> --budget <usd>
```

## 2. Check integrations before any real run

`scout.py check` makes one Places lookup, one Jev request and one Claude call, and records each as **live**, **docs-only**
(no key or host blocked; the request is built from the official docs) or **mock** (offline fixture) in
`integration_checks` and in `Vending Scout/manifests/run-NNN.json`. If anything is missing, **report the missing access to
the user and stop**. Do not start `run` with any check in docs-only mode.

## 3. Run

`scout.py run --max-sites 15` does, in order, stopping before the spending limit is passed:

1. Center point from Places Text Search (lat/lng cached at most 30 days).
2. Candidate workplaces: Text Search per query in `config.json → site_queries`, filtered to the radius. **Only place IDs are stored.**
3. Per site: live Place Details (not stored) gives Claude a hint; Claude (web search + web fetch) reads the business's **own
   website** for name/address (`sites.source_url`), finds job ads, shift times, a sourced night headcount, canteen and vending facts.
   `match_status` compares website name with the live Maps name.
4. Each job ad is labelled by Jev (`scripts/jev.py`, official `typesafe-sdk`): arrangement, work_type, shift, confidence,
   review_reason. Thresholds and decisions are in code; all question wording is in `QUESTIONS`.
5. Each distinct schedule becomes its own `shift_id` (an ad with day and night rotas gives two shifts).
6. Food venues within `food_radius_m` come from Nearby Search; their Google hours are used live for scoring and **not stored**.
7. `checks` and `results` are keyed by `(site_id, job_id, shift_id)`. Score = night headcount × dead hours × cafeteria factor ×
   vending factor (`scripts/scoring.py`). No verified headcount → no score (`needs_headcount`).
8. Claude drafts outreach for scored sites with a published contact. Drafts are never sent.

Then `scout.py report` writes `Vending Scout/report/sites.html` (linked: site → job → shift, Google Maps links) and CSVs in
`Vending Scout/tables/`. `scout.py verify` re-checks the joins. `scout.py where` prints every location.

## Data rules

- Permanent IDs (`site-001`, `job-001`, `shift-001`, `food-001`, `out-001`, `run-001`, `chk-001`) come from `id_counters` and are never reused.
- Unknown values stay blank; the reason goes in that row's `notes`. Never estimate headcounts.
- Google Maps: store place IDs only; fetch other details live; show Google Maps attribution; see `references/google-places.md`.
- Every table has `notes`. Schema: `scripts/schema.sql`.

## References

- `references/google-places.md` — endpoints, field masks, storage and attribution rules, sources.
- `references/typesafe-jev.md` — SDK usage, questions, decision rules; the official skill is in `.claude/skills/typesafe-ai/`.
- `references/claude.md` — model, tools, structured outputs, fallbacks.
- `references/costs.md` — what a run costs and how to pick a spending limit.
