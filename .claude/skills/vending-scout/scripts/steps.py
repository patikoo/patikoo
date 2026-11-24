"""Pipeline steps. Each step owns specific fields and never overwrites the others:

discover  -> sites (place_id, distance_m, found_by), run_manifest coverage
research  -> sites identity + status found/researched/review; jobs source fields; shifts; blank checks
label     -> jobs labels, confidence, review_reason, distributions (Jev)
food      -> food, food_searches, results (dead hours + score)
score     -> results only (recalculated from checks + stored food)
outreach  -> outreach; status draft_ready
user      -> checks answers, food access, contacted/visited/closed
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

import vs_common as vc
import places
import timecalc as tc

AUTO_FROM = {"found", "review", "researched"}       # statuses research may move
LOCKED = {"contacted", "visited", "closed"}           # only the user moves these


def set_status(con, cfg, site_id: str, to: str, by: str, reason: str) -> bool:
    cur = con.execute("SELECT status FROM sites WHERE site_id=?", (site_id,)).fetchone()[0]
    if cur == to:
        return False
    if cur == "closed":
        raise ValueError(f"{site_id} is closed; it is never reopened automatically")
    if by != "user" and cur in LOCKED:
        return False
    con.execute("UPDATE sites SET status=? WHERE site_id=?", (to, site_id))
    con.execute("INSERT INTO status_log VALUES (?,?,?,?,?,?,NULL)", (site_id, vc.now_iso(cfg), cur, to, by, reason))
    return True


# ------------------------------------------------------------------ 1. discover
def discover(con, svc, cfg, center: dict) -> dict:
    lat, lng, rad = center["lat"], center["lng"], vc.radius_m(cfg)
    cov = {"center": center, "radius_m": rad, "method": "Places Text Search, bbox restriction, all pages up to 3, "
           "dedup by place_id, straight-line distance <= radius", "queries": [], "note":
           "Searches are candidate discovery, not a complete census."}
    candidates: dict[str, dict] = {}
    for q in cfg["site_queries"]:
        res, pages, more = svc.text_search(q, lat, lng, rad)
        inside = 0
        for p in res:
            loc = p.get("location")
            if not loc:
                continue
            d = places.distance_m(lat, lng, loc["latitude"], loc["longitude"])
            if d <= rad:
                inside += 1
                candidates.setdefault(p["id"], {"distance_m": d, "query": q, "order": len(candidates)})
        cov["queries"].append({"query": q, "pages": pages, "results": len(res), "within_radius": inside,
                               "more_pages_not_fetched": more})
    cov["unique_within_radius"] = len(candidates)
    # refresh derived distance on known sites; add new ones up to the batch size, mixing queries
    for pid, c in candidates.items():
        con.execute("UPDATE sites SET distance_m=? WHERE place_id=?", (c["distance_m"], pid))
    existing = {r[0] for r in con.execute("SELECT place_id FROM sites")}
    new = [pid for pid in candidates if pid not in existing]
    by_q: dict[str, list] = {}
    for pid in new:
        by_q.setdefault(candidates[pid]["query"], []).append(pid)
    for lst in by_q.values():
        lst.sort(key=lambda p: candidates[p]["distance_m"])
    ordered = []
    while any(by_q.values()):
        for q in list(by_q):
            if by_q[q]:
                ordered.append(by_q[q].pop(0))
    added = []
    for pid in ordered[: cfg["batch_size"]]:
        sid = vc.new_id(con, "site")
        con.execute("INSERT INTO sites(site_id, place_id, match_status, status, distance_m, found_by, notes) VALUES (?,?,?,?,?,?,?)",
                    (sid, pid, "unverified", "found", candidates[pid]["distance_m"], candidates[pid]["query"],
                     "name/address blank until verified on the employer's own website"))
        con.execute("INSERT INTO status_log VALUES (?,?,?,?,?,?,NULL)", (sid, vc.now_iso(cfg), None, "found", "research", "discovered"))
        added.append(sid)
    cov["new_sites_added"] = added
    cov["candidates_not_added"] = len(ordered) - len(added)
    con.commit()
    return cov


# ------------------------------------------------------------------ 2. research
def _match(a, b):
    norm = lambda s: {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in s).split() if len(w) > 2}
    if not a or not b:
        return "unverified"
    return "matched" if norm(a) & norm(b) else "mismatch"


def _unavailable(con, cfg, site_id, url, reason):
    url = url or f"unavailable:{site_id}"
    if con.execute("SELECT 1 FROM jobs WHERE site_id=? AND source_url=?", (site_id, url)).fetchone():
        con.execute("UPDATE jobs SET checked_at=?, notes=? WHERE site_id=? AND source_url=?", (vc.now_iso(cfg), reason, site_id, url))
        return
    con.execute("INSERT INTO jobs(job_id, site_id, source_url, checked_at, ad_status, notes) VALUES (?,?,?,?,?,?)",
                (vc.new_id(con, "job"), site_id, url, vc.now_iso(cfg), "unavailable", f"source failed: {reason}"))


def research(con, svc, cfg, only: list[str] | None = None) -> dict:
    rows = con.execute("SELECT * FROM sites WHERE status<>'closed' ORDER BY site_id").fetchall()
    summary = {}
    for s in rows:
        sid = s["site_id"]
        if only and sid not in only:
            continue
        try:
            det = svc.details(s["place_id"])  # live, not stored
            hint = {"place_id": s["place_id"], "map_name": (det.get("displayName") or {}).get("text"),
                    "website": det.get("websiteUri"), "map_address": det.get("formattedAddress")}
            data = svc.research_site(hint)
        except vc.BudgetExceeded:
            raise
        except Exception as e:
            _unavailable(con, cfg, sid, None, f"{type(e).__name__}: {str(e)[:200]}")
            con.execute("UPDATE sites SET notes=? WHERE site_id=?", (f"research source failed ({str(e)[:120]}); one unavailable entry recorded", sid))
            if s["status"] in AUTO_FROM:
                set_status(con, cfg, sid, "review", "research", "research source failed")
            con.commit()
            summary[sid] = "source unavailable"
            continue
        b = data["business"]
        verified = bool(b["verified"] and b["name"] and b["address"] and b["source_url"])
        if verified:
            con.execute("UPDATE sites SET name=?, address=?, source_url=?, match_status=?, notes=? WHERE site_id=?",
                        (b["name"], b["address"], b["source_url"], _match(b["name"], hint["map_name"]), b["note"] or None, sid))
        else:
            con.execute("UPDATE sites SET notes=? WHERE site_id=?", (f"identity not verified on employer website: {b['note']}", sid))
        for f in data.get("sources_failed", []):
            _unavailable(con, cfg, sid, f["url"], f["reason"])
        for ad in data["jobs"]:
            url = ad["original_ad_url"]
            sch = ad["schedules"]
            one = sch[0] if len(sch) == 1 else {}
            note = "; ".join(x for x in [ad["note"], f"match: {ad['match_evidence']}",
                                         f"{len(sch)} schedules, see shifts" if len(sch) > 1 else "",
                                         "" if sch else "ad states no schedule"] if x)
            row = con.execute("SELECT job_id FROM jobs WHERE site_id=? AND source_url=?", (sid, url)).fetchone()
            vals = (vc.now_iso(cfg), ad["ad_status"], ad["site_match"], ad["excerpt"][:300],
                    one.get("start"), one.get("end"), one.get("days"), note)
            if row:
                jid = row[0]
                con.execute("UPDATE jobs SET checked_at=?, ad_status=?, site_match=?, excerpt=?, shift_start=?, shift_end=?, "
                            "shift_days=?, notes=? WHERE job_id=?", (*vals, jid))
            else:
                jid = vc.new_id(con, "job")
                con.execute("INSERT INTO jobs(job_id, site_id, source_url, checked_at, ad_status, site_match, excerpt, "
                            "shift_start, shift_end, shift_days, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (jid, sid, url, *vals))
            for sc in sch:
                key = (sc["start"], sc["end"], sc["days"])
                ex = con.execute('SELECT shift_id FROM shifts WHERE job_id=? AND start IS ? AND "end" IS ? AND days IS ?',
                                 (jid, *key)).fetchone()
                if ex:
                    shid = ex[0]
                else:
                    shid = vc.new_id(con, "shift")
                    missing = [n for n, v in zip(("start", "end", "days"), key) if not v]
                    con.execute("INSERT INTO shifts VALUES (?,?,?,?,?,?,?,?)",
                                (shid, jid, sid, *key, sc["source_quote"],
                                 f"source does not state {', '.join(missing)}" if missing else None))
                con.execute("INSERT OR IGNORE INTO checks(site_id, job_id, shift_id, entered_by, notes) VALUES (?,?,?,'auto',?)",
                            (sid, jid, shid, "awaiting independent verification (visit or employer confirmation)"))
        # every job must link to an existing site before 'researched'
        orphans = con.execute("SELECT COUNT(*) FROM jobs j LEFT JOIN sites s ON s.site_id=j.site_id WHERE j.site_id=? AND s.site_id IS NULL",
                              (sid,)).fetchone()[0]
        if s["status"] in AUTO_FROM:
            if verified and orphans == 0:
                set_status(con, cfg, sid, "researched", "research", "identity verified; all jobs link to this site")
            else:
                set_status(con, cfg, sid, "review", "research", "identity not verified on employer website")
        con.commit()
        summary[sid] = "verified" if verified else "review"
    return summary


# ------------------------------------------------------------------ 3. label
class _Stored:
    """Stored label distributions shaped like an SDK response (for reuse without a new call)."""
    def __init__(self, dist):
        A = type("A", (), {})
        self.choices = {}
        for q, probs in dist.items():
            a = A(); a.probabilities = probs; a.choice = max(probs, key=probs.get); self.choices[q] = a


def label(con, svc, cfg) -> dict:
    import jev
    stats = {"excerpts": 0, "labelled": 0, "reused": 0, "failed": 0}
    for j in con.execute("SELECT * FROM jobs WHERE excerpt IS NOT NULL AND excerpt<>'' ORDER BY job_id").fetchall():
        stats["excerpts"] += 1
        h = jev.input_hash(j["excerpt"])
        shift_rows = [dict(r) for r in con.execute('SELECT start, "end" FROM shifts WHERE job_id=?', (j["job_id"],))]
        if j["label_input_hash"] == h and j["arrangement"] and not j["label_error"]:
            stats["reused"] += 1
            # metadata (stale, match, shift times) can change without the excerpt changing:
            # re-run the decision code on the stored distributions, no new Jev call
            d = jev.decide(_Stored(json.loads(j["label_distributions"])), dict(j), shift_rows, tc.crosses_midnight)
            con.execute("UPDATE jobs SET review_reason=? WHERE job_id=?", (d["review_reason"], j["job_id"]))
            continue
        try:
            resp = svc.jev(j["excerpt"])
            d = jev.decide(resp, dict(j), shift_rows, tc.crosses_midnight)
            con.execute("UPDATE jobs SET arrangement=?, work_type=?, shift=?, confidence=?, review_reason=?, label_distributions=?, "
                        "label_input_hash=?, label_error=NULL, labeled_at=? WHERE job_id=?",
                        (d["arrangement"], d["work_type"], d["shift"], d["confidence"], d["review_reason"],
                         d["label_distributions"], h, vc.now_iso(cfg), j["job_id"]))
            stats["labelled"] += 1
        except vc.BudgetExceeded:
            raise
        except Exception as e:
            con.execute("UPDATE jobs SET label_error=?, review_reason=?, labeled_at=? WHERE job_id=?",
                        (f"{type(e).__name__}: {str(e)[:200]}", "Jev label failed", vc.now_iso(cfg), j["job_id"]))
            stats["failed"] += 1
        con.commit()
    return stats


# ------------------------------------------------------------------ 4. food + results
def _overnight_shifts(con, site_id):
    return [dict(r) for r in con.execute(
        "SELECT sh.* FROM shifts sh JOIN jobs j USING(job_id) WHERE sh.site_id=? AND j.ad_status='active' "
        "AND j.site_match='confirmed' AND sh.start IS NOT NULL AND sh.\"end\" IS NOT NULL AND sh.days IS NOT NULL", (site_id,))
            if tc.crosses_midnight(r["start"], r["end"])]


def food(con, svc, cfg, run_id, today: date | None = None) -> dict:
    out = {}
    for s in con.execute("SELECT * FROM sites WHERE status NOT IN ('closed','review','found') ORDER BY site_id").fetchall():
        sid = s["site_id"]
        if not _overnight_shifts(con, sid):
            continue
        det = svc.details(s["place_id"])
        loc = det.get("location")
        if not loc:
            continue
        r = cfg["food_radius_m"]
        venues, cov = places.nearby_complete(svc.nearby, loc["latitude"], loc["longitude"], r)
        con.execute("INSERT INTO food_searches VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, run_id, vc.now_iso(cfg), r, cov["calls"], cov["raw_results"], cov["unique_in_radius"],
                     cov["capped_calls"], int(cov["complete"]),
                     None if cov["complete"] else f"still at the 20-result cap after narrowing: {cov['still_capped']}"))
        hints = [{"key": v["id"], "map_name": (v.get("displayName") or {}).get("text"), "website": v.get("websiteUri")} for v in venues]
        verified = {x["key"]: x for x in (svc.verify_hours(hints)["venues"] if hints else [])}
        for v in venues:
            loc2 = v["location"]
            dist = places.distance_m(loc["latitude"], loc["longitude"], loc2["latitude"], loc2["longitude"])
            h = verified.get(v["id"])
            ok = bool(h and h["verified"] and h["source_url"])
            periods = None
            if ok:
                periods = json.dumps({"always_open": h["always_open"], "weekly": h["weekly"],
                                      "holidays": {x["date"]: "closed" if x["closed"] else [{"open": x["open"], "close": x["close"]}]
                                                   for x in h["holidays"]},
                                      "holidays_same_as_regular": h["holidays_same_as_regular"]})
            note = None if ok else f"hours unknown: {(h or {}).get('note') or 'not verified on a business-owned source'}"
            ex = con.execute("SELECT food_id, access_by FROM food WHERE site_id=? AND place_id=?", (sid, v["id"])).fetchone()
            if ex:
                con.execute("UPDATE food SET source_url=?, distance_m=?, opening_periods=?, checked_at=?, notes=? WHERE food_id=?",
                            (h["source_url"] if ok else None, dist, periods, vc.now_iso(cfg), note, ex[0]))
            else:
                con.execute("INSERT INTO food VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (vc.new_id(con, "food"), sid, v["id"], h["source_url"] if ok else None, dist, periods,
                             vc.now_iso(cfg), "unknown", "auto",
                             (note + "; " if note else "") + "walking access unknown until checked"))
        con.commit()
        out[sid] = cov
    score(con, cfg, today)
    return out


def score(con, cfg, today: date | None = None) -> dict:
    """(Re)compute results for every shift. Reuses rows whose inputs are unchanged."""
    today = today or vc.now_local(cfg).date()
    counts = {"computed": 0, "reused": 0}
    for sh in con.execute('SELECT sh.*, j.ad_status, j.site_match FROM shifts sh JOIN jobs j USING(job_id)').fetchall():
        key = (sh["site_id"], sh["job_id"], sh["shift_id"])
        chk = con.execute("SELECT * FROM checks WHERE site_id=? AND job_id=? AND shift_id=?", key).fetchone()
        if chk is None:
            continue
        fs = con.execute("SELECT complete FROM food_searches WHERE site_id=? ORDER BY searched_at DESC LIMIT 1", (sh["site_id"],)).fetchone()
        fr = [dict(r) for r in con.execute("SELECT food_id, opening_periods, access_status FROM food WHERE site_id=? ORDER BY food_id", (sh["site_id"],))]
        inputs = json.dumps([dict(sh), dict(chk), fs and fs[0], fr, today.isoformat(), cfg["timezone"]], sort_keys=True, default=str)
        h = hashlib.sha256(inputs.encode()).hexdigest()[:16]
        old = con.execute("SELECT input_hash FROM results WHERE site_id=? AND job_id=? AND shift_id=?", key).fetchone()
        if old and old[0] == h:
            counts["reused"] += 1
            continue
        res = _score_one(sh, chk, fs, fr, cfg, today)
        con.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(site_id, job_id, shift_id) DO UPDATE SET "
                    "dead_hours=excluded.dead_hours, score=excluded.score, score_status=excluded.score_status, "
                    "missing_checks=excluded.missing_checks, input_hash=excluded.input_hash, computed_at=excluded.computed_at, notes=excluded.notes",
                    (*key, res["dead_hours"], res["score"], res["status"], res["missing"], h, vc.now_iso(cfg), res["notes"]))
        counts["computed"] += 1
    con.commit()
    return counts


def _score_one(sh, chk, fs, food_rows, cfg, today) -> dict:
    missing, notes = [], []
    if not (sh["start"] and sh["end"] and sh["days"]):
        return {"dead_hours": None, "score": None, "status": "needs_verification",
                "missing": "explicit shift start/end/days from the source", "notes": "source gives no exact schedule"}
    if not tc.crosses_midnight(sh["start"], sh["end"]):
        return {"dead_hours": None, "score": None, "status": "not_overnight", "missing": None,
                "notes": f"{sh['start']}-{sh['end']} does not span midnight"}
    if sh["ad_status"] != "active":
        missing.append("a current ad (this one is stale)")
    if sh["site_match"] != "confirmed":
        missing.append("confirmation the ad is for this building")
    usable = [f for f in food_rows if f["access_status"] != "not_accessible"]
    dh, prov = None, False
    if fs is None:
        missing.append("food search")
    else:
        hours = [json.loads(f["opening_periods"]) if f["opening_periods"] else None for f in usable]
        r = tc.shift_dead_hours(sh["start"], sh["end"], sh["days"], hours, cfg["timezone"], bool(fs[0]), today)
        dh = r["dead_hours"]
        if dh is None:
            missing.append(f"dead hours ({r['why']})")
        hol = [o["date"] + " " + o["holiday"] for o in r["occurrences"] if o["holiday"]]
        notes.append("per date: " + ", ".join(f"{o['date']}={o['dead_hours']}" for o in r["occurrences"]))
        if hol:
            notes.append("holiday occurrences: " + ", ".join(hol))
        # walking access of venues that are open during the shift
        zone_iv = []
        for f, hh in zip(usable, hours):
            if hh and f["access_status"] == "unknown" and _open_in_shift(hh, sh, cfg, today):
                zone_iv.append(f["food_id"])
        if zone_iv:
            prov = True
            notes.append("walking access unknown for " + ", ".join(zone_iv))
    hc = chk["verified_night_headcount"]
    if hc is None:
        missing.append("verified on-shift headcount")
    if missing:
        return {"dead_hours": dh, "score": None, "status": "needs_verification", "missing": "; ".join(missing), "notes": " | ".join(notes) or None}
    return {"dead_hours": dh, "score": round(hc * dh, 2), "status": "provisional" if prov else "scored",
            "missing": "walking access to open venues" if prov else None,
            "notes": " | ".join([f"{hc} workers x {dh} dead hours = {round(hc * dh, 2)} worker-hours per shift"] + notes)}


def _open_in_shift(hours, sh, cfg, today) -> bool:
    r = tc.shift_dead_hours(sh["start"], sh["end"], sh["days"], [hours], cfg["timezone"], True, today)
    return any(o["dead_hours"] is None or o["dead_hours"] < o["shift_hours"] for o in r["occurrences"] if not o["holiday"])


# ------------------------------------------------------------------ 5. outreach
BANNED = ("omzet", "winst", "verdien", "gratis", "revenue", "profit", "earn", "commission", "free ", "korting", "aanbieding")


def outreach(con, svc, cfg) -> dict:
    prio = [r[0] for r in con.execute(
        "SELECT site_id FROM results WHERE score IS NOT NULL GROUP BY site_id ORDER BY MAX(score) DESC")]
    # unscored verification queue: sites with overnight evidence (a shift awaiting checks, or an overnight/rotating label)
    rest = [r[0] for r in con.execute(
        "SELECT s.site_id FROM sites s WHERE s.status='researched' AND (EXISTS (SELECT 1 FROM results r WHERE r.site_id=s.site_id "
        "AND r.score_status='needs_verification') OR EXISTS (SELECT 1 FROM jobs j WHERE j.site_id=s.site_id "
        "AND j.shift IN ('overnight','rotating'))) ORDER BY s.distance_m")]
    for r in con.execute("SELECT site_id FROM sites WHERE status='researched'"):
        if r[0] not in rest and r[0] not in prio:
            con.execute("UPDATE sites SET notes=COALESCE(notes,'') || ' | no draft: no overnight evidence found' WHERE site_id=? "
                        "AND COALESCE(notes,'') NOT LIKE '%no overnight evidence%'", (r[0],))
    order = prio + [s for s in rest if s not in prio]
    out = {"drafted": [], "skipped": {}}
    for sid in order:
        s = con.execute("SELECT * FROM sites WHERE site_id=?", (sid,)).fetchone()
        if s["status"] not in ("researched", "draft_ready"):
            out["skipped"][sid] = f"status {s['status']}"
            continue
        if con.execute("SELECT 1 FROM outreach WHERE site_id=?", (sid,)).fetchone():
            out["skipped"][sid] = "draft already saved"
            continue
        ref = con.execute(
            "SELECT sh.* FROM shifts sh JOIN jobs j USING(job_id) WHERE sh.site_id=? AND j.ad_status='active' AND j.site_match='confirmed' "
            "AND (j.review_reason IS NULL OR j.review_reason='') AND j.shift IN ('overnight','rotating') AND sh.start IS NOT NULL "
            "AND sh.days IS NOT NULL ORDER BY sh.shift_id", (sid,)).fetchall()
        ref = next((dict(r) for r in ref if tc.crosses_midnight(r["start"], r["end"])), None)
        c = svc.find_contact({"site_id": sid, "name": s["name"], "address": s["address"], "website": s["source_url"]})
        lang = cfg.get("language", "nl")
        shift_txt = f"{ref['start']}-{ref['end']} on {ref['days']}, from your current vacancy" if ref else "none"
        prompt = (f"Write a short first email (language: {lang}) from a local person in {cfg['center_query']} to {s['name']}.\n"
                  "Goal: ask whether the overnight team already has food available during the shift.\n"
                  f"Referenced shift: {shift_txt}\n"
                  "Rules: under 80 words in the body; no sales pitch, no prices, no revenue or earnings promises; "
                  "if Referenced shift is none, ask about overnight staffing without claiming they have a night shift; "
                  "end with [Your name] and [phone] placeholders.")
        d = None
        for _ in range(2):
            cand = svc.draft_email(prompt)
            words = len(cand["body"].split())
            if words < 90 and not any(b in cand["body"].lower() for b in BANNED):
                d = cand
                break
            prompt += "\nThe previous draft was too long or salesy. Shorter and plainer."
        if d is None:
            out["skipped"][sid] = "draft failed the length/sales check twice"
            continue
        note = "Draft only — not sent."
        if not c["recipient"]:
            note += f" No public email found; use the contact page: {c['contact_page'] or s['source_url']}"
        if not ref:
            note += " No current site-matched overnight shift; email asks generally."
        con.execute("INSERT INTO outreach VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (vc.new_id(con, "out"), sid, c["contact_source"] or c["contact_page"], c["recipient"], d["subject"], d["body"],
                     ref["shift_id"] if ref else None, len(d["body"].split()), vc.now_iso(cfg), note))
        set_status(con, cfg, sid, "draft_ready", "outreach", "draft saved")
        con.commit()
        out["drafted"].append(sid)
    return out


# ------------------------------------------------------------------ 6. user answers
def answer(con, cfg, site_id, shift_id=None, headcount=None, source=None, cafeteria=None, vending=None, access_notes=None):
    rows = con.execute("SELECT * FROM checks WHERE site_id=?" + (" AND shift_id=?" if shift_id else ""),
                       (site_id, shift_id) if shift_id else (site_id,)).fetchall()
    if not rows:
        raise ValueError("no matching check rows (does the site have shifts?)")
    if headcount is not None and (not shift_id or not source):
        raise ValueError("headcount needs --shift and --source (headcount is per shift)")
    for r in rows:
        con.execute("UPDATE checks SET verified_night_headcount=COALESCE(?, verified_night_headcount), "
                    "headcount_source=COALESCE(?, headcount_source), verified_at=CASE WHEN ? IS NOT NULL THEN ? ELSE verified_at END, "
                    "cafeteria=COALESCE(?, cafeteria), vending_contract=COALESCE(?, vending_contract), "
                    "access_notes=COALESCE(?, access_notes), entered_by='user', notes=? WHERE site_id=? AND job_id=? AND shift_id=?",
                    (headcount, source if headcount is not None else None, headcount, vc.now_iso(cfg), cafeteria, vending,
                     access_notes, "entered by user", r["site_id"], r["job_id"], r["shift_id"]))
    con.commit()
    return score(con, cfg)


def food_access(con, cfg, food_id, status, note=None):
    con.execute("UPDATE food SET access_status=?, access_by='user', notes=? WHERE food_id=?",
                (status, note or "walking access set by user", food_id))
    con.commit()
    return score(con, cfg)
