-- Vending Scout tracker schema (SQLite). Linked tables with enforced foreign keys.
-- Unknown values stay NULL and the reason goes in that row's `notes`.
-- Google Maps content is NOT stored except place IDs (exempt from caching limits).
PRAGMA foreign_keys = ON;

-- Permanent ID counters: an ID is never reused, even if its row is deleted.
CREATE TABLE IF NOT EXISTS id_counters (prefix TEXT PRIMARY KEY, last_value INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS sites (
  site_id      TEXT PRIMARY KEY CHECK (site_id GLOB 'site-[0-9][0-9][0-9]*'),
  place_id     TEXT UNIQUE,         -- Google place ID (storable); details fetched live
  name         TEXT,                -- verified on the employer's own website
  address      TEXT,                -- verified on the employer's own website
  source_url   TEXT,                -- employer page that shows name + address
  match_status TEXT CHECK (match_status IN ('matched','mismatch','unverified') OR match_status IS NULL),
  status       TEXT NOT NULL DEFAULT 'found'
               CHECK (status IN ('found','researched','draft_ready','contacted','visited','closed','review')),
  distance_m   INTEGER,             -- straight-line distance from the search center (derived, refreshed each run)
  found_by     TEXT,                -- Text Search query that first found it
  notes        TEXT
);

CREATE TABLE IF NOT EXISTS status_log (
  site_id TEXT NOT NULL REFERENCES sites(site_id),
  changed_at TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
  changed_by TEXT NOT NULL CHECK (changed_by IN ('research','outreach','user')),
  reason TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id        TEXT PRIMARY KEY CHECK (job_id GLOB 'job-[0-9][0-9][0-9]*'),
  site_id       TEXT NOT NULL REFERENCES sites(site_id) ON UPDATE CASCADE,
  -- source fields (owned by the research step)
  source_url    TEXT,               -- employer's original ad (or the failed source for an 'unavailable' entry)
  checked_at    TEXT,
  ad_status     TEXT CHECK (ad_status IN ('active','stale','unavailable') OR ad_status IS NULL),
  site_match    TEXT CHECK (site_match IN ('confirmed','ambiguous') OR site_match IS NULL),
  excerpt       TEXT,               -- short relevant quote from the ad
  shift_start   TEXT,               -- exact source time, only when the ad has exactly one schedule
  shift_end     TEXT,
  shift_days    TEXT,
  -- label fields (owned by the Jev step)
  arrangement   TEXT CHECK (arrangement IN ('onsite','remote','hybrid','unknown') OR arrangement IS NULL),
  work_type     TEXT CHECK (work_type IN ('physical','desk','mixed','unknown') OR work_type IS NULL),
  shift         TEXT CHECK (shift IN ('day','evening','overnight','rotating','unknown') OR shift IS NULL),
  confidence    REAL CHECK (confidence BETWEEN 0 AND 1 OR confidence IS NULL),
  review_reason TEXT,
  label_distributions TEXT,         -- JSON {question: {option: probability}}
  label_input_hash    TEXT,         -- reuse labels when excerpt + questions are unchanged
  label_error         TEXT,         -- recorded failure (labels stay blank)
  labeled_at          TEXT,
  notes         TEXT,
  UNIQUE (job_id, site_id),
  UNIQUE (site_id, source_url)
);

CREATE TABLE IF NOT EXISTS shifts (
  shift_id TEXT PRIMARY KEY CHECK (shift_id GLOB 'shift-[0-9][0-9][0-9]*'),
  job_id   TEXT NOT NULL,
  site_id  TEXT NOT NULL,
  start    TEXT CHECK (start GLOB '[0-2][0-9]:[0-5][0-9]' OR start IS NULL),
  "end"    TEXT CHECK ("end" GLOB '[0-2][0-9]:[0-5][0-9]' OR "end" IS NULL),
  days     TEXT,                    -- Mon,Tue,... exactly as stated in the source
  source_quote TEXT,
  notes    TEXT,
  UNIQUE (shift_id, job_id, site_id),
  FOREIGN KEY (job_id, site_id) REFERENCES jobs(job_id, site_id) ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS food (
  food_id         TEXT PRIMARY KEY CHECK (food_id GLOB 'food-[0-9][0-9][0-9]*'),
  site_id         TEXT NOT NULL REFERENCES sites(site_id) ON UPDATE CASCADE,
  place_id        TEXT,             -- Google place ID (storable)
  source_url      TEXT,             -- business-owned page the hours were verified on
  distance_m      INTEGER,          -- straight-line from the site (derived; refresh within 30 days)
  opening_periods TEXT,             -- JSON from the business-owned source only, never from Maps
  checked_at      TEXT,
  access_status   TEXT CHECK (access_status IN ('accessible','not_accessible','unknown') OR access_status IS NULL),
  access_by       TEXT CHECK (access_by IN ('user','auto') OR access_by IS NULL),
  notes           TEXT,
  UNIQUE (site_id, place_id)
);

CREATE TABLE IF NOT EXISTS food_searches (
  site_id     TEXT NOT NULL REFERENCES sites(site_id),
  run_id      TEXT,
  searched_at TEXT NOT NULL,
  radius_m    INTEGER NOT NULL,
  calls       INTEGER NOT NULL,
  raw_results INTEGER NOT NULL,
  unique_in_radius INTEGER NOT NULL,
  capped_calls INTEGER NOT NULL,     -- calls that returned the 20-result cap
  complete    INTEGER NOT NULL CHECK (complete IN (0,1)),
  notes       TEXT
);

CREATE TABLE IF NOT EXISTS checks (
  site_id                  TEXT NOT NULL,
  job_id                   TEXT NOT NULL,
  shift_id                 TEXT NOT NULL,
  verified_night_headcount INTEGER CHECK (verified_night_headcount >= 0 OR verified_night_headcount IS NULL),
  headcount_source         TEXT,
  verified_at              TEXT,
  cafeteria                TEXT CHECK (cafeteria IN ('yes','no','unknown') OR cafeteria IS NULL),
  vending_contract         TEXT CHECK (vending_contract IN ('yes','no','unknown') OR vending_contract IS NULL),
  access_notes             TEXT,
  entered_by               TEXT NOT NULL DEFAULT 'auto' CHECK (entered_by IN ('auto','user')),
  notes                    TEXT,
  PRIMARY KEY (site_id, job_id, shift_id),
  FOREIGN KEY (shift_id, job_id, site_id) REFERENCES shifts(shift_id, job_id, site_id) ON UPDATE CASCADE,
  CHECK (verified_night_headcount IS NULL OR (headcount_source IS NOT NULL AND verified_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS results (
  site_id      TEXT NOT NULL,
  job_id       TEXT NOT NULL,
  shift_id     TEXT NOT NULL,
  dead_hours   REAL,                -- shift duration minus union of open-food intervals (hours)
  score        REAL,                -- verified_night_headcount x dead_hours (worker-hours per shift)
  score_status TEXT CHECK (score_status IN ('scored','provisional','needs_verification','not_overnight') OR score_status IS NULL),
  missing_checks TEXT,              -- what must be verified before a score exists
  input_hash   TEXT,                -- reuse when inputs are unchanged
  computed_at  TEXT,
  notes        TEXT,
  PRIMARY KEY (site_id, job_id, shift_id),
  FOREIGN KEY (site_id, job_id, shift_id) REFERENCES checks(site_id, job_id, shift_id) ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS outreach (
  outreach_id    TEXT PRIMARY KEY CHECK (outreach_id GLOB 'out-[0-9][0-9][0-9]*'),
  site_id        TEXT NOT NULL UNIQUE REFERENCES sites(site_id) ON UPDATE CASCADE,  -- one draft per site
  contact_source TEXT,              -- public page where the contact (or contact form) was found
  recipient      TEXT,              -- blank when no public email; see notes for the contact page
  subject        TEXT,
  body           TEXT,              -- draft only, never sent by the skill
  shift_id_referenced TEXT REFERENCES shifts(shift_id),
  word_count     INTEGER CHECK (word_count < 90 OR word_count IS NULL),
  drafted_at     TEXT,
  notes          TEXT
);

CREATE TABLE IF NOT EXISTS run_manifest (
  run_id          TEXT PRIMARY KEY CHECK (run_id GLOB 'run-[0-9][0-9][0-9]*'),
  step            TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  timezone        TEXT NOT NULL,
  center_query    TEXT,
  center_place_id TEXT,
  center_lat      REAL,
  center_lng      REAL,
  center_resolved_at TEXT,
  radius_m        INTEGER,
  batch_size      INTEGER,
  budget_usd      REAL,
  spent_usd_est   REAL DEFAULT 0,
  places_mode     TEXT CHECK (places_mode IN ('live','docs-only','mock')),
  jev_mode        TEXT CHECK (jev_mode IN ('live','docs-only','mock')),
  claude_mode     TEXT CHECK (claude_mode IN ('live','docs-only','mock')),
  coverage        TEXT,             -- JSON: queries, pages, results, dedup, distance filter, caps
  counts          TEXT,             -- JSON row counts at finish
  notes           TEXT
);

CREATE TABLE IF NOT EXISTS integration_checks (
  check_id    TEXT PRIMARY KEY CHECK (check_id GLOB 'chk-[0-9][0-9][0-9]*'),
  run_id      TEXT REFERENCES run_manifest(run_id),
  integration TEXT NOT NULL,
  test        TEXT NOT NULL,
  mode        TEXT NOT NULL CHECK (mode IN ('live','docs-only','mock')),
  result      TEXT NOT NULL CHECK (result IN ('pass','fail','blocked')),
  checked_at  TEXT NOT NULL,
  docs_url    TEXT,
  notes       TEXT
);

CREATE VIEW IF NOT EXISTS v_site_shift AS
SELECT s.site_id, s.place_id, s.name, s.address, s.status AS site_status, s.match_status,
       j.job_id, j.source_url AS job_url, j.ad_status, j.site_match, j.shift AS shift_label, j.confidence, j.review_reason,
       sh.shift_id, sh.start, sh."end", sh.days,
       c.verified_night_headcount, c.headcount_source, c.cafeteria, c.vending_contract, c.entered_by,
       r.dead_hours, r.score, r.score_status, r.missing_checks
FROM sites s
JOIN jobs j    ON j.site_id = s.site_id
JOIN shifts sh ON sh.job_id = j.job_id AND sh.site_id = s.site_id
LEFT JOIN checks c  ON c.site_id = sh.site_id AND c.job_id = sh.job_id AND c.shift_id = sh.shift_id
LEFT JOIN results r ON r.site_id = sh.site_id AND r.job_id = sh.job_id AND r.shift_id = sh.shift_id;
