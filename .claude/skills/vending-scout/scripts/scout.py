#!/usr/bin/env python3
"""Vending Scout CLI (run from the folder that holds "Vending Scout/").

Setup     init --center "<town or address>" [--radius 20 --unit mi] --timezone <Area/City> [--budget 10]
          access                       which keys / settings are missing (never prints values)
          check                        one Places lookup, one Jev request, one Claude call -> live/docs-only/mock
Pipeline  run [--batch 10]             discover -> research -> label -> food+score -> outreach -> checklist -> report
          research | label | food | score | outreach | checklist | report      (single steps)
You       answer --site S [--shift SH] [--headcount N --source TEXT] [--cafeteria yes|no|unknown]
                 [--vending yes|no|unknown] [--access-notes TEXT]           -> updates checks, rescores
          food-access --food F --status accessible|not_accessible|unknown  -> rescores
          status --site S --to contacted|visited|closed --confirm "what you did"
Inspect   queue | verify | where
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vs_common as vc  # noqa: E402
import places  # noqa: E402
import steps  # noqa: E402
import timecalc as tc  # noqa: E402

TABLES = ["sites", "jobs", "shifts", "food", "food_searches", "checks", "results", "outreach",
          "status_log", "run_manifest", "integration_checks"]


# ================================================================ setup
def cmd_init(a):
    cfg = vc.load_config()
    for k, v in (("center_query", a.center), ("radius_value", a.radius), ("radius_unit", a.unit),
                 ("timezone", a.timezone), ("budget_usd", a.budget)):
        if v is not None:
            if k == "center_query" and v != cfg.get("center_query"):
                cfg.update(center=None)
            cfg[k] = v
    vc.save_config(cfg)
    vc.init_db(vc.connect())
    sec = vc.ensure_secrets_file()
    for d in ("report", "tables", "manifests"):
        (vc.TRACKER / d).mkdir(exist_ok=True)
    print(f"Tracker {vc.TRACKER}\nSecrets file {sec}\nSearch {cfg['radius_value']} {cfg['radius_unit']} "
          f"({vc.radius_m(cfg)} m) around {cfg['center_query']!r}, time zone {cfg['timezone']}, budget {cfg['budget_usd']}")


def missing_access(cfg):
    sec = vc.load_secrets()
    miss = [k for k in vc.SECRET_KEYS if k not in sec]
    if not cfg.get("center_query"):
        miss.append("location (town or address)")
    if cfg.get("budget_usd") is None:
        miss.append("spending limit (USD per run)")
    return miss


def cmd_access(a):
    m = missing_access(vc.load_config())
    print("Missing: " + (", ".join(m) if m else "nothing"))


def start_run(con, cfg, step, modes, extra=None):
    rid = vc.new_id(con, "run")
    c = cfg.get("center") or {}
    con.execute("INSERT INTO run_manifest(run_id, step, started_at, timezone, center_query, center_place_id, center_lat, center_lng, "
                "center_resolved_at, radius_m, batch_size, budget_usd, places_mode, jev_mode, claude_mode, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, step, vc.now_iso(cfg), cfg["timezone"], cfg["center_query"], c.get("place_id"), c.get("lat"), c.get("lng"),
                 c.get("resolved_at"), vc.radius_m(cfg), cfg["batch_size"], cfg["budget_usd"],
                 modes["places"], modes["jev"], modes["claude"], extra))
    con.commit()
    return rid


def finish_run(con, cfg, rid, spent, coverage=None, folder=None):
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    c = cfg.get("center") or {}
    con.execute("UPDATE run_manifest SET finished_at=?, spent_usd_est=?, counts=?, coverage=COALESCE(?, coverage), "
                "center_place_id=?, center_lat=?, center_lng=?, center_resolved_at=? WHERE run_id=?",
                (vc.now_iso(cfg), round(spent, 4), json.dumps(counts), json.dumps(coverage) if coverage else None,
                 c.get("place_id"), c.get("lat"), c.get("lng"), c.get("resolved_at"), rid))
    con.commit()
    row = dict(con.execute("SELECT * FROM run_manifest WHERE run_id=?", (rid,)).fetchone())
    for k in ("coverage", "counts"):
        row[k] = json.loads(row[k]) if row[k] else None
    row["integration_checks"] = [dict(r) for r in con.execute("SELECT * FROM integration_checks WHERE run_id=?", (rid,))]
    folder = folder or vc.TRACKER / "manifests"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{rid}.json").write_text(json.dumps(row, indent=2) + "\n")


def _reachable(url):
    try:
        urllib.request.urlopen(url, timeout=10)
        return "reachable"
    except urllib.error.HTTPError as e:
        return f"reachable (HTTP {e.code} without key)"
    except Exception as e:
        return f"NOT reachable ({str(e)[:90]})"


def _record(con, cfg, rid, integ, test, mode, result, docs, notes):
    con.execute("INSERT INTO integration_checks VALUES (?,?,?,?,?,?,?,?,?)",
                (vc.new_id(con, "chk"), rid, integ, test, mode, result, vc.now_iso(cfg), docs, notes))
    con.commit()
    print(f"[{mode:9}] {integ:13} {result:7} {test} — {notes}")


def cmd_check(a):
    import claude_research as cr
    import jev
    from services import MockServices
    cfg, sec, con = vc.load_config(), vc.load_secrets(), vc.connect()
    vc.init_db(con)
    modes = {"places": "live" if "GOOGLE_MAPS_API_KEY" in sec else "docs-only",
             "jev": "live" if "TYPESAFE_API_KEY" in sec else "docs-only",
             "claude": "live" if "ANTHROPIC_API_KEY" in sec else "docs-only"}
    rid, spent = start_run(con, cfg, "check", modes), 0.0
    # Places: one Text Search lookup (IDs-only field mask)
    if modes["places"] == "live":
        try:
            c = places.resolve_center(sec["GOOGLE_MAPS_API_KEY"], cfg["center_query"])
            _record(con, cfg, rid, "google_places", "Text Search lookup", "live", "pass", places.DOCS["text_search"],
                    f"place_id {c['place_id']} · {places.maps_url(c['place_id'], cfg['center_query'])}")
        except Exception as e:
            _record(con, cfg, rid, "google_places", "Text Search lookup", "live", "fail", places.DOCS["text_search"], str(e)[:300])
    else:
        _record(con, cfg, rid, "google_places", "Text Search lookup", "docs-only", "blocked", places.DOCS["text_search"],
                f"No GOOGLE_MAPS_API_KEY. Request built per docs. places.googleapis.com {_reachable(places.BASE + '/places:searchText')}.")
    _, cov = places.nearby_complete(MockServices().nearby, *services_center(), 800)
    _record(con, cfg, rid, "google_places", "Nearby 20-cap narrowing on fixtures", "mock", "pass" if cov["calls"] > 1 and cov["complete"] else "fail",
            places.DOCS["nearby_search"], f"{cov['calls']} calls, {cov['capped_calls']} capped, complete={cov['complete']}")
    # Jev: one System One request
    if modes["jev"] == "live":
        try:
            r = jev.ask(sec["TYPESAFE_API_KEY"], "Orderpicker, vaste nachtdienst 22:00-06:00, ma t/m vr, in ons magazijn.")
            _record(con, cfg, rid, "typesafe_jev", "System One request (3 Choice questions)", "live", "pass", jev.DOCS["sdk"],
                    f"model={r.model} " + json.dumps({k: v.choice for k, v in r.choices.items()}))
        except Exception as e:
            _record(con, cfg, rid, "typesafe_jev", "System One request (3 Choice questions)", "live", "fail", jev.DOCS["sdk"], str(e)[:300])
    else:
        _record(con, cfg, rid, "typesafe_jev", "System One request (3 Choice questions)", "docs-only", "blocked", jev.DOCS["sdk"],
                f"No TYPESAFE_API_KEY. Questions validated by typesafe-sdk types. api.typesafe.ai {_reachable('https://api.typesafe.ai/v1/models')}.")
    r = MockServices().jev("Orderpicker, vaste nachtdienst 22:00-06:00")
    d = jev.decide(r, {"ad_status": "active", "site_match": "confirmed"}, [{"start": "22:00", "end": "06:00"}], tc.crosses_midnight)
    _record(con, cfg, rid, "typesafe_jev", "Decision code on SDK-typed response", "mock", "pass" if d["shift"] == "overnight" and not d["review_reason"] else "fail",
            jev.DOCS["choice"], json.dumps({k: d[k] for k in ("arrangement", "work_type", "shift", "confidence", "review_reason")}))
    # Claude
    if modes["claude"] == "live":
        try:
            txt, usd = cr.Claude(sec["ANTHROPIC_API_KEY"], cfg["claude_model"]).ping()
            spent += usd
            _record(con, cfg, rid, "claude", "Messages API call", "live", "pass", cr.DOCS["messages"], f"reply={txt!r} ~${usd:.4f}")
        except Exception as e:
            _record(con, cfg, rid, "claude", "Messages API call", "live", "fail", cr.DOCS["messages"], str(e)[:300])
    else:
        _record(con, cfg, rid, "claude", "Messages API call", "docs-only", "blocked", cr.DOCS["messages"],
                f"No ANTHROPIC_API_KEY. api.anthropic.com {_reachable('https://api.anthropic.com/v1/models')}.")
    finish_run(con, cfg, rid, spent)
    print(f"Manifest: {vc.TRACKER / 'manifests' / (rid + '.json')}")
    m = missing_access(cfg)
    if m:
        print("MISSING ACCESS — stop before a real run: " + ", ".join(m))


def services_center():
    from services import MOCK_PLACES
    p = MOCK_PLACES["ChIJMOCK01"]
    return p["latitude"], p["longitude"]


# ================================================================ pipeline
def ensure_center(svc, cfg):
    c = cfg.get("center")
    if c and datetime.fromisoformat(c["resolved_at"]) > vc.now_local(cfg) - timedelta(days=30):
        return c
    c = {**svc.resolve_center(cfg["center_query"]), "query": cfg["center_query"], "resolved_at": vc.now_iso(cfg)}
    cfg["center"] = c
    return c


def pipeline(con, svc, cfg, run_folder=None, save_cfg=True, today=None):
    """Full run. Returns a summary dict. Stops cleanly on the spending limit."""
    rid = start_run(con, cfg, "run", svc.modes)
    out, cov = {"run_id": rid}, None
    try:
        center = ensure_center(svc, cfg)
        if save_cfg:
            vc.save_config(cfg)
        cov = steps.discover(con, svc, cfg, center)
        out["discover"] = {k: cov[k] for k in ("unique_within_radius", "new_sites_added", "candidates_not_added")}
        out["research"] = steps.research(con, svc, cfg)
        out["label"] = steps.label(con, svc, cfg)
        fd = steps.food(con, svc, cfg, rid, today)
        cov["food"] = fd
        out["food"] = {s: {k: v[k] for k in ("calls", "capped_calls", "unique_in_radius", "complete")} for s, v in fd.items()}
        out["score"] = steps.score(con, cfg, today)
        out["outreach"] = steps.outreach(con, svc, cfg)
    except vc.BudgetExceeded as e:
        out["stopped"] = str(e)
    finally:
        finish_run(con, cfg, rid, svc.budget.spent, cov, run_folder)
    return out


def live_services(cfg):
    from services import LiveServices
    m = missing_access(cfg)
    if m:
        sys.exit("Missing access, not running: " + ", ".join(m))
    return LiveServices(cfg, vc.load_secrets(), vc.Budget(cfg["budget_usd"]))


def cmd_run(a):
    cfg = vc.load_config()
    if a.batch:
        cfg["batch_size"] = a.batch
    svc = live_services(cfg)
    con = vc.connect()
    vc.init_db(con)
    out = pipeline(con, svc, cfg)
    print(json.dumps(out, indent=2))
    write_checklist(con, cfg, vc.TRACKER / "visit-checklist.md")
    cmd_report(a)
    print_queue(con)


def _single(step):
    def f(a):
        cfg = vc.load_config()
        con = vc.connect()
        if step == "score":
            print(steps.score(con, cfg))
            return
        svc = live_services(cfg)
        rid = start_run(con, cfg, step, svc.modes)
        try:
            r = {"research": lambda: steps.research(con, svc, cfg), "label": lambda: steps.label(con, svc, cfg),
                 "food": lambda: steps.food(con, svc, cfg, rid), "outreach": lambda: steps.outreach(con, svc, cfg)}[step]()
            print(json.dumps(r, indent=2, default=str))
        finally:
            finish_run(con, cfg, rid, svc.budget.spent)
    return f


# ================================================================ user actions
def cmd_answer(a):
    cfg, con = vc.load_config(), vc.connect()
    print(steps.answer(con, cfg, a.site, a.shift, a.headcount, a.source, a.cafeteria, a.vending, a.access_notes))
    print_queue(con)


def cmd_food_access(a):
    cfg, con = vc.load_config(), vc.connect()
    print(steps.food_access(con, cfg, a.food, a.status, a.note))


def cmd_status(a):
    cfg, con = vc.load_config(), vc.connect()
    cur = con.execute("SELECT status FROM sites WHERE site_id=?", (a.site,)).fetchone()[0]
    if a.to == "visited" and cur not in ("contacted", "draft_ready", "researched", "review"):
        sys.exit(f"{a.site} is {cur}; visited needs a researched/contacted site")
    if a.to == "contacted" and cur not in ("draft_ready", "researched", "review"):
        sys.exit(f"{a.site} is {cur}; contacted needs a draft_ready/researched site")
    steps.set_status(con, cfg, a.site, a.to, "user", a.confirm)
    con.commit()
    print(f"{a.site}: {cur} -> {a.to} ({a.confirm})")


# ================================================================ queues, checklist, report
def queues(con):
    q = lambda s: [dict(r) for r in con.execute(s)]
    return {
        "status_counts": dict(con.execute("SELECT status, COUNT(*) FROM sites GROUP BY status").fetchall()),
        "sites": q("SELECT s.site_id, s.status, s.name, s.distance_m, "
                   "(SELECT COUNT(*) FROM jobs j WHERE j.site_id=s.site_id AND ad_status='active') active_ads, "
                   "(SELECT COUNT(*) FROM jobs j WHERE j.site_id=s.site_id AND ad_status='stale') stale_ads, "
                   "(SELECT COUNT(*) FROM jobs j WHERE j.site_id=s.site_id AND ad_status='unavailable') unavailable, "
                   "(SELECT COUNT(*) FROM shifts sh WHERE sh.site_id=s.site_id) shifts FROM sites s ORDER BY site_id"),
        "review_sites": q("SELECT site_id, notes FROM sites WHERE status='review' ORDER BY site_id"),
        "review_jobs": q("SELECT job_id, site_id, ad_status, site_match, arrangement, work_type, shift, confidence, "
                         "COALESCE(review_reason, label_error) reason FROM jobs WHERE review_reason IS NOT NULL "
                         "OR label_error IS NOT NULL ORDER BY job_id"),
        "scored": q("SELECT site_id, job_id, shift_id, dead_hours, score, score_status FROM results "
                    "WHERE score IS NOT NULL ORDER BY score DESC"),
        "needs_verification": q("SELECT site_id, job_id, shift_id, dead_hours, missing_checks FROM results "
                                "WHERE score_status='needs_verification' ORDER BY site_id, shift_id"),
        "labels": q("SELECT COUNT(*) excerpts, SUM(arrangement IS NOT NULL) labelled, SUM(label_error IS NOT NULL) failed "
                    "FROM jobs WHERE excerpt IS NOT NULL AND excerpt<>''")[0],
        "drafts": q("SELECT outreach_id, site_id, recipient, word_count, shift_id_referenced, contact_source FROM outreach ORDER BY outreach_id"),
    }


def print_queue(con):
    Q = queues(con)
    print("\nSite status counts:", Q["status_counts"])
    for s in Q["sites"]:
        print(f"  {s['site_id']} {s['status']:11} active {s['active_ads']} stale {s['stale_ads']} unavailable {s['unavailable']} "
              f"shifts {s['shifts']}  {s['name'] or '(name unverified)'}")
    L = Q["labels"]
    print(f"\nJev: {L['labelled']}/{L['excerpts']} excerpts labelled, {L['failed']} recorded failures")
    print("\nReview queue (sites):", *[f"\n  {r['site_id']}: {r['notes']}" for r in Q["review_sites"]] or [" none"])
    print("Review queue (jobs):", *[f"\n  {r['job_id']} ({r['site_id']}) {r['shift']}/{r['arrangement']}/{r['work_type']}: {r['reason']}"
                                   for r in Q["review_jobs"]] or [" none"])
    print("\nScored (worker-hours per shift):", *[f"\n  {r['site_id']} {r['shift_id']} score {r['score']} ({r['score_status']}, dead {r['dead_hours']} h)"
                                                  for r in Q["scored"]] or [" none"])
    print("Needs verification:", *[f"\n  {r['site_id']} {r['shift_id']}: {r['missing_checks']}" for r in Q["needs_verification"]] or [" none"])
    print(f"\nDrafts: {len(Q['drafts'])}", *[f"\n  {d['outreach_id']} {d['site_id']} to {d['recipient'] or '(contact page)'} "
                                             f"{d['word_count']} words, source {d['contact_source']}" for d in Q["drafts"]])


def write_checklist(con, cfg, path):
    L = ["# Visit checklist", "", f"Generated {vc.now_iso(cfg)}. Confirm these BEFORE buying a machine. "
         "Then record answers with `scout.py answer ...` (scores update automatically).", ""]
    for s in con.execute("SELECT * FROM sites WHERE status IN ('researched','draft_ready','contacted','visited') ORDER BY site_id"):
        sid = s["site_id"]
        L += [f"## {sid} · {s['name']}", f"{s['address']} · [Google Maps]({places.maps_url(s['place_id'], s['name'] or '')})", ""]
        for sh in con.execute("SELECT * FROM shifts WHERE site_id=? ORDER BY shift_id", (sid,)):
            L.append(f"- [ ] **Night staffing, {sh['shift_id']}** ({sh['start'] or '?'}–{sh['end'] or '?'} {sh['days'] or ''}): "
                     f"how many people actually work on site during this shift? Who confirmed it? "
                     f"→ `answer --site {sid} --shift {sh['shift_id']} --headcount N --source \"who/when\"`")
        L += [f"- [ ] **Break-room access**: is there a break room night staff can use, and could a machine stand there (space, power, who allows it)? → `--access-notes`",
              f"- [ ] **Cafeteria hours**: is there a canteen, and is it open during night shifts (which days/hours)? → `--cafeteria yes|no`",
              f"- [ ] **Existing vending contracts**: is there a vending operator now? Contract end date, exclusivity? → `--vending yes|no`"]
        fr = con.execute("SELECT food_id, distance_m, access_status FROM food WHERE site_id=? ORDER BY distance_m", (sid,)).fetchall()
        if fr:
            L.append(f"- [ ] **Walking access to nearby food** (can night staff actually walk there on a break?): " +
                     ", ".join(f"{f['food_id']} {f['distance_m']} m ({f['access_status']})" for f in fr) +
                     " → `food-access --food food-NNN --status accessible|not_accessible`")
        L.append("")
    path.write_text("\n".join(L) + "\n")
    return path


def _e(v):
    import html
    return "" if v is None else html.escape(str(v))


def write_report(con, path, mock=False):
    Q = queues(con)
    best = {r[0]: r[1] for r in con.execute("SELECT site_id, MAX(score) FROM results GROUP BY site_id")}
    sites = sorted(con.execute("SELECT * FROM sites").fetchall(), key=lambda s: (-(best.get(s["site_id"]) or -1), s["site_id"]))
    maps = lambda pid, lab="": (f'<a href="{_e(places.maps_url(pid, lab) if pid else places.maps_search_url(lab))}" target=_blank rel=noopener>Google Maps</a>'
                                if (pid or lab) else "")
    T = lambda head, rows: "<table><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>" + "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"
    a = lambda i: f'<a href="#{i}">{i}</a>'
    parts = [T(["site", "name", "status", "distance", "best score", "map"],
               [[a(s["site_id"]), _e(s["name"]) or "<i>unverified</i>", s["status"], f'{_e(s["distance_m"])} m',
                 _e(best.get(s["site_id"])), maps(s["place_id"], " ".join(x for x in (s["name"], s["address"]) if x))] for s in sites])]
    parts.append("<h2>Scored shifts</h2><p class=note>Score = verified night headcount × dead hours (worker-hours per shift). "
                 "Ranks research priority, not earnings.</p>" +
                 T(["site", "shift", "dead h", "score", "status"], [[a(r["site_id"]), a(r["shift_id"]), r["dead_hours"], r["score"], r["score_status"]]
                                                                    for r in Q["scored"]]))
    parts.append("<h2>Needs verification</h2>" + T(["site", "shift", "dead h", "missing"],
                 [[a(r["site_id"]), a(r["shift_id"]), _e(r["dead_hours"]), _e(r["missing_checks"])] for r in Q["needs_verification"]]))
    parts.append("<h2>Review queue</h2>" + T(["item", "site", "reason"],
                 [[a(r["site_id"]), "", _e(r["notes"])] for r in Q["review_sites"]] +
                 [[a(r["job_id"]), a(r["site_id"]), _e(r["reason"])] for r in Q["review_jobs"]]))
    for s in sites:
        sid = s["site_id"]
        src = f'<a href="{_e(s["source_url"])}">employer website</a>' if s["source_url"] else "identity not verified"
        p = [f'<section id="{sid}"><h2>{sid} · {_e(s["name"]) or "<i>name unverified</i>"} <small>[{s["status"]}]</small></h2>'
             f'<p>{_e(s["address"])} — {src} · {maps(s["place_id"], " ".join(x for x in (s["name"], s["address"]) if x))}</p><p class=note>{_e(s["notes"])}</p>']
        jobs = con.execute("SELECT * FROM jobs WHERE site_id=? ORDER BY job_id", (sid,)).fetchall()
        lab = {}
        for j in jobs:
            if j["shift"]:
                lab[j["shift"]] = lab.get(j["shift"], 0) + 1
        if lab:
            p.append("<p class=note>Ad labels at this site (counts of ads, not headcount): " + ", ".join(f"{k} {v}" for k, v in lab.items()) + "</p>")
        p.append(T(["job", "status", "match", "excerpt", "labels", "conf", "review"],
                   [[f'<a id="{j["job_id"]}" href="{_e(j["source_url"])}">{j["job_id"]}</a>', j["ad_status"], _e(j["site_match"]),
                     _e(j["excerpt"]), f'{_e(j["arrangement"])}/{_e(j["work_type"])}/{_e(j["shift"])}', _e(j["confidence"]),
                     _e(j["review_reason"] or j["label_error"] or j["notes"] if j["ad_status"] == "unavailable" else j["review_reason"] or j["label_error"])]
                    for j in jobs]))
        rows = con.execute('SELECT sh.*, c.verified_night_headcount hc, c.entered_by, r.dead_hours, r.score, r.score_status, r.missing_checks '
                           'FROM shifts sh LEFT JOIN checks c USING(site_id, job_id, shift_id) LEFT JOIN results r USING(site_id, job_id, shift_id) '
                           'WHERE sh.site_id=? ORDER BY shift_id', (sid,)).fetchall()
        p.append(T(["shift", "job", "start", "end", "days", "night staff", "dead h", "score", "status / missing"],
                   [[f'<span id="{r["shift_id"]}">{r["shift_id"]}</span>', a(r["job_id"]), _e(r["start"]), _e(r["end"]), _e(r["days"]),
                     _e(r["hc"]), _e(r["dead_hours"]), _e(r["score"]), f'{_e(r["score_status"])}<br><small>{_e(r["missing_checks"])}</small>']
                    for r in rows]))
        fr = con.execute("SELECT * FROM food WHERE site_id=? ORDER BY distance_m", (sid,)).fetchall()
        if fr:
            p.append("<p>Food ≤ 800 m: " + "; ".join(
                f'{f["food_id"]} {f["distance_m"]} m {maps(f["place_id"])} hours: '
                f'{"<a href=" + chr(34) + _e(f["source_url"]) + chr(34) + ">verified</a>" if f["source_url"] else "unknown"}, access {f["access_status"]}'
                for f in fr) + "</p>")
        for o in con.execute("SELECT * FROM outreach WHERE site_id=?", (sid,)):
            p.append(f'<details><summary>Draft {o["outreach_id"]} → {_e(o["recipient"]) or "contact page"} ({o["word_count"]} words)</summary>'
                     f'<p><b>{_e(o["subject"])}</b></p><pre>{_e(o["body"])}</pre><p class=note>{_e(o["notes"])} · source {_e(o["contact_source"])}</p></details>')
        parts.append("".join(p) + "</section>")
    banner = "<p class=mock>MOCK DATA — fictional rows that test the pipeline. Not real businesses.</p>" if mock else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Vending Scout sites</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#ddd;--acc:#0b57d0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--mut:#aaa;--line:#333;--acc:#8ab4f8}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
a{{color:var(--acc)}}table{{border-collapse:collapse;width:100%;display:block;overflow-x:auto;margin:8px 0}}
td,th{{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}}
.note,small{{color:var(--mut)}}.mock{{background:#ffe08a;color:#000;padding:8px;font-weight:600}}section{{margin-top:28px}}pre{{white-space:pre-wrap}}
</style><h1>Vending Scout — sites</h1>{banner}
<p class=note>Generated {vc.now_iso()} · names/addresses verified on employer websites; Google Maps content is linked, not stored.</p>
{''.join(parts)}<p class=note>Map links: Google Maps.</p></html>""")


def cmd_report(a=None):
    con = vc.connect()
    vc.init_db(con)
    write_report(con, vc.TRACKER / "report" / "sites.html")
    t = vc.TRACKER / "tables"
    t.mkdir(exist_ok=True)
    for name in TABLES:
        cur = con.execute(f"SELECT * FROM {name}")
        with open(t / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            w.writerows(cur.fetchall())
    print(f"Report {vc.TRACKER / 'report' / 'sites.html'} · CSV {t}")


def cmd_checklist(a):
    print(write_checklist(vc.connect(), vc.load_config(), vc.TRACKER / "visit-checklist.md"))


# ================================================================ manual import
def cmd_import(a):
    """Add sites/jobs/shifts found by hand or by Claude in chat (no paid APIs).
    JSON: {"evidence": "...", "sites": [{"key", "name", "address", "source_url", "status": "found|review",
           "town", "approx_distance_km", "notes", "jobs": [{"source_url", "ad_status", "site_match", "excerpt",
           "notes", "suggested_labels", "schedules": [{"start","end","days","source_quote"}]}]}]}
    Re-importing the same key updates the row and keeps its IDs."""
    cfg, con = vc.load_config(), vc.connect()
    vc.init_db(con)
    data = json.loads(Path(a.file).read_text())
    rid = start_run(con, cfg, "manual-import", {"places": "docs-only", "jev": "docs-only", "claude": "docs-only"},
                    data.get("evidence"))
    for s in data["sites"]:
        key = f"manual:{s['key']}"
        row = con.execute("SELECT site_id, status FROM sites WHERE place_id=?", (key,)).fetchone()
        note = " | ".join(x for x in [s.get("notes"), f"approx. {s['approx_distance_km']} km from {cfg['center_query']} (town centres; exact distance needs geocoding)"
                                      if s.get("approx_distance_km") is not None else None, "entered manually: no Google place_id"] if x)
        if row:
            sid = row[0]
            con.execute("UPDATE sites SET name=?, address=?, source_url=?, notes=? WHERE site_id=?",
                        (s.get("name"), s.get("address"), s.get("source_url"), note, sid))
            if row[1] in ("found", "review") and s.get("status") and s["status"] != row[1]:
                steps.set_status(con, cfg, sid, s["status"], "research", "manual import")
        else:
            sid = vc.new_id(con, "site")
            con.execute("INSERT INTO sites(site_id, place_id, name, address, source_url, match_status, status, found_by, notes) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (sid, key, s.get("name"), s.get("address"), s.get("source_url"), "unverified",
                                                      s.get("status", "found"), "manual", note))
            con.execute("INSERT INTO status_log VALUES (?,?,?,?,?,?,NULL)", (sid, vc.now_iso(cfg), None, s.get("status", "found"), "research", "manual import"))
        for j in s.get("jobs", []):
            ex = con.execute("SELECT job_id FROM jobs WHERE site_id=? AND source_url=?", (sid, j["source_url"])).fetchone()
            jn = " | ".join(x for x in [j.get("notes"), ("labels suggested by Claude in chat (not Jev): " + json.dumps(j["suggested_labels"]))
                                        if j.get("suggested_labels") else None] if x)
            sch = j.get("schedules", [])
            one = sch[0] if len(sch) == 1 else {}
            vals = (vc.now_iso(cfg), j.get("ad_status"), j.get("site_match"), j.get("excerpt"), one.get("start"), one.get("end"), one.get("days"), jn)
            if ex:
                jid = ex[0]
                con.execute("UPDATE jobs SET checked_at=?, ad_status=?, site_match=?, excerpt=?, shift_start=?, shift_end=?, shift_days=?, notes=? "
                            "WHERE job_id=?", (*vals, jid))
            else:
                jid = vc.new_id(con, "job")
                con.execute("INSERT INTO jobs(job_id, site_id, source_url, checked_at, ad_status, site_match, excerpt, shift_start, shift_end, "
                            "shift_days, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (jid, sid, j["source_url"], *vals))
            if j.get("excerpt"):
                con.execute("UPDATE jobs SET label_error=?, review_reason=? WHERE job_id=? AND arrangement IS NULL",
                            ("Jev not run: no TYPESAFE_API_KEY (labels blank on purpose)", "awaiting Jev labels", jid))
            for sc in sch:
                k = (sc.get("start"), sc.get("end"), sc.get("days"))
                e2 = con.execute('SELECT shift_id FROM shifts WHERE job_id=? AND start IS ? AND "end" IS ? AND days IS ?', (jid, *k)).fetchone()
                if e2:
                    shid = e2[0]
                else:
                    shid = vc.new_id(con, "shift")
                    miss = [n for n, v in zip(("start", "end", "days"), k) if not v]
                    con.execute("INSERT INTO shifts VALUES (?,?,?,?,?,?,?,?)", (shid, jid, sid, *k, sc.get("source_quote"),
                                f"source does not state {', '.join(miss)}" if miss else None))
                con.execute("INSERT OR IGNORE INTO checks(site_id, job_id, shift_id, entered_by, notes) VALUES (?,?,?,'auto',?)",
                            (sid, jid, shid, "awaiting independent verification (visit or employer confirmation)"))
    con.commit()
    print(steps.score(con, cfg))
    finish_run(con, cfg, rid, 0.0)
    print_queue(con)


# ================================================================ verification
def join_checks(con):
    q = lambda s: con.execute(s).fetchone()[0]
    return [
        ("foreign keys", q("SELECT COUNT(*) FROM pragma_foreign_key_check") == 0),
        ("every job links to an existing site", q("SELECT COUNT(*) FROM jobs j LEFT JOIN sites s USING(site_id) WHERE s.site_id IS NULL") == 0),
        ("shift site = its job's site", q("SELECT COUNT(*) FROM shifts sh JOIN jobs j USING(job_id) WHERE j.site_id<>sh.site_id") == 0),
        ("every shift appears in the joined view", q("SELECT COUNT(*) FROM shifts") == q("SELECT COUNT(*) FROM v_site_shift")),
        ("every shift has a check row", q("SELECT COUNT(*) FROM shifts sh LEFT JOIN checks c USING(site_id, job_id, shift_id) WHERE c.shift_id IS NULL") == 0),
        ("results keyed to a check", q("SELECT COUNT(*) FROM results r LEFT JOIN checks c USING(site_id, job_id, shift_id) WHERE c.site_id IS NULL") == 0),
        ("researched+ sites have verified identity", q("SELECT COUNT(*) FROM sites WHERE status IN ('researched','draft_ready') AND (name IS NULL OR source_url IS NULL)") == 0),
        ("draft_ready sites have a saved draft", q("SELECT COUNT(*) FROM sites s LEFT JOIN outreach o USING(site_id) WHERE s.status='draft_ready' AND o.site_id IS NULL") == 0),
        ("blank scores list a missing check or reason", q("SELECT COUNT(*) FROM results WHERE score IS NULL AND COALESCE(missing_checks, notes) IS NULL") == 0),
        ("every excerpt is labelled or has a recorded failure",
         q("SELECT COUNT(*) FROM jobs WHERE excerpt<>'' AND arrangement IS NULL AND label_error IS NULL") == 0),
        ("unknown labels are in review", q("SELECT COUNT(*) FROM jobs WHERE 'unknown' IN (arrangement, work_type, shift) AND review_reason IS NULL") == 0),
        ("drafts under 90 words", q("SELECT COUNT(*) FROM outreach WHERE word_count >= 90") == 0),
    ]


def synthetic_tests():
    """The user's required examples, plus DST/holiday/midnight cases (Europe/Amsterdam)."""
    T, tz = [], "Europe/Amsterdam"
    H = lambda o, c: {"weekly": [{"day": d, "open": o, "close": c} for d in tc.DAYS], "holidays_same_as_regular": True}
    mon = date(2026, 11, 30)
    r = tc.shift_dead_hours("22:00", "06:00", "Mon", [H("06:00", "00:00")], tz, True, mon, 1)
    T.append(("80 people x 6 dead hours = 480", r["dead_hours"] == 6.0 and 80 * r["dead_hours"] == 480, f"dead={r['dead_hours']}, score={80 * (r['dead_hours'] or 0)}"))
    r = tc.shift_dead_hours("22:00", "06:00", "Mon", [{"always_open": True, "holidays_same_as_regular": True}], tz, True, mon, 1)
    T.append(("accessible 24-hour option gives 0 dead hours", r["dead_hours"] == 0.0, f"dead={r['dead_hours']}"))
    r = tc.shift_dead_hours("22:00", "06:00", "Mon", [H("20:00", "23:00"), H("22:00", "01:00")], tz, True, mon, 1)
    T.append(("overlapping intervals not double-counted (open 22-01 = 3 h → 5 dead)", r["dead_hours"] == 5.0, f"dead={r['dead_hours']}"))
    r = tc.shift_dead_hours("22:00", "06:00", "Mon", [H("06:00", "00:00"), None], tz, True, mon, 1)
    T.append(("unknown hours → dead hours unknown, not 0", r["dead_hours"] is None, f"dead={r['dead_hours']} ({r['why']})"))
    r = tc.shift_dead_hours("22:00", "06:00", "Mon", [H("06:00", "00:00")], tz, False, mon, 1)
    T.append(("incomplete food coverage → unknown", r["dead_hours"] is None, f"dead={r['dead_hours']}"))
    r = tc.shift_dead_hours("22:00", "06:00", "Sat", [], tz, True, date(2027, 3, 27), 1)
    T.append(("DST spring-forward night is 7 h (2027-03-27/28)", r["occurrences"][0]["shift_hours"] == 7.0, str(r["occurrences"][0])))
    r = tc.shift_dead_hours("22:00", "06:00", "Sat", [], tz, True, date(2026, 10, 24), 1)
    T.append(("DST fall-back night is 9 h (2026-10-24/25)", r["occurrences"][0]["shift_hours"] == 9.0, str(r["occurrences"][0])))
    wk = {"weekly": H("06:00", "00:00")["weekly"]}  # no holiday hours verified
    r = tc.shift_dead_hours("22:00", "06:00", "Tue,Fri", [wk], tz, True, date(2026, 12, 22), 4)
    T.append(("Christmas Day occurrence without verified holiday hours is excluded and listed",
              r["occurrences"][1]["holiday"] == "Eerste Kerstdag" and r["dead_hours"] == 6.0, json.dumps(r["occurrences"])))
    r = tc.shift_dead_hours("22:00", "06:00", "Thu", [wk], tz, True, date(2026, 12, 24), 1)
    T.append(("night of 24 Dec crossing into Christmas Day → unknown, not guessed", r["dead_hours"] is None, json.dumps(r["occurrences"])))
    T.append(("midnight rule: 22-06 overnight, 14-00 evening, 07-15 day",
              tc.crosses_midnight("22:00", "06:00") and not tc.crosses_midnight("14:00", "00:00") and not tc.crosses_midnight("07:00", "15:00"), ""))
    return T


def cmd_verify(a):
    from services import MockServices
    cfg = vc.load_config()
    L = [f"# Vending Scout verification — {vc.now_iso(cfg)}", "", "## Synthetic examples (timecalc, mode: mock)"]
    for name, ok, info in synthetic_tests():
        L.append(f"- {'PASS' if ok else 'FAIL'} {name} — {info}")
    with tempfile.TemporaryDirectory() as td:
        mcfg = {**cfg, "center_query": "MOCK center", "center": None, "site_queries": ["warehouse", "distributiecentrum", "fabriek", "ziekenhuis"],
                "batch_size": 10, "timezone": cfg["timezone"] or "Europe/Amsterdam", "budget_usd": 1.0}
        con = vc.connect(Path(td) / "mock.db")
        vc.init_db(con)
        today = date(2026, 11, 30)
        out = pipeline(con, MockServices(), mcfg, Path(td) / "m", save_cfg=False, today=today)
        L += ["", "## Full pipeline on fictional sites (mode: mock)", "```", json.dumps(out, indent=1, default=str), "```"]
        Q = queues(con)
        L.append(f"- Site counts (all {len(Q['sites'])}): " + json.dumps(Q["status_counts"]))
        L.append(f"- Jev: {Q['labels']['labelled']}/{Q['labels']['excerpts']} excerpts labelled, {Q['labels']['failed']} failures recorded")
        L.append(f"- Review queue: {len(Q['review_sites'])} sites, {len(Q['review_jobs'])} jobs")
        # user answers -> rescore
        s1 = con.execute("SELECT site_id FROM sites WHERE place_id='ChIJMOCK01'").fetchone()[0]
        night = con.execute("SELECT shift_id FROM shifts WHERE site_id=? AND start='22:00'", (s1,)).fetchone()[0]
        steps.answer(con, mcfg, s1, night, 80, "MOCK: shift lead on visit", "no", "no")
        for f in con.execute("SELECT food_id FROM food WHERE site_id=? AND opening_periods IS NOT NULL", (s1,)).fetchall():
            steps.food_access(con, mcfg, f[0], "accessible")
        r = con.execute("SELECT * FROM results WHERE shift_id=?", (night,)).fetchone()
        L.append(f"- PASS? pipeline 80 x {r['dead_hours']} = {r['score']} ({r['score_status']})" .replace(
            "PASS?", "PASS" if r["score"] == 480 and r["score_status"] == "scored" else "FAIL"))
        s10 = con.execute("SELECT site_id FROM sites WHERE place_id='ChIJMOCK10'").fetchone()[0]
        sh10 = con.execute("SELECT shift_id FROM shifts WHERE site_id=?", (s10,)).fetchone()[0]
        steps.answer(con, mcfg, s10, sh10, 25, "MOCK: HR email")
        for f in con.execute("SELECT food_id FROM food WHERE site_id=?", (s10,)).fetchall():
            steps.food_access(con, mcfg, f[0], "accessible")
        r = con.execute("SELECT * FROM results WHERE shift_id=?", (sh10,)).fetchone()
        L.append(f"- {'PASS' if r['dead_hours'] == 0 and r['score'] == 0 else 'FAIL'} pipeline 24-hour venue → dead 0, score {r['score']}")
        # status rules
        steps.set_status(con, mcfg, s1, "contacted", "user", "MOCK: emailed")
        steps.set_status(con, mcfg, s10, "closed", "user", "MOCK: not interested")
        try:
            steps.set_status(con, mcfg, s10, "researched", "research", "auto")
            L.append("- FAIL closed site reopened")
        except ValueError:
            L.append("- PASS closed site is never reopened automatically")
        # rerun: IDs, human checks, statuses preserved; unchanged results reused; closed skipped
        before = {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in ("sites", "shifts")}
        ids_before = {r[0] for r in con.execute("SELECT job_id FROM jobs")}
        human = [tuple(r) for r in con.execute("SELECT * FROM checks WHERE entered_by='user' ORDER BY shift_id")]
        out2 = pipeline(con, MockServices(), mcfg, Path(td) / "m", save_cfg=False, today=today)
        L.append(f"- {'PASS' if {r[0] for r in con.execute('SELECT job_id FROM jobs')} == ids_before else 'FAIL'} rerun keeps job IDs")
        L.append(f"- {'PASS' if [tuple(r) for r in con.execute('SELECT * FROM shifts ORDER BY 1')] == before['shifts'] else 'FAIL'} rerun keeps shift IDs")
        L.append(f"- {'PASS' if [tuple(r) for r in con.execute('SELECT * FROM checks WHERE entered_by=? ORDER BY shift_id', ('user',))] == human else 'FAIL'} rerun keeps human-entered checks")
        st = dict(con.execute("SELECT site_id, status FROM sites").fetchall())
        L.append(f"- {'PASS' if st[s1] == 'contacted' and st[s10] == 'closed' else 'FAIL'} rerun keeps contacted/closed")
        L.append(f"- {'PASS' if out2['label']['reused'] == out2['label']['excerpts'] else 'FAIL'} rerun reuses unchanged Jev labels ({out2['label']})")
        L.append(f"- {'PASS' if out2['score']['computed'] == 0 else 'FAIL'} rerun reuses unchanged results ({out2['score']})")
        L.append(f"- {'PASS' if s10 not in out2['research'] else 'FAIL'} rerun skips closed site")
        L += ["", "## Join + integrity checks (mock tracker)"]
        L += [f"- {'PASS' if ok else 'FAIL'} {n}" for n, ok in join_checks(con)]
        for label, sql in [("reject check for unknown shift", "INSERT INTO checks(site_id, job_id, shift_id) VALUES ('site-001','job-001','shift-999')"),
                           ("reject shift whose site differs from its job", "INSERT INTO shifts(shift_id, job_id, site_id) VALUES ('shift-900','job-001','site-002')"),
                           ("reject headcount without source", "UPDATE checks SET verified_night_headcount=5, headcount_source=NULL WHERE entered_by='auto'"),
                           ("reject draft of 90+ words", "UPDATE outreach SET word_count=95")]:
            try:
                con.execute(sql)
                L.append(f"- FAIL {label}")
            except sqlite3.IntegrityError:
                L.append(f"- PASS {label}")
        L += ["", "### Status log (mock)"] + [f"- {r['site_id']} {r['from_status']} → {r['to_status']} by {r['changed_by']}: {r['reason']}"
                                             for r in con.execute("SELECT * FROM status_log ORDER BY rowid")]
        write_report(con, vc.TRACKER / "report" / "sample-MOCK-report.html", mock=True)
        write_checklist(con, mcfg, vc.TRACKER / "report" / "sample-MOCK-visit-checklist.md")
        con.close()
    if vc.DB_PATH.exists():
        con = vc.connect()
        L += ["", "## Real tracker"] + [f"- {'PASS' if ok else 'FAIL'} {n}" for n, ok in join_checks(con)]
    p = vc.TRACKER / "verification.md"
    p.write_text("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\nSaved {p}")


def cmd_where(a):
    t = vc.TRACKER
    print(f"Skill        {vc.SCRIPTS.parent}\nTracker      {t}\nDatabase     {vc.DB_PATH}\nConfig       {vc.CONFIG_PATH}\n"
          f"CSV tables   {t / 'tables'}\nReport       {t / 'report' / 'sites.html'}\nChecklist    {t / 'visit-checklist.md'}\n"
          f"Manifests    {t / 'manifests'}\nVerification {t / 'verification.md'}\nSecrets      {vc.SECRETS_PATH} (outside the tracker)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("init")
    p.add_argument("--center"); p.add_argument("--radius", type=float); p.add_argument("--unit", choices=["mi", "km"])
    p.add_argument("--timezone"); p.add_argument("--budget", type=float)
    for n in ("access", "check", "verify", "report", "where", "queue", "checklist", "research", "label", "food", "score", "outreach"):
        sp.add_parser(n)
    p = sp.add_parser("run"); p.add_argument("--batch", type=int)
    p = sp.add_parser("import"); p.add_argument("file")
    p = sp.add_parser("answer")
    p.add_argument("--site", required=True); p.add_argument("--shift"); p.add_argument("--headcount", type=int); p.add_argument("--source")
    p.add_argument("--cafeteria", choices=["yes", "no", "unknown"]); p.add_argument("--vending", choices=["yes", "no", "unknown"])
    p.add_argument("--access-notes")
    p = sp.add_parser("food-access"); p.add_argument("--food", required=True)
    p.add_argument("--status", required=True, choices=["accessible", "not_accessible", "unknown"]); p.add_argument("--note")
    p = sp.add_parser("status"); p.add_argument("--site", required=True)
    p.add_argument("--to", required=True, choices=["contacted", "visited", "closed"]); p.add_argument("--confirm", required=True)
    a = ap.parse_args()
    {"init": cmd_init, "access": cmd_access, "check": cmd_check, "run": cmd_run, "verify": cmd_verify, "report": cmd_report,
     "where": cmd_where, "queue": lambda a: print_queue(vc.connect()), "checklist": cmd_checklist,
     "research": _single("research"), "label": _single("label"), "food": _single("food"), "score": _single("score"),
     "outreach": _single("outreach"), "answer": cmd_answer, "food-access": cmd_food_access, "status": cmd_status, "import": cmd_import}[a.cmd](a)


if __name__ == "__main__":
    main()
