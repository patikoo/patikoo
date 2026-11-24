# Vending Scout tracker

| What | Where |
|---|---|
| Linked tables (source of truth) | `vending_scout.db` (SQLite, foreign keys on) |
| Same tables as CSV | `tables/*.csv` (rewritten by `scout.py report`) |
| Search settings | `config.json` (center, radius, time zone, spending limit) |
| Site report with Google Maps links | `report/sites.html` |
| Sample report from mock data | `report/sample-MOCK-report.html` (fictional rows) |
| Run manifests + integration checks | `manifests/run-NNN.json` and the `run_manifest` / `integration_checks` tables |
| Join verification | `verification.md` |
| API keys | **not here**: `~/.config/vending-scout/secrets.env` |

Tables: `sites`, `jobs`, `shifts`, `food`, `checks`, `results`, `outreach`, `run_manifest`, `integration_checks`.
Schema and comments: `.claude/skills/vending-scout/scripts/schema.sql`.
