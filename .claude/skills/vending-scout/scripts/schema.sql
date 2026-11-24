-- Vending Scout tracker schema (SQLite). Linked tables with enforced foreign keys.
-- Rule: unknown values stay NULL/blank and the reason goes in that row's `notes`.
-- Rule: Google Maps content is NOT stored here except place IDs (exempt from caching limits).
PRAGMA foreign_keys = ON;

-- Permanent ID counters: an ID is never reused, even if its row is deleted.
CREATE TABLE IF NOT EXISTS id_counters (
  prefix TEXT PRIMARY KEY,          -- site, job, shift, food, out, run, chk
  last_value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sites (
  site_id      TEXT PRIMARY KEY CHECK (site_id GLOB 'site-[0-9][0-9][0-9]*'),
  place_id     TEXT UNIQUE,         -- Google place ID (storable); details are fetched live
  name         TEXT,                -- from the business's own website, not from Maps
  address      TEXT,                -- from the business's own website, not from Maps
  source_url   TEXT,                -- website page the name/address came from
  match_status TEXT CHECK (match_status IN ('matched','mismatch','unverified') OR match_status IS NULL),
  status       TEXT NOT NULL DEFAULT 'candidate'
               CHECK (status IN ('candidate','researching','qualified','rejected','contacted')),
  notes        TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id        TEXT PRIMARY KEY CHECK (job_id GLOB 'job-[0-9][0-9][0-9]*'),
  site_id       TEXT NOT NULL REFERENCES sites(site_id) ON UPDATE CASCADE,
  source_url    TEXT,
  checked_at    TEXT,               -- ISO 8601 in the tracker's local time zone
  ad_status     TEXT CHECK (ad_status IN ('active','expired','not_job_ad','unknown') OR ad_status IS NULL),
  excerpt       TEXT,               -- short quote (<= 300 chars) used as Jev state
  arrangement   TEXT,               -- Jev label: on_site | hybrid | remote
  work_type     TEXT,               -- Jev label: full_time | part_time | temporary_or_agency | internship | freelance
  shift         TEXT,               -- Jev label: fixed_day | fixed_evening | fixed_night | rotating | unspecified
  shift_start   TEXT,               -- HH:MM, only when the ad has exactly one schedule (else see shifts)
  shift_end     TEXT,
  shift_days    TEXT,               -- e.g. Mon,Tue,Wed,Thu,Fri
  confidence    REAL CHECK (confidence BETWEEN 0 AND 1 OR confidence IS NULL),
  review_reason TEXT,               -- why a person should look at this row (blank = none)
  notes         TEXT,
  UNIQUE (job_id, site_id)
);

CREATE TABLE IF NOT EXISTS shifts (
  shift_id TEXT PRIMARY KEY CHECK (shift_id GLOB 'shift-[0-9][0-9][0-9]*'),
  job_id   TEXT NOT NULL,
  site_id  TEXT NOT NULL,
  start    TEXT CHECK (start GLOB '[0-2][0-9]:[0-5][0-9]' OR start IS NULL),
  "end"    TEXT CHECK ("end" GLOB '[0-2][0-9]:[0-5][0-9]' OR "end" IS NULL),
  days     TEXT,
  notes    TEXT,
  UNIQUE (shift_id, job_id, site_id),
  FOREIGN KEY (job_id, site_id) REFERENCES jobs(job_id, site_id) ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS food (
  food_id         TEXT PRIMARY KEY CHECK (food_id GLOB 'food-[0-9][0-9][0-9]*'),
  site_id         TEXT NOT NULL REFERENCES sites(site_id) ON UPDATE CASCADE,
  place_id        TEXT,             -- Google place ID of the food venue (storable)
  source_url      TEXT,             -- venue's own website page for opening hours
  distance_m      INTEGER,          -- derived from live coordinates; refresh within 30 days
  opening_periods TEXT,             -- JSON, only from the venue's own website (never Maps)
  checked_at      TEXT,
  access_status   TEXT CHECK (access_status IN ('open_during_shift','closed_during_shift','partial','unknown') OR access_status IS NULL),
  notes           TEXT,
  UNIQUE (site_id, place_id)
);

CREATE TABLE IF NOT EXISTS checks (
  site_id                  TEXT NOT NULL,
  job_id                   TEXT NOT NULL,
  shift_id                 TEXT NOT NULL,
  verified_night_headcount INTEGER CHECK (verified_night_headcount >= 0 OR verified_night_headcount IS NULL),
  headcount_source         TEXT,    -- URL or named person/document; required when headcount is set
  verified_at              TEXT,
  cafeteria                TEXT CHECK (cafeteria IN ('yes','no','unknown') OR cafeteria IS NULL),
  vending_contract         TEXT CHECK (vending_contract IN ('yes','no','unknown') OR vending_contract IS NULL),
  access_notes             TEXT,
  notes                    TEXT,
  PRIMARY KEY (site_id, job_id, shift_id),
  FOREIGN KEY (shift_id, job_id, site_id) REFERENCES shifts(shift_id, job_id, site_id) ON UPDATE CASCADE,
  CHECK (verified_night_headcount IS NULL OR headcount_source IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS results (
  site_id      TEXT NOT NULL,
  job_id       TEXT NOT NULL,
  shift_id     TEXT NOT NULL,
  dead_hours   REAL,                -- avg hours per shift with no nearby food open
  score        REAL,
  score_status TEXT CHECK (score_status IN ('scored','needs_headcount','needs_food_hours','needs_shift_times','not_night_shift') OR score_status IS NULL),
  notes        TEXT,
  PRIMARY KEY (site_id, job_id, shift_id),
  FOREIGN KEY (site_id, job_id, shift_id) REFERENCES checks(site_id, job_id, shift_id) ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS outreach (
  outreach_id    TEXT PRIMARY KEY CHECK (outreach_id GLOB 'out-[0-9][0-9][0-9]*'),
  site_id        TEXT NOT NULL REFERENCES sites(site_id) ON UPDATE CASCADE,
  contact_source TEXT,              -- URL where the contact was published
  recipient      TEXT,
  subject        TEXT,
  body           TEXT,              -- draft only; the skill never sends email
  notes          TEXT
);

CREATE TABLE IF NOT EXISTS run_manifest (
  run_id         TEXT PRIMARY KEY CHECK (run_id GLOB 'run-[0-9][0-9][0-9]*'),
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  timezone       TEXT NOT NULL,
  center_query   TEXT,
  radius_m       INTEGER,
  budget_usd     REAL,
  spent_usd_est  REAL DEFAULT 0,
  places_mode    TEXT CHECK (places_mode IN ('live','docs-only','mock')),
  jev_mode       TEXT CHECK (jev_mode IN ('live','docs-only','mock')),
  claude_mode    TEXT CHECK (claude_mode IN ('live','docs-only','mock')),
  counts         TEXT,              -- JSON row counts at finish
  notes          TEXT
);

CREATE TABLE IF NOT EXISTS integration_checks (
  check_id    TEXT PRIMARY KEY CHECK (check_id GLOB 'chk-[0-9][0-9][0-9]*'),
  run_id      TEXT REFERENCES run_manifest(run_id),
  integration TEXT NOT NULL,        -- google_places | typesafe_jev | claude
  test        TEXT NOT NULL,
  mode        TEXT NOT NULL CHECK (mode IN ('live','docs-only','mock')),
  result      TEXT NOT NULL,        -- pass | fail | blocked
  checked_at  TEXT NOT NULL,
  docs_url    TEXT,
  notes       TEXT
);

-- One row per site x job x shift with everything joined (used by the report and join checks).
CREATE VIEW IF NOT EXISTS v_site_shift AS
SELECT s.site_id, s.place_id, s.name, s.address, s.status AS site_status, s.match_status,
       j.job_id, j.source_url AS job_url, j.ad_status, j.shift AS shift_label, j.confidence, j.review_reason,
       sh.shift_id, sh.start, sh."end", sh.days,
       c.verified_night_headcount, c.headcount_source, c.cafeteria, c.vending_contract,
       r.dead_hours, r.score, r.score_status
FROM sites s
JOIN jobs j    ON j.site_id = s.site_id
JOIN shifts sh ON sh.job_id = j.job_id AND sh.site_id = s.site_id
LEFT JOIN checks c  ON c.site_id = sh.site_id AND c.job_id = sh.job_id AND c.shift_id = sh.shift_id
LEFT JOIN results r ON r.site_id = sh.site_id AND r.job_id = sh.job_id AND r.shift_id = sh.shift_id;
