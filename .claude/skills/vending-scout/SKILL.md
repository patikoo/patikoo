---
name: vending-scout
description: >
  Research vending machine locations (workplaces with overnight shifts and no food open during them)
  within a set distance of the user's town or address. Uses Google Places to find sites and food,
  Claude to research and write, and TypeSafe Jev to label job ads; scores each shift, drafts outreach,
  and keeps everything in the linked "Vending Scout" tracker with a Google Maps-linked site report.
  Use when the user asks to find, update, score, contact or report vending locations or leads, or
  gives visit answers or outreach activity for a site.
---

# Vending Scout

All commands: `python3 .claude/skills/vending-scout/scripts/scout.py <command>`, run from the folder that holds
`Vending Scout/`. Install the official SDKs once: `pip install anthropic typesafe-sdk`.

## 1. Ask only for what is missing

Run `access`. Ask the user only for what it lists:
- **Location**: the town or address to center on. **Distance**: default **20 miles**; keep km if the user gives km.
- **Spending limit** (USD per run). If unsure, suggest $10 (`references/costs.md`) and ask them to confirm.
- **Missing access**: `ANTHROPIC_API_KEY`, `GOOGLE_MAPS_API_KEY`, `TYPESAFE_API_KEY` go in `~/.config/vending-scout/secrets.env`
  (mode 600). Never ask for keys in chat, print them, or write them to the tracker, reports, manifests or git.

Use the user's local IANA time zone for every date and time.
`init --center "<town or address>" --radius 20 --unit mi --timezone <Area/City> --budget <usd>`

## 2. Check, then stop if anything is missing

`check` makes one Places lookup, one Jev request and one Claude call and records **live**, **docs-only** (no key or host
blocked; request built from official docs) or **mock** beside each in `integration_checks` and the run manifest.
`verify` runs the synthetic examples, a full mock pipeline and the join checks. **Report missing access to the user and stop**
before `run` if any integration is not live. `where` shows where everything is saved.

## 3. Run a batch: `run --batch 10`

Steps own their fields and never overwrite each other's. Each run is recorded in `run_manifest` + `manifests/run-NNN.json`
with the exact center (query, place_id, lat/lng, resolve time) and search coverage. The run stops before the spending limit.

1. **discover** (owns sites.place_id, distance_m): Places Text Search per `config.json → site_queries` (warehouses, plants,
   hospitals, care homes, parcel/data centres…), restricted to the circle's bounding box, all pages (≤3), dedup by place_id,
   keep only straight-line distance ≤ radius. Adds up to `batch` new sites as `found`. Discovery is not a census.
2. **research** (owns site identity, job source fields, shifts): Claude verifies name + address on the employer's **own
   website**, opens the **original** ad, marks filled/expired ads `stale`, and copies shift times/days only as stated. Each
   distinct schedule gets a permanent `shift_id`. Employer name or city alone does not join an ad to a building →
   `site_match = ambiguous` (review). A failed source gets one `unavailable` job entry, never an invented result.
   Every job must link to an existing site before the site becomes `researched`; unverified identity → `review`.
3. **label** (owns arrangement, work_type, shift, confidence, review_reason, distributions): Jev answers three independent
   Choice questions over each excerpt (`scripts/jev.py`). `unknown` → review even at high confidence; uncertain labels and
   conflicts (stale ad, ambiguous match, label vs source times) → review. Labels never change shift times. Unchanged
   excerpts reuse labels. Verify N/N excerpts are labelled or have a recorded failure.
4. **food** (owns food, food_searches, results): for verified sites with an active, site-matched ad whose source times span
   midnight with explicit days. Nearby Search ≤ 800 m; a 20-result response is narrowed (per type, then quarter circles) and
   deduped, else flagged incomplete. Google results are used live; hours are stored only from business-owned sources.
5. **score** (owns results): dead hours = shift duration − union of open-food intervals, on real local dates (midnight, DST,
   Dutch holidays, 24-hour venues; `scripts/timecalc.py`). Unknown hours or incomplete coverage → unknown, never 0.
   Unknown walking access → `provisional`. Score = verified_night_headcount × dead_hours (worker-hours per shift), only from
   independently verified headcount in `checks`; otherwise blank with `missing_checks`. Ranks research priority, not earnings.
6. **outreach** (owns outreach, status `draft_ready`): scored sites first, then the unscored verification queue. Public
   facilities contact + source (blank recipient → contact page in notes). One email per site, under 90 words, asking whether
   the overnight team already has food available; cites a shift only if current and site-matched; no sales or revenue
   claims. Saved, never sent.
7. `checklist` writes `Vending Scout/visit-checklist.md`; `report` writes `report/sites.html` + `tables/*.csv`; `queue` prints
   counts for every site, the review queue, scored vs needs-verification, and drafts.

Show the user: counts for all sites in the batch (including unresolved), the review queue, scored sites separately from
those needing verification, draft count with sources, and status changes (`status_log`).

## 4. When the user reports back

- Visit/phone answers → `answer --site S --shift SH --headcount N --source "who, when"` (+ `--cafeteria`, `--vending`,
  `--access-notes`); walking access → `food-access --food F --status accessible|not_accessible`. Both rescore.
- Activity → `status --site S --to contacted|visited|closed --confirm "what they did"`. Only from the user's confirmed
  activity. Closed sites are skipped and never reopened automatically; contacted/visited/closed survive reruns.

## Data rules

Permanent IDs (`site-001`, `job-001`, `shift-001`, `food-001`, `out-001`, `run-001`, `chk-001`) are never reused. Reruns keep
IDs and user-entered checks. Unknown values stay blank with the reason in `notes`. Every table has `notes`. Google Maps:
store place IDs only, fetch details live, link with Google Maps attribution (`references/google-places.md`).
Status ladder: found → researched → draft_ready → contacted → visited → closed, with `review` as a hold.

## References

`references/google-places.md` · `references/typesafe-jev.md` (official skill in `.claude/skills/typesafe-ai/`) ·
`references/claude.md` · `references/costs.md` · schema: `scripts/schema.sql`
