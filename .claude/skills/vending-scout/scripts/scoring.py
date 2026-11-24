"""Dead hours and score. Pure code, no API calls.

dead_hours: average hours per shift day when no food venue within walking distance is open.
score:      night_headcount x dead_hours x cafeteria factor x vending factor  (higher = better lead)
"""
from __future__ import annotations

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]  # Places-style day index 0 = Sunday
CAFETERIA = {"no": 1.0, "unknown": 0.7, "yes": 0.4}
VENDING = {"no": 1.0, "unknown": 0.6, "yes": 0.2}
WEEK = 7 * 24 * 60


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def shift_minutes(start: str, end: str, days: str) -> list[list[int]]:
    """Per shift day, the list of week-minutes the shift covers (overnight shifts wrap)."""
    out = []
    s, e = _hm(start), _hm(end)
    length = (e - s) % (24 * 60) or 24 * 60
    for d in [x.strip()[:3].title() for x in days.split(",") if x.strip()]:
        base = DAYS.index(d) * 1440 + s
        out.append([(base + i) % WEEK for i in range(length)])
    return out


def open_minutes(periods: list[dict]) -> set[int]:
    """Places-style periods: [{'open': {'day':1,'hour':7,'minute':0}, 'close': {...}}]. No close = 24/7."""
    mins: set[int] = set()
    for p in periods:
        o = p["open"]
        if "close" not in p:
            return set(range(WEEK))
        c = p["close"]
        a = o["day"] * 1440 + o["hour"] * 60 + o.get("minute", 0)
        b = c["day"] * 1440 + c["hour"] * 60 + c.get("minute", 0)
        length = (b - a) % WEEK or WEEK
        mins.update((a + i) % WEEK for i in range(length))
    return mins


def dead_hours(start: str, end: str, days: str, venues: list[list[dict]]) -> float:
    any_open: set[int] = set()
    for v in venues:
        any_open |= open_minutes(v)
    per_day = [sum(1 for m in day if m not in any_open) / 60 for day in shift_minutes(start, end, days)]
    return round(sum(per_day) / len(per_day), 2) if per_day else 0.0


def is_night(start: str, end: str) -> bool:
    s, e = _hm(start), _hm(end)
    covered = {(s + i) % 1440 for i in range(((e - s) % 1440) or 1440)}
    return any(m in covered for m in list(range(22 * 60, 1440)) + list(range(0, 6 * 60)))


def score(shift: dict, check: dict | None, venues: list[list[dict]] | None) -> dict:
    """Returns {'dead_hours','score','score_status','notes'}; blanks carry a reason in notes."""
    if not shift.get("start") or not shift.get("end") or not shift.get("days"):
        return {"dead_hours": None, "score": None, "score_status": "needs_shift_times",
                "notes": "Shift start/end/days unknown, so dead hours cannot be computed."}
    if not is_night(shift["start"], shift["end"]):
        return {"dead_hours": None, "score": None, "score_status": "not_night_shift",
                "notes": "Shift has no hours between 22:00 and 06:00."}
    if venues is None:
        return {"dead_hours": None, "score": None, "score_status": "needs_food_hours",
                "notes": "No opening hours available for nearby food venues."}
    dh = dead_hours(shift["start"], shift["end"], shift["days"], venues)
    hc = (check or {}).get("verified_night_headcount")
    if hc is None:
        return {"dead_hours": dh, "score": None, "score_status": "needs_headcount",
                "notes": "Night headcount not verified; score left blank."}
    f = CAFETERIA[(check.get("cafeteria") or "unknown")] * VENDING[(check.get("vending_contract") or "unknown")]
    return {"dead_hours": dh, "score": round(hc * dh * f, 1), "score_status": "scored",
            "notes": f"{hc} staff x {dh} h x factor {f:.2f} (cafeteria={check.get('cafeteria')}, vending={check.get('vending_contract')})."}
