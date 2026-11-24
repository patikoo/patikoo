"""Dead hours on real local dates (time zone, midnight crossings, DST, holidays, 24-hour venues).

dead_hours(shift occurrence) = shift duration - |union of open-food intervals inside the shift|
Unknown venue hours or incomplete food coverage -> unknown (None), never zero — unless the
known intervals already cover the whole shift (then 0 is certain).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
UTC = timezone.utc


def parse_days(days: str) -> list[int]:
    out = []
    for d in (days or "").split(","):
        d = d.strip()[:3].title()
        if d in DAYS:
            out.append(DAYS.index(d))
    return out


def _t(s: str) -> time:
    if s == "24:00":
        return time(0, 0)
    h, m = s.split(":")
    return time(int(h), int(m))


def _span(day: date, start: str, end: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local wall-clock span -> UTC instants. End <= start (or 24:00) means the next calendar day."""
    s = datetime.combine(day, _t(start), tzinfo=tz)
    end_day = day + timedelta(days=1) if (end == "24:00" or _t(end) <= _t(start)) else day
    e = datetime.combine(end_day, _t(end), tzinfo=tz)
    return s.astimezone(UTC), e.astimezone(UTC)


def crosses_midnight(start: str | None, end: str | None) -> bool:
    """Overnight per the user's definition: work spanning midnight."""
    if not (start and end) or end in ("00:00", "24:00"):  # ending at midnight is late work, not spanning it
        return False
    return _t(end) <= _t(start)


def easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(y, month, (h + l - 7 * m + 114) % 31 + 1)


def nl_holidays(y: int) -> dict[date, str]:
    e = easter(y)
    kd = date(y, 4, 27) if date(y, 4, 27).weekday() != 6 else date(y, 4, 26)
    return {date(y, 1, 1): "Nieuwjaarsdag", e - timedelta(2): "Goede Vrijdag", e: "Eerste Paasdag",
            e + timedelta(1): "Tweede Paasdag", kd: "Koningsdag", date(y, 5, 5): "Bevrijdingsdag",
            e + timedelta(39): "Hemelvaartsdag", e + timedelta(49): "Eerste Pinksterdag",
            e + timedelta(50): "Tweede Pinksterdag", date(y, 12, 25): "Eerste Kerstdag", date(y, 12, 26): "Tweede Kerstdag"}


def holidays_for(d: date, country: str = "NL") -> dict[date, str]:
    if country != "NL":
        return {}
    return {**nl_holidays(d.year - 1), **nl_holidays(d.year), **nl_holidays(d.year + 1)}


def venue_intervals(hours: dict | None, day: date, tz: ZoneInfo, hol: dict[date, str]):
    """Open intervals (UTC) that START on local `day`. Returns None when hours are unknown for that day.

    hours JSON (business-owned source only):
      {"always_open": bool, "weekly": [{"day":"Mon","open":"07:00","close":"22:00"}],
       "holidays": {"2026-12-25": "closed" | [{"open":"10:00","close":"16:00"}]},
       "holidays_same_as_regular": bool}
    """
    if not hours:
        return None
    if day in hol:
        h = (hours.get("holidays") or {}).get(day.isoformat())
        if h == "closed":
            return []
        if isinstance(h, list):
            return [_span(day, p["open"], p["close"], tz) for p in h]
        if not hours.get("holidays_same_as_regular"):
            return None
    if hours.get("always_open"):
        s = datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)
        e = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
        return [(s, e)]
    return [_span(day, p["open"], p["close"], tz) for p in hours.get("weekly", []) if DAYS.index(p["day"]) == day.weekday()]


def union_seconds(intervals, lo: datetime, hi: datetime) -> float:
    clipped = sorted((max(a, lo), min(b, hi)) for a, b in intervals if b > lo and a < hi)
    total, cur_s, cur_e = 0.0, None, None
    for a, b in clipped:
        if cur_e is None or a > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = a, b
        else:
            cur_e = max(cur_e, b)
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total


def occurrence_dead_hours(day: date, start: str, end: str, venues: list[dict | None], tz: ZoneInfo,
                          coverage_complete: bool, hol: dict[date, str]) -> dict:
    s, e = _span(day, start, end, tz)
    duration = (e - s).total_seconds()
    known, unknown = [], 0
    for v in venues:
        any_unknown = False
        for d in (day - timedelta(1), day, day + timedelta(1)):
            iv = venue_intervals(v, d, tz, hol)
            if iv is None:
                any_unknown = True
            else:
                known.extend(iv)
        unknown += any_unknown
    open_s = union_seconds(known, s, e)
    out = {"date": day.isoformat(), "shift_hours": round(duration / 3600, 2), "holiday": hol.get(day)}
    if open_s >= duration:
        out["dead_hours"] = 0.0
    elif unknown or not coverage_complete:
        out["dead_hours"] = None
        out["why"] = (f"{unknown} venue(s) with unknown hours" if unknown else "") + \
                     ("" if coverage_complete else ("; " if unknown else "") + "food search coverage incomplete")
    else:
        out["dead_hours"] = round((duration - open_s) / 3600, 2)
    return out


def shift_dead_hours(start: str, end: str, days: str, venues: list[dict | None], tz_name: str,
                     coverage_complete: bool, from_day: date, window_days: int = 7) -> dict:
    """Evaluate each actual occurrence in the next `window_days`. Holiday occurrences with unknown
    hours are listed but excluded; any unknown regular occurrence makes the result unknown."""
    tz = ZoneInfo(tz_name)
    hol = holidays_for(from_day)
    wd = parse_days(days)
    occ = [occurrence_dead_hours(from_day + timedelta(i), start, end, venues, tz, coverage_complete, hol)
           for i in range(window_days) if (from_day + timedelta(i)).weekday() in wd]
    regular = [o for o in occ if not o["holiday"]]
    if not regular:
        return {"dead_hours": None, "occurrences": occ, "why": "no regular occurrence in window"}
    if any(o["dead_hours"] is None for o in regular):
        why = next(o["why"] for o in regular if o["dead_hours"] is None)
        return {"dead_hours": None, "occurrences": occ, "why": why}
    return {"dead_hours": min(o["dead_hours"] for o in regular), "occurrences": occ, "why": None}
