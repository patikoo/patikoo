"""Service layer: LiveServices calls Google Places, Claude and TypeSafe Jev; MockServices returns
synthetic fixtures through the same interface so every pipeline step can be tested offline.
Mock data is fictional and only ever written to a temporary database."""
from __future__ import annotations

import vs_common as vc
import places


class LiveServices:
    modes = {"places": "live", "jev": "live", "claude": "live"}

    def __init__(self, cfg: dict, sec: dict, budget: vc.Budget):
        import claude_research as cr
        self.cr, self.cfg, self.budget = cr, cfg, budget
        self.gkey, self.tkey = sec["GOOGLE_MAPS_API_KEY"], sec["TYPESAFE_API_KEY"]
        self.claude = cr.Claude(sec["ANTHROPIC_API_KEY"], cfg["claude_model"], cfg.get("country", "NL"))

    # Google Places (live, nothing but place IDs is stored by callers)
    def resolve_center(self, q): return places.resolve_center(self.gkey, q)
    def text_search(self, q, lat, lng, r): return places.text_search_pages(self.gkey, q, lat, lng, r)
    def details(self, pid): return places.place_details(self.gkey, pid)
    def nearby(self, types, lat, lng, r): return places.nearby(self.gkey, types, lat, lng, r)

    # Claude (budget-guarded)
    def _paid(self, kind, fn, *a):
        self.budget.guard(self.cr.EST[kind])
        data, usd = fn(*a)
        self.budget.charge(usd)
        return data

    def research_site(self, hint): return self._paid("research", self.claude.research_site, hint)
    def verify_hours(self, venues): return self._paid("hours", self.claude.verify_hours, venues)
    def find_contact(self, site): return self._paid("contact", self.claude.find_contact, site)
    def draft_email(self, prompt): return self._paid("draft", self.claude.draft_email, prompt)

    # TypeSafe Jev
    def jev(self, excerpt):
        import jev
        self.budget.guard(0.001)
        return jev.ask(self.tkey, excerpt)


# ----------------------------------------------------------------------------- mock
C = (52.2215, 6.8937)  # fictional center near Enschede


def _off(dlat_m, dlng_m, base=C):
    import math
    return {"latitude": base[0] + dlat_m / 111320.0, "longitude": base[1] + dlng_m / (111320.0 * math.cos(math.radians(base[0])))}


MOCK_PLACES = {f"ChIJMOCK{i:02d}": _off(800 * i, -500 * i) for i in range(1, 13)}
MOCK_PLACES["ChIJMOCKFAR"] = _off(60000, 0)  # outside any 25 km radius
QUERY_HITS = {  # duplicates across queries on purpose
    "warehouse": ["ChIJMOCK01", "ChIJMOCK02", "ChIJMOCK03", "ChIJMOCK04", "ChIJMOCK11", "ChIJMOCK12"],
    "distributiecentrum": ["ChIJMOCK01", "ChIJMOCK05", "ChIJMOCK10", "ChIJMOCKFAR"],
    "fabriek": ["ChIJMOCK02", "ChIJMOCK06", "ChIJMOCK07"],
    "ziekenhuis": ["ChIJMOCK08", "ChIJMOCK09"],
}
J = lambda url, status, match, ex, sch, note="": {"original_ad_url": url, "ad_status": status, "site_match": match,
                                                 "match_evidence": "MOCK", "excerpt": ex, "schedules": sch, "note": note}
SCH = lambda s, e, d: {"start": s, "end": e, "days": d, "source_quote": f"{s}-{e} {d}" if s else "ploegendienst"}
MOCK_RESEARCH = {
    "ChIJMOCK01": {"jobs": [J("https://example.com/m01/orderpicker", "active", "confirmed",
                              "Orderpicker MOCK DC: dagdienst 07:00-15:00 of nachtdienst 22:00-06:00, ma t/m vr.",
                              [SCH("07:00", "15:00", "Mon,Tue,Wed,Thu,Fri"), SCH("22:00", "06:00", "Mon,Tue,Wed,Thu,Fri")])]},
    "ChIJMOCK02": {"jobs": [J("https://example.com/m02/operator", "active", "confirmed",
                              "Productie-operator MOCK fabriek, vaste nachtdienst 23:00-07:00 maandag t/m vrijdag.",
                              [SCH("23:00", "07:00", "Mon,Tue,Wed,Thu,Fri")])]},
    "ChIJMOCK03": {"jobs": [J("https://example.com/m03/zorg", "active", "confirmed",
                              "Verpleegkundige MOCK, werken in wisselende diensten (vroege, late en nachtdienst).",
                              [SCH(None, None, None)])]},
    "ChIJMOCK04": {"jobs": [J("https://example.com/m04/nacht", "stale", "confirmed",
                              "Vacature gesloten. Nachtmedewerker 22:00-06:00 zo t/m do.",
                              [SCH("22:00", "06:00", "Sun,Mon,Tue,Wed,Thu")], "closing date passed")]},
    "ChIJMOCK05": {"jobs": [J("https://example.com/m05/other-city", "active", "ambiguous",
                              "Logistiek medewerker MOCK, locatie Hengelo of Enschede, nachtdienst 22:00-06:00.",
                              [SCH("22:00", "06:00", "Mon,Tue,Wed,Thu,Fri")], "employer has two buildings")]},
    "ChIJMOCK06": "FAIL",
    "ChIJMOCK07": {"verified": False, "jobs": [J("https://example.com/m07/job", "active", "confirmed",
                                                 "Inpakker MOCK, dagdienst 08:00-16:30.", [SCH("08:00", "16:30", "Mon,Tue,Wed,Thu,Fri")])]},
    "ChIJMOCK08": {"jobs": []},
    "ChIJMOCK09": {"jobs": [J("https://example.com/m09/it", "active", "confirmed",
                              "Medior developer MOCK, volledig thuiswerken, kantoortijden.", [])]},
    "ChIJMOCK10": {"jobs": [J("https://example.com/m10/sorteer", "active", "confirmed",
                              "Sorteerder MOCK pakketcentrum, nachtdienst 22:00-06:00 zondag t/m donderdag.",
                              [SCH("22:00", "06:00", "Sun,Mon,Tue,Wed,Thu")])]},
}
H = lambda o, c: [{"day": d, "open": o, "close": c} for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]]
MOCK_HOURS = {  # venue place_id -> verified business-owned hours (None = not verifiable)
    "late": {"always_open": False, "weekly": H("06:00", "00:00"), "holidays": [], "holidays_same_as_regular": True},
    "day": {"always_open": False, "weekly": H("08:00", "21:00"), "holidays": [], "holidays_same_as_regular": True},
    "overlap": {"always_open": False, "weekly": H("07:00", "23:00"), "holidays": [], "holidays_same_as_regular": True},
    "allday": {"always_open": True, "weekly": [], "holidays": [], "holidays_same_as_regular": True},
    "none": None,
}


class MockServices:
    modes = {"places": "mock", "jev": "mock", "claude": "mock"}

    def __init__(self):
        self.budget = vc.Budget(1.0)

    def resolve_center(self, q): return {"place_id": "ChIJMOCKCENTER", "lat": C[0], "lng": C[1]}

    def text_search(self, q, lat, lng, r):
        hits = QUERY_HITS.get(q, [])
        return [{"id": h, "location": MOCK_PLACES[h]} for h in hits], 1, False

    def details(self, pid):
        return {"id": pid, "displayName": {"text": f"MOCK {pid[-2:]}"}, "websiteUri": f"https://example.com/{pid[-2:].lower()}",
                "location": MOCK_PLACES[pid]}

    def nearby(self, types, lat, lng, r):
        base = (lat, lng)
        site = min(MOCK_PLACES, key=lambda k: abs(MOCK_PLACES[k]["latitude"] - lat) + abs(MOCK_PLACES[k]["longitude"] - lng))
        v = lambda i, kind, d=300: {"id": f"VEN-{site[-2:]}-{kind}-{i}", "location": _off(d, 0, base),
                                    "displayName": {"text": kind}, "websiteUri": f"https://example.com/venue/{kind}"}
        if site == "ChIJMOCK01":
            if len(types) > 1:   # all-types call hits the 20 cap -> pipeline must narrow
                return [v(i, "day", 200 + i) for i in range(20)]
            return {"convenience_store": [v(0, "late")], "supermarket": [v(1, "day")],
                    "cafe": [v(2, "overlap"), v(3, "far", 2000)]}.get(types[0], [])
        if site == "ChIJMOCK02" and len(types) > 1:
            return [v(0, "late"), v(1, "none")]
        if site == "ChIJMOCK10" and len(types) > 1:
            return [v(0, "allday"), v(1, "day")]
        return []

    def research_site(self, hint):
        pid = hint["place_id"]
        r = MOCK_RESEARCH.get(pid)
        if r == "FAIL":
            raise RuntimeError("MOCK: employer website timed out")
        r = r or {"jobs": []}
        ok = r.get("verified", True)
        return {"business": {"name": f"MOCK Employer {pid[-2:]}" if ok else None,
                             "address": f"MOCK Industrieweg {pid[-2:]}, Enschede" if ok else None,
                             "source_url": f"https://example.com/{pid[-2:]}/contact" if ok else None,
                             "verified": ok, "note": "MOCK" if ok else "MOCK: website shows no street address"},
                "jobs": r["jobs"], "sources_failed": []}

    def verify_hours(self, venues):
        out = []
        for v in venues:
            kind = v["key"].split("-")[2]
            h = MOCK_HOURS.get(kind)
            out.append({"key": v["key"], "verified": h is not None, "source_url": f"https://example.com/venue/{kind}" if h else None,
                        **(h or {"always_open": False, "weekly": [], "holidays": [], "holidays_same_as_regular": False}),
                        "note": "MOCK" if h else "MOCK: no business-owned hours found"})
        return {"venues": out}

    def find_contact(self, site):
        n = site["site_id"][-1]
        if int(n) % 2:
            return {"recipient": f"facilities@example.com", "contact_source": "https://example.com/contact",
                    "contact_page": "https://example.com/contact", "note": "MOCK"}
        return {"recipient": None, "contact_source": None, "contact_page": "https://example.com/contact-form", "note": "MOCK: no public email"}

    def draft_email(self, prompt):
        ref = "nachtdienst" if "Referenced shift:" in prompt and "none" not in prompt.split("Referenced shift:")[1][:6] else "nachtploeg"
        return {"subject": "Vraag over eten voor uw nachtploeg",
                "body": f"Goedemiddag,\n\nIk ben een lokale ondernemer uit Enschede. Ik zag dat u mensen in de {ref} heeft. "
                        "Heeft het nachtteam op dit moment toegang tot eten of drinken tijdens de dienst? "
                        "Ik hoor graag hoe dat nu geregeld is.\n\nMet vriendelijke groet,\n[Uw naam]\n[telefoon]"}

    def jev(self, excerpt):
        from typesafe_sdk import SystemOneResponse
        t = excerpt.lower()
        pick = lambda opts, top: {o: (0.9 if o == top else 0.1 / (len(opts) - 1)) for o in opts}
        arr = "remote" if "thuis" in t else "onsite"
        wt = "desk" if "developer" in t else "physical"
        sh = ("rotating" if "wisselende" in t or ("dagdienst" in t and "nachtdienst" in t) else
              "overnight" if "nacht" in t else "day" if "dagdienst" in t else "unknown")
        q = {"arrangement": (["onsite", "remote", "hybrid", "unknown"], arr),
             "work_type": (["physical", "desk", "mixed", "unknown"], wt),
             "shift": (["day", "evening", "overnight", "rotating", "unknown"], sh)}
        ans = {k: {"type": "choice", "choice": top, "confidence": 0.9, "probabilities": pick(o, top)} for k, (o, top) in q.items()}
        return SystemOneResponse.model_validate({"model": "jev-mock", "usage": {"input_tokens": 0, "output_tokens": 0}, "answers": ans})
