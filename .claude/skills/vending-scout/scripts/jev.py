"""Label job ads with TypeSafe Jev via the official `typesafe-sdk` (System One API).

Follows the official TypeSafe skill (.claude/skills/typesafe-ai/SKILL.md):
- Jev makes small judgments; the decision logic (thresholds, review flags) lives in code below.
- All question wording is kept in QUESTIONS, in one place.
- Each Choice is paired with a Noul for the null case.
- A failed API call is an error, never treated as an answer.
- 0.7 / 0.3 are the starting Noul thresholds; the band between means "needs review".
"""
from __future__ import annotations

from typesafe_sdk import Choice, Noul, TypeSafeClient

DOCS = {
    "sdk": "https://docs.typesafe.ai/go/python-sdk",
    "api": "https://docs.typesafe.ai/go/api",
    "openapi": "https://api.typesafe.ai/openapi.json",
    "skill": "https://github.com/typesafe-ai/skills",
}
YES, NO = 0.7, 0.3
CHOICE_MIN = 0.6

QUESTIONS = {
    "is_job_ad": Noul(
        instructions="Is `ad.text` an advertisement for a job opening?",
        criteria={"true": "The text offers a position that people can apply for.",
                  "false": "The text is a news item, company profile, or a page that lists no position."},
    ),
    "mentions_schedule": Noul(
        instructions="Does `ad.text` state working hours or a shift pattern for the position?",
        criteria={"true": "It names times, shifts (such as night shift or ploegendienst), or the days worked.",
                  "false": "It gives no hours or shift information."},
    ),
    "night_work": Noul(
        instructions="Does `ad.text` say the position includes work between 22:00 and 06:00?",
        criteria={"true": "Night shifts, a rota that includes nights, or hours that run past 22:00 or start before 06:00.",
                  "false": "The hours stated are all between 06:00 and 22:00."},
    ),
    "arrangement": Choice(
        instructions="Where does the person in `ad.text` do the work?",
        criteria={"on_site": "At the company's location, every working day.",
                  "hybrid": "Partly at the location and partly from home.",
                  "remote": "From home or anywhere, not at the location."},
    ),
    "work_type": Choice(
        instructions="What kind of contract does `ad.text` offer?",
        criteria={"full_time": "A permanent or fixed-term job of about 32 hours a week or more.",
                  "part_time": "A job of fewer than about 32 hours a week.",
                  "temporary_or_agency": "Work through an employment agency (uitzendbureau) or a short temporary placement.",
                  "internship": "An internship or traineeship (stage).",
                  "freelance": "Self-employed or freelance work (zzp)."},
    ),
    "shift": Choice(
        instructions="Which shift pattern does `ad.text` describe?",
        criteria={"fixed_day": "Always daytime hours.",
                  "fixed_evening": "Always evening hours.",
                  "fixed_night": "Always night hours.",
                  "rotating": "A rota that moves between day, evening, and/or night shifts (such as 2- or 3-ploegendienst).",
                  "unspecified": "The ad does not describe the pattern."},
    ),
}


def build_state(excerpt: str, site_name: str | None, url: str | None) -> dict:
    return {"ad": {"text": excerpt, "employer": site_name or "", "url": url or ""}}


def decide(resp) -> dict:
    """Map Jev answers to tracker fields. Pure code — testable without the API."""
    n = {k: v.noul for k, v in resp.nouls.items()}
    c = resp.choices
    reasons = []
    for k in ("is_job_ad", "night_work", "mentions_schedule"):
        if NO < n[k] < YES:
            reasons.append(f"{k} unsettled ({n[k]:.2f})")
    if n["is_job_ad"] <= NO:
        reasons.append("probably not a job ad")
    shift = c["shift"].choice if n["mentions_schedule"] >= YES else "unspecified"
    for k in ("arrangement", "work_type") + (("shift",) if shift != "unspecified" else ()):
        top = max(c[k].probabilities.values())
        if top < CHOICE_MIN:
            reasons.append(f"{k} top probability {top:.2f}")
    # Confidence = weakest settled judgment used for this row.
    used = [max(n["is_job_ad"], 1 - n["is_job_ad"]), max(n["night_work"], 1 - n["night_work"]),
            max(c["arrangement"].probabilities.values()), max(c["work_type"].probabilities.values())]
    return {
        "ad_status_hint": "not_job_ad" if n["is_job_ad"] <= NO else None,
        "arrangement": c["arrangement"].choice,
        "work_type": c["work_type"].choice,
        "shift": shift,
        "night_work": n["night_work"],
        "confidence": round(min(used), 3),
        "review_reason": "; ".join(reasons) or None,
    }


def label_ad(api_key: str, excerpt: str, site_name: str | None, url: str | None):
    """Returns (decision dict, raw response, exact payload). Raises on any API failure."""
    state = build_state(excerpt, site_name, url)
    with TypeSafeClient(api_key=api_key) as client:
        resp = client.system_one(state=state, questions=QUESTIONS)
    return decide(resp), resp, {"state": state, "questions": {k: q.model_dump(exclude_none=True) for k, q in QUESTIONS.items()}}
