# Vending Scout tracker

| What | Where |
|---|---|
| Linked tables (source of truth) | `vending_scout.db` (SQLite, foreign keys on) |
| Same tables as CSV | `tables/*.csv` |
| Search settings (center, radius, time zone, spending limit) | `config.json` |
| Site report with Google Maps links | `report/sites.html` |
| Visit checklist | `visit-checklist.md` |
| Run manifests (center, coverage, modes, counts) | `manifests/run-NNN.json` + `run_manifest` table |
| Verification results | `verification.md` |
| Sample output from fictional data | `report/sample-MOCK-report.html`, `report/sample-MOCK-visit-checklist.md` |
| API keys | **not here**: `~/.config/vending-scout/secrets.env` |

Tables: sites, jobs, shifts, food, food_searches, checks, results, outreach, status_log, run_manifest, integration_checks.
Schema: `.claude/skills/vending-scout/scripts/schema.sql`. Only this README is committed to git; the data stays local.
