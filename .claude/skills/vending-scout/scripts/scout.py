#!/usr/bin/env python3
"""Vending Scout CLI.

  scout.py init --center "Enschede" --radius 25 --unit km --timezone Europe/Amsterdam [--budget 10]
  scout.py access                 # which keys are missing (never prints values)
  scout.py check                  # one Places lookup, one Jev request, one Claude ping; records live/docs-only/mock
  scout.py run [--max-sites 15]   # full research run (needs all keys + budget)
  scout.py verify                 # join/integrity checks on a mock copy + the real tracker
  scout.py report                 # HTML site report + CSV exports
  scout.py where                  # where everything is saved
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vs_common as vc  # noqa: E402
import places  # noqa: E402
import scoring  # noqa: E402

TABLES = ["sites", "jobs", "shifts", "food", "checks", "results", "outreach", "run_manifest", "integration_checks"]


# ---------------------------------------------------------------- setup
def cmd_init(a) -> None:
    cfg = vc.load_config()
    for k, v in (("center_query", a.center), ("radius_value", a.radius), ("radius_unit", a.unit),
                 ("timezone", a.timezone), ("budget_usd", a.budget)):
        if v is not None:
            if k == "center_query" and v != cfg.get("center_query"):
                cfg.update(center_lat=None, center_lng=None, center_fetched_at=None)
            cfg[k] = v
    vc.save_config(cfg)
    con = vc.connect()
    vc.init_db(con)
    sec = vc.ensure_secrets_file()
    (vc.TRACKER / "report").mkdir(exist_ok=True)
    (vc.TRACKER / "tables").mkdir(exist_ok=True)
    (vc.TRACKER / "manifests").mkdir(exist_ok=True)
    print(f"Tracker: {vc.TRACKER}\nDatabase: {vc.DB_PATH}\nConfig: {vc.CONFIG_PATH}\nSecrets file: {sec}")
    print(f"Search: {cfg['radius_value']} {cfg['radius_unit']} ({vc.radius_m(cfg)} m) around {cfg['center_query']!r}, tz {cfg['timezone']}")


def missing_access(cfg: dict) -> list[str]:
    sec = vc.load_secrets()
    miss = [k for k in vc.SECRET_KEYS if k not in sec]
    if not cfg.get("center_query"):
        miss.append("location (town or address)")
    if cfg.get("budget_usd") is None:
        miss.append("spending limit (USD per run)")
    return miss


def cmd_access(a) -> None:
    miss = missing_access(vc.load_config())
    print("Missing: " + (", ".join(miss) if miss else "nothing"))


def _start_run(con, cfg, modes, notes="") -> str:
    rid = vc.new_id(con, "run")
    con.execute("INSERT INTO run_manifest(run_id, started_at, timezone, center_query, radius_m, budget_usd, "
                "places_mode, jev_mode, claude_mode, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, vc.now_iso(cfg), cfg["timezone"], cfg["center_query"], vc.radius_m(cfg), cfg["budget_usd"],
                 modes["places"], modes["jev"], modes["claude"], notes))
    con.commit()
    return rid


def _finish_run(con, cfg, rid, spent) -> None:
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    con.execute("UPDATE run_manifest SET finished_at=?, spent_usd_est=?, counts=? WHERE run_id=?",
                (vc.now_iso(cfg), round(spent, 4), json.dumps(counts), rid))
    con.commit()
    row = dict(con.execute("SELECT * FROM run_manifest WHERE run_id=?", (rid,)).fetchone())
    row["integration_checks"] = [dict(r) for r in con.execute("SELECT * FROM integration_checks WHERE run_id=?", (rid,))]
    (vc.TRACKER / "manifests" / f"{rid}.json").write_text(json.dumps(row, indent=2) + "\n")


def _record(con, cfg, rid, integration, test, mode, result, docs, notes) -> None:
    con.execute("INSERT INTO integration_checks VALUES (?,?,?,?,?,?,?,?,?)",
                (vc.new_id(con, "chk"), rid, integration, test, mode, result, vc.now_iso(cfg), docs, notes))
    con.commit()
    print(f"[{mode:9}] {integration:13} {result:7} {test} — {notes}")


def _reachable(url: str) -> str:
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10)
        return "reachable"
    except urllib.error.HTTPError as e:
        return f"reachable (HTTP {e.code} without key)"
    except Exception as e:  # proxy refusal, DNS, timeout
        return f"NOT reachable ({type(e).__name__}: {str(e)[:80]})"


# ---------------------------------------------------------------- integration checks
SAMPLE_AD = ("Orderpicker nachtdienst Enschede. Je werkt in een vaste nachtdienst van 22:00 tot 06:30, "
             "maandag t/m vrijdag, in ons distributiecentrum. 38 uur per week, via uitzendbureau, met kans op vast contract.")


def cmd_check(a) -> None:
    cfg = vc.load_config()
    sec = vc.load_secrets()
    con = vc.connect()
    vc.init_db(con)
    modes = {"places": "live" if "GOOGLE_MAPS_API_KEY" in sec else "docs-only",
             "jev": "live" if "TYPESAFE_API_KEY" in sec else "docs-only",
             "claude": "live" if "ANTHROPIC_API_KEY" in sec else "docs-only"}
    rid = _start_run(con, cfg, modes, "integration check")
    spent = 0.0

    # --- Google Places: one Text Search lookup (IDs-only field mask = no-charge SKU)
    url = places.BASE + "/places:searchText"
    if modes["places"] == "live":
        try:
            d = places._call("POST", "/places:searchText", sec["GOOGLE_MAPS_API_KEY"], "places.id",
                             {"textQuery": cfg["center_query"] or "Enschede", "pageSize": 1})
            pid = d["places"][0]["id"]
            _record(con, cfg, rid, "google_places", "Text Search lookup (IDs only)", "live", "pass", places.DOCS["text_search"],
                    f"Got place_id {pid}; Maps link {places.maps_url(pid, cfg['center_query'] or '')}")
        except Exception as e:
            _record(con, cfg, rid, "google_places", "Text Search lookup (IDs only)", "live", "fail", places.DOCS["text_search"], str(e)[:300])
    else:
        _record(con, cfg, rid, "google_places", "Text Search lookup (IDs only)", "docs-only", "blocked", places.DOCS["text_search"],
                f"No GOOGLE_MAPS_API_KEY. Request built per docs (POST {url}, X-Goog-FieldMask: places.id). Endpoint {_reachable(url)}.")
    fixture = {"places": [{"id": "ChIJmock-site", "location": {"latitude": 52.2215, "longitude": 6.8937}}]}
    ok = fixture["places"][0]["id"].startswith("ChIJ") and places.maps_url("ChIJmock-site", "x").startswith("https://www.google.com/maps/search/?api=1")
    _record(con, cfg, rid, "google_places", "Parse Text Search response + build Maps URL", "mock", "pass" if ok else "fail",
            places.DOCS["maps_urls"], "Fixture shaped like the documented Places (New) response; no network.")

    # --- TypeSafe Jev: one System One request
    import jev
    from typesafe_sdk import SystemOneResponse
    ts_url = "https://api.typesafe.ai/v1/models"
    if modes["jev"] == "live":
        try:
            dec, resp, _ = jev.label_ad(sec["TYPESAFE_API_KEY"], SAMPLE_AD, "Sample DC", None)
            _record(con, cfg, rid, "typesafe_jev", "System One request (6 questions)", "live", "pass", jev.DOCS["sdk"],
                    f"model={resp.model} tokens_in={resp.usage.input_tokens} -> {dec}")
        except Exception as e:
            _record(con, cfg, rid, "typesafe_jev", "System One request (6 questions)", "live", "fail", jev.DOCS["sdk"], str(e)[:300])
    else:
        payload = {k: q.model_dump(exclude_none=True) for k, q in jev.QUESTIONS.items()}
        _record(con, cfg, rid, "typesafe_jev", "System One request (6 questions)", "docs-only", "blocked", jev.DOCS["sdk"],
                f"No TYPESAFE_API_KEY. {len(payload)} questions validated by typesafe-sdk types. api.typesafe.ai {_reachable(ts_url)}.")
    fake = SystemOneResponse.model_validate({
        "model": "jev-mock", "usage": {"input_tokens": 0, "output_tokens": 0},
        "answers": {
            "is_job_ad": {"type": "noul", "noul": 0.97}, "mentions_schedule": {"type": "noul", "noul": 0.95},
            "night_work": {"type": "noul", "noul": 0.96},
            "arrangement": {"type": "choice", "choice": "on_site", "confidence": 0.9, "probabilities": {"on_site": 0.95, "hybrid": 0.04, "remote": 0.01}},
            "work_type": {"type": "choice", "choice": "temporary_or_agency", "confidence": 0.7,
                          "probabilities": {"full_time": 0.2, "part_time": 0.02, "temporary_or_agency": 0.75, "internship": 0.01, "freelance": 0.02}},
            "shift": {"type": "choice", "choice": "fixed_night", "confidence": 0.9,
                      "probabilities": {"fixed_day": 0.01, "fixed_evening": 0.01, "fixed_night": 0.93, "rotating": 0.04, "unspecified": 0.01}}}})
    dec = jev.decide(fake)
    ok = dec["shift"] == "fixed_night" and dec["arrangement"] == "on_site" and dec["review_reason"] is None
    _record(con, cfg, rid, "typesafe_jev", "Decision code on SDK-typed response", "mock", "pass" if ok else "fail", jev.DOCS["api"],
            f"Fake SystemOneResponse -> {dec}")

    # --- Claude
    import claude_research as cr
    if modes["claude"] == "live":
        try:
            txt, usd = cr.ping(cr.make_client(sec["ANTHROPIC_API_KEY"]), cfg["claude_model"])
            spent += usd
            _record(con, cfg, rid, "claude", "Messages API ping", "live", "pass", cr.DOCS["messages"], f"reply={txt!r} cost~${usd:.4f}")
        except Exception as e:
            _record(con, cfg, rid, "claude", "Messages API ping", "live", "fail", cr.DOCS["messages"], str(e)[:300])
    else:
        _record(con, cfg, rid, "claude", "Messages API ping", "docs-only", "blocked", cr.DOCS["messages"],
                f"No ANTHROPIC_API_KEY. api.anthropic.com {_reachable('https://api.anthropic.com/v1/models')}.")
    s = scoring.score({"start": "22:00", "end": "06:30", "days": "Mon,Tue,Wed,Thu,Fri"},
                      {"verified_night_headcount": 40, "cafeteria": "no", "vending_contract": "unknown"},
                      [[{"open": {"day": d, "hour": 7, "minute": 0}, "close": {"day": d, "hour": 21, "minute": 0}} for d in range(7)]])
    _record(con, cfg, rid, "claude", "Scoring pipeline on mock research output", "mock", "pass" if s["score_status"] == "scored" else "fail",
            cr.DOCS["structured_outputs"], f"{s}")
    _finish_run(con, cfg, rid, spent)
    print(f"Run manifest: {vc.TRACKER / 'manifests' / (rid + '.json')}")
    miss = missing_access(cfg)
    if miss:
        print("MISSING ACCESS (stop before a real run): " + ", ".join(miss))


# ---------------------------------------------------------------- full run
def _match(website_name: str | None, live_name: str | None) -> str:
    if not website_name or not live_name:
        return "unverified"
    norm = lambda s: {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in s).split() if len(w) > 2}
    a, b = norm(website_name), norm(live_name)
    return "matched" if a & b else "mismatch"


def cmd_run(a) -> None:
    cfg = vc.load_config()
    miss = missing_access(cfg)
    if miss:
        sys.exit("Missing access, not running: " + ", ".join(miss))
    sec = vc.load_secrets()
    import claude_research as cr
    import jev
    con = vc.connect()
    vc.init_db(con)
    budget = vc.Budget(cfg["budget_usd"])
    rid = _start_run(con, cfg, {"places": "live", "jev": "live", "claude": "live"}, f"max_sites={a.max_sites}")
    gkey, model = sec["GOOGLE_MAPS_API_KEY"], cfg["claude_model"]
    client = cr.make_client(sec["ANTHROPIC_API_KEY"])
    try:
        # 1. center point (lat/lng cache limited to 30 days)
        fetched = cfg.get("center_fetched_at")
        if not cfg.get("center_lat") or not fetched or datetime.fromisoformat(fetched) < vc.now_local(cfg) - timedelta(days=30):
            cfg["center_lat"], cfg["center_lng"] = places.geocode_center(gkey, cfg["center_query"])
            cfg["center_fetched_at"] = vc.now_iso(cfg)
            vc.save_config(cfg)
        lat0, lng0, rad = cfg["center_lat"], cfg["center_lng"], vc.radius_m(cfg)

        # 2. candidate sites (store place IDs only)
        live = {}
        for q in cfg["site_queries"]:
            for p in places.text_search(gkey, q, lat0, lng0, rad):
                loc = p.get("location") or {}
                if loc and places.distance_m(lat0, lng0, loc["latitude"], loc["longitude"]) <= rad:
                    live[p["id"]] = p
        for pid in live:
            if not con.execute("SELECT 1 FROM sites WHERE place_id=?", (pid,)).fetchone():
                con.execute("INSERT INTO sites(site_id, place_id, match_status, status, notes) VALUES (?,?,?,?,?)",
                            (vc.new_id(con, "site"), pid, "unverified", "candidate",
                             "name/address blank: not yet read from the business's own website"))
        con.commit()

        # 3. research each candidate
        todo = con.execute("SELECT * FROM sites WHERE status='candidate' ORDER BY site_id LIMIT ?", (a.max_sites,)).fetchall()
        for site in todo:
            budget.guard(cr.EST_SITE_USD)
            det = places.place_details(gkey, site["place_id"])  # live, not stored
            hint = {"label": (det.get("displayName") or {}).get("text"), "website": det.get("websiteUri"),
                    "map_address": det.get("formattedAddress")}
            data, usd = cr.research_site(client, model, hint)
            budget.charge(usd)
            b = data["business"]
            con.execute("UPDATE sites SET name=?, address=?, source_url=?, match_status=?, status='researching', notes=? WHERE site_id=?",
                        (b["name"], b["address"], b["source_url"], _match(b["name"], hint["label"]),
                         b["note"] or None, site["site_id"]))
            loc = det.get("location") or {}
            venues_live = places.nearby_food(gkey, loc["latitude"], loc["longitude"], cfg["food_radius_m"]) if loc else []
            hours_live = [v["regularOpeningHours"]["periods"] for v in venues_live if v.get("regularOpeningHours", {}).get("periods")]
            for v in venues_live:
                vl = v.get("location") or {}
                dist = places.distance_m(loc["latitude"], loc["longitude"], vl["latitude"], vl["longitude"]) if vl else None
                con.execute("INSERT INTO food(food_id, site_id, place_id, source_url, distance_m, opening_periods, checked_at, access_status, notes) "
                            "VALUES (?,?,?,?,?,NULL,?,?,?) ON CONFLICT(site_id, place_id) DO UPDATE SET distance_m=excluded.distance_m, checked_at=excluded.checked_at",
                            (vc.new_id(con, "food"), site["site_id"], v["id"], None, dist, vc.now_iso(cfg), "unknown",
                             "opening_periods blank: Google hours are used live for scoring and not stored (Maps terms); "
                             "fill from the venue's own website if published. distance_m refresh within 30 days."))
            for ad in data["jobs"]:
                if con.execute("SELECT 1 FROM jobs WHERE site_id=? AND source_url=?", (site["site_id"], ad["source_url"])).fetchone():
                    continue
                job_id = vc.new_id(con, "job")
                budget.guard(0.01)
                dec, _, _ = jev.label_ad(sec["TYPESAFE_API_KEY"], ad["excerpt"][:300], b["name"], ad["source_url"])
                sch = ad["schedules"]
                one = sch[0] if len(sch) == 1 else {}
                notes = [ad["note"]] if ad["note"] else []
                if len(sch) > 1:
                    notes.append(f"{len(sch)} schedules in this ad; see shifts table")
                if not sch:
                    notes.append("ad states no schedule")
                con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (job_id, site["site_id"], ad["source_url"], vc.now_iso(cfg),
                             dec["ad_status_hint"] or ad["ad_status"], ad["excerpt"][:300], dec["arrangement"], dec["work_type"],
                             dec["shift"], one.get("start"), one.get("end"), one.get("days"), dec["confidence"],
                             dec["review_reason"], "; ".join(notes) or None))
                hc = data["headcount"]
                for s in sch or [{"start": None, "end": None, "days": None, "note": "no schedule stated"}]:
                    sid = vc.new_id(con, "shift")
                    con.execute("INSERT INTO shifts VALUES (?,?,?,?,?,?,?)",
                                (sid, job_id, site["site_id"], s["start"], s["end"], s["days"], s["note"] or None))
                    night = bool(s["start"] and s["end"] and scoring.is_night(s["start"], s["end"]))
                    count = hc["night_count"] if (night and hc["source"]) else None
                    con.execute("INSERT INTO checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (site["site_id"], job_id, sid, count, hc["source"] if count is not None else None,
                                 vc.now_iso(cfg) if count is not None else None, data["cafeteria"], data["vending_contract"],
                                 None, hc["note"] if count is None else None))
                    chk = dict(con.execute("SELECT * FROM checks WHERE shift_id=?", (sid,)).fetchone())
                    r = scoring.score(s, chk, hours_live if hours_live else None)
                    con.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?)",
                                (site["site_id"], job_id, sid, r["dead_hours"], r["score"], r["score_status"], r["notes"]))
            best = con.execute("SELECT MAX(score) FROM results WHERE site_id=?", (site["site_id"],)).fetchone()[0]
            if best is not None:
                con.execute("UPDATE sites SET status='qualified' WHERE site_id=?", (site["site_id"],))
                c = data["contact"]
                if c["recipient"] and not con.execute("SELECT 1 FROM outreach WHERE site_id=?", (site["site_id"],)).fetchone():
                    budget.guard(0.05)
                    em, usd = cr.draft_outreach(client, model, {"name": b["name"]},
                                                f"cafeteria={data['cafeteria']}, night shifts advertised")
                    budget.charge(usd)
                    con.execute("INSERT INTO outreach VALUES (?,?,?,?,?,?,?)",
                                (vc.new_id(con, "out"), site["site_id"], c["source"], c["recipient"], em["subject"], em["body"],
                                 "Draft only — review before sending."))
            con.commit()
            print(f"{site['site_id']}: researched, ~${budget.spent:.2f} spent")
    except vc.BudgetExceeded as e:
        print(f"Stopped: {e}")
    finally:
        con.commit()
        _finish_run(con, cfg, rid, budget.spent)
    cmd_report(a)


# ---------------------------------------------------------------- verification
def join_checks(con) -> list[tuple[str, bool, str]]:
    q = lambda s: con.execute(s).fetchone()[0]
    out = []
    out.append(("foreign keys", q("SELECT COUNT(*) FROM pragma_foreign_key_check") == 0, "PRAGMA foreign_key_check"))
    out.append(("shifts -> jobs same site", q("SELECT COUNT(*) FROM shifts sh JOIN jobs j ON j.job_id=sh.job_id WHERE j.site_id<>sh.site_id") == 0, ""))
    out.append(("every shift in joined view", q("SELECT COUNT(*) FROM shifts") == q("SELECT COUNT(*) FROM v_site_shift"), ""))
    out.append(("checks keyed to a real shift", q("SELECT COUNT(*) FROM checks c LEFT JOIN shifts s ON s.shift_id=c.shift_id AND s.job_id=c.job_id AND s.site_id=c.site_id WHERE s.shift_id IS NULL") == 0, ""))
    out.append(("results keyed to a check", q("SELECT COUNT(*) FROM results r LEFT JOIN checks c USING(site_id, job_id, shift_id) WHERE c.site_id IS NULL") == 0, ""))
    out.append(("blank values explained", q("SELECT COUNT(*) FROM results WHERE score IS NULL AND (notes IS NULL OR notes='')") == 0
                and q("SELECT COUNT(*) FROM sites WHERE name IS NULL AND (notes IS NULL OR notes='')") == 0, "blank score/name has a notes reason"))
    return out


def cmd_verify(a) -> None:
    lines = [f"# Join verification — {vc.now_iso()}", ""]
    with tempfile.TemporaryDirectory() as td:
        con = vc.connect(Path(td) / "mock.db")
        vc.init_db(con)
        seed_mock(con)
        lines.append("## Mock tracker (mode: mock)")
        for name, ok, info in join_checks(con):
            lines.append(f"- {'PASS' if ok else 'FAIL'} {name} {info}")
        # negative tests: the schema must reject bad links
        for label, sql in [
            ("reject check for unknown shift", "INSERT INTO checks(site_id, job_id, shift_id) VALUES ('site-001','job-001','shift-999')"),
            ("reject shift whose site differs from its job", "INSERT INTO shifts VALUES ('shift-900','job-001','site-002','22:00','06:00','Mon',NULL)"),
            ("reject result without a check", "INSERT INTO results(site_id, job_id, shift_id) VALUES ('site-002','job-003','shift-005')"),
            ("reject headcount without source", "UPDATE checks SET verified_night_headcount=5, headcount_source=NULL WHERE shift_id='shift-002'"),
        ]:
            try:
                con.execute(sql)
                lines.append(f"- FAIL {label} (accepted)")
            except sqlite3.IntegrityError:
                lines.append(f"- PASS {label}")
        con.execute("DELETE FROM outreach WHERE outreach_id='out-001'")
        nid = vc.new_id(con, "out")
        lines.append(f"- {'PASS' if nid == 'out-002' else 'FAIL'} IDs never reused (after deleting out-001 the next ID is {nid})")
        rows = con.execute("SELECT site_id, job_id, shift_id, start, \"end\", days, score, score_status FROM v_site_shift ORDER BY shift_id").fetchall()
        lines += ["", "| site | job | shift | start | end | days | score | status |", "|---|---|---|---|---|---|---|---|"]
        lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
        mock_report = vc.TRACKER / "report" / "sample-MOCK-report.html"
        write_report(con, mock_report, mock=True)
        lines += ["", f"Sample report from mock data: `{mock_report}`"]
        con.close()
    if vc.DB_PATH.exists():
        con = vc.connect()
        lines += ["", "## Real tracker"]
        for name, ok, info in join_checks(con):
            lines.append(f"- {'PASS' if ok else 'FAIL'} {name} {info}")
    out = vc.TRACKER / "verification.md"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nSaved: {out}")


def seed_mock(con) -> None:
    """Fictional data (MOCK) used only to prove the links; never written to the real tracker."""
    n = vc.now_iso()
    for _ in range(2):
        sid = vc.new_id(con, "site")
        con.execute("INSERT INTO sites VALUES (?,?,?,?,?,?,?,?)",
                    (sid, f"ChIJMOCK{sid}", f"MOCK Logistics {sid}", "MOCK street 1", "https://example.com/contact",
                     "matched", "qualified", "MOCK row"))
    # site-001: one ad with two schedules (day + night); site-002: one night-only ad, one ad without times
    con.execute("INSERT INTO jobs VALUES ('job-001','site-001','https://example.com/job1',?, 'active','Orderpicker dag- en nachtdienst',"
                "'on_site','temporary_or_agency','rotating',NULL,NULL,NULL,0.88,NULL,'2 schedules in this ad; see shifts table')", (n,))
    con.execute("INSERT INTO jobs VALUES ('job-002','site-002','https://example.com/job2',?, 'active','Productiemedewerker nacht',"
                "'on_site','full_time','fixed_night','23:00','07:00','Mon,Tue,Wed,Thu,Fri',0.91,NULL,NULL)", (n,))
    con.execute("INSERT INTO jobs VALUES ('job-003','site-002','https://example.com/job3',?, 'active','Schoonmaker',"
                "'on_site','part_time','unspecified',NULL,NULL,NULL,0.55,'mentions_schedule unsettled (0.52)','ad states no schedule')", (n,))
    for nm in ("job", "shift"):
        con.execute("INSERT OR REPLACE INTO id_counters VALUES (?, ?)", (nm, 3 if nm == "job" else 4))
    shifts = [("shift-001", "job-001", "site-001", "07:00", "15:00", "Mon,Tue,Wed,Thu,Fri", None),
              ("shift-002", "job-001", "site-001", "22:00", "06:00", "Sun,Mon,Tue,Wed,Thu", None),
              ("shift-003", "job-002", "site-002", "23:00", "07:00", "Mon,Tue,Wed,Thu,Fri", None),
              ("shift-004", "job-003", "site-002", None, None, None, "no schedule stated")]
    con.executemany("INSERT INTO shifts VALUES (?,?,?,?,?,?,?)", shifts)
    hours = [[{"open": {"day": d, "hour": 8, "minute": 0}, "close": {"day": d, "hour": 22, "minute": 0}} for d in range(7)]]
    checks = {"shift-001": (None, None, "no", "no"), "shift-002": (35, "https://example.com/news", "no", "no"),
              "shift-003": (None, None, "yes", "unknown"), "shift-004": (None, None, "unknown", "unknown")}
    for sh in shifts:
        hc, src, caf, ven = checks[sh[0]]
        con.execute("INSERT INTO checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sh[2], sh[1], sh[0], hc, src, n if hc else None, caf, ven, None,
                     None if hc else "MOCK: no source states a night headcount"))
        r = scoring.score({"start": sh[3], "end": sh[4], "days": sh[5]},
                          {"verified_night_headcount": hc, "cafeteria": caf, "vending_contract": ven}, hours)
        con.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?)", (sh[2], sh[1], sh[0], r["dead_hours"], r["score"], r["score_status"], r["notes"]))
    con.execute("INSERT INTO food VALUES ('food-001','site-001','ChIJMOCKfood','https://example.com/snackbar',420,?,?,'partial','MOCK')",
                (json.dumps(hours[0]), n))
    con.execute("INSERT INTO id_counters VALUES ('food', 1)")
    con.execute("INSERT INTO outreach VALUES (?,?,?,?,?,?,?)", (vc.new_id(con, "out"), "site-001", "https://example.com/contact",
                "facilities@example.com", "MOCK subject", "MOCK body", "Draft only"))
    con.commit()


# ---------------------------------------------------------------- report + exports
def _esc(v) -> str:
    import html
    return "" if v is None else html.escape(str(v))


def write_report(con, path: Path, mock: bool = False) -> None:
    sites = con.execute("SELECT * FROM sites ORDER BY site_id").fetchall()
    best = {r[0]: r[1] for r in con.execute("SELECT site_id, MAX(score) FROM results GROUP BY site_id")}
    sites = sorted(sites, key=lambda s: -(best.get(s["site_id"]) or -1))
    banner = "<p class=mock>MOCK DATA — fictional rows used to test the links. Not real businesses.</p>" if mock else ""
    rows = []
    for s in sites:
        link = f'<a href="{_esc(places.maps_url(s["place_id"], s["name"] or ""))}" target=_blank>Google Maps</a>' if s["place_id"] else ""
        rows.append(f'<tr><td><a href="#{s["site_id"]}">{s["site_id"]}</a></td><td>{_esc(s["name"])}</td><td>{_esc(s["address"])}</td>'
                    f'<td>{_esc(s["status"])}</td><td>{_esc(s["match_status"])}</td><td>{_esc(best.get(s["site_id"]))}</td><td>{link}</td></tr>')
    detail = []
    for s in sites:
        sid = s["site_id"]
        src = f'<a href="{_esc(s["source_url"])}">website</a>' if s["source_url"] else "no website source yet"
        maps = f' · <a href="{_esc(places.maps_url(s["place_id"], s["name"] or ""))}" target=_blank>Google Maps</a>' if s["place_id"] else ""
        part = [f'<section id="{sid}"><h2>{sid} · {_esc(s["name"]) or "<i>name pending</i>"}</h2><p>{_esc(s["address"])} — {src}{maps}</p>',
                f'<p class=note>{_esc(s["notes"])}</p>']
        part.append("<table><tr><th>shift</th><th>job</th><th>label</th><th>start</th><th>end</th><th>days</th><th>night staff</th>"
                    "<th>cafeteria</th><th>vending</th><th>dead h</th><th>score</th><th>status / notes</th></tr>")
        for r in con.execute("SELECT v.*, j.source_url ju, res.notes rn FROM v_site_shift v JOIN jobs j USING(job_id) "
                             "LEFT JOIN results res USING(site_id, job_id, shift_id) WHERE v.site_id=? ORDER BY shift_id", (sid,)):
            part.append(f'<tr id="{r["shift_id"]}"><td>{r["shift_id"]}</td><td><a href="{_esc(r["ju"])}">{r["job_id"]}</a></td>'
                        f'<td>{_esc(r["shift_label"])}</td><td>{_esc(r["start"])}</td><td>{_esc(r["end"])}</td><td>{_esc(r["days"])}</td>'
                        f'<td>{_esc(r["verified_night_headcount"])}</td><td>{_esc(r["cafeteria"])}</td><td>{_esc(r["vending_contract"])}</td>'
                        f'<td>{_esc(r["dead_hours"])}</td><td>{_esc(r["score"])}</td><td>{_esc(r["score_status"])}<br><small>{_esc(r["rn"])}</small></td></tr>')
        part.append("</table>")
        food = con.execute("SELECT * FROM food WHERE site_id=? ORDER BY distance_m", (sid,)).fetchall()
        if food:
            part.append("<p>Food within walking distance: " + ", ".join(
                f'<a href="{_esc(places.maps_url(f["place_id"]))}" target=_blank>{f["food_id"]}</a> ({_esc(f["distance_m"])} m)'
                for f in food if f["place_id"]) + "</p>")
        for o in con.execute("SELECT * FROM outreach WHERE site_id=?", (sid,)):
            part.append(f'<details><summary>Outreach draft {o["outreach_id"]} → {_esc(o["recipient"])}</summary>'
                        f'<p><b>{_esc(o["subject"])}</b></p><pre>{_esc(o["body"])}</pre><p class=note>{_esc(o["notes"])}</p></details>')
        detail.append("".join(part) + "</section>")
    html = f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Vending Scout sites</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#ddd;--acc:#0b57d0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--mut:#aaa;--line:#333;--acc:#8ab4f8}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
a{{color:var(--acc)}}table{{border-collapse:collapse;width:100%;display:block;overflow-x:auto;margin:8px 0}}
td,th{{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}}
.note,small{{color:var(--mut)}}.mock{{background:#ffe08a;color:#000;padding:8px;font-weight:600}}section{{margin-top:28px}}
</style><h1>Vending Scout — sites</h1>{banner}
<p class=note>Generated {vc.now_iso()} · ranked by best shift score · map links: Google Maps.
Business names and addresses come from each business's own website; Google Maps content is not stored in this report.</p>
<table><tr><th>site</th><th>name</th><th>address</th><th>status</th><th>website ↔ map</th><th>best score</th><th>map</th></tr>{''.join(rows)}</table>
{''.join(detail)}<p class=note>Map data and links © Google Maps.</p></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)


def cmd_report(a) -> None:
    con = vc.connect()
    vc.init_db(con)
    out = vc.TRACKER / "report" / "sites.html"
    write_report(con, out)
    tdir = vc.TRACKER / "tables"
    tdir.mkdir(exist_ok=True)
    for t in TABLES:
        cur = con.execute(f"SELECT * FROM {t}")
        with open(tdir / f"{t}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            w.writerows(cur.fetchall())
    print(f"Report: {out}\nCSV exports: {tdir}")


def cmd_where(a) -> None:
    print(f"Skill:        {vc.SCRIPTS.parent}\nTracker:      {vc.TRACKER}\nDatabase:     {vc.DB_PATH}\n"
          f"Config:       {vc.CONFIG_PATH}\nCSV tables:   {vc.TRACKER / 'tables'}\nReport:       {vc.TRACKER / 'report' / 'sites.html'}\n"
          f"Manifests:    {vc.TRACKER / 'manifests'}\nVerification: {vc.TRACKER / 'verification.md'}\nSecrets:      {vc.SECRETS_PATH} (outside the tracker)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("init")
    p.add_argument("--center"); p.add_argument("--radius", type=float); p.add_argument("--unit", choices=["mi", "km"])
    p.add_argument("--timezone"); p.add_argument("--budget", type=float)
    sp.add_parser("access"); sp.add_parser("check"); sp.add_parser("verify"); sp.add_parser("report"); sp.add_parser("where")
    p = sp.add_parser("run"); p.add_argument("--max-sites", type=int, default=15)
    a = ap.parse_args()
    {"init": cmd_init, "access": cmd_access, "check": cmd_check, "run": cmd_run, "verify": cmd_verify,
     "report": cmd_report, "where": cmd_where}[a.cmd](a)


if __name__ == "__main__":
    main()
