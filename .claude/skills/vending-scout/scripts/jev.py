"""Label job excerpts with TypeSafe Jev via the official `typesafe-sdk` (System One API).

Three independent Choice questions over the same text (one request, shared state):
  arrangement = onsite | remote | hybrid | unknown
  work_type   = physical | desk | mixed | unknown
  shift       = day | evening | overnight | rotating | unknown
Definitions: overnight = work spanning midnight; evening = late work ending before midnight;
rotating = explicitly changing shifts. Missing or contradictory evidence = unknown.

Following the official TypeSafe skill: wording lives only in QUESTIONS; Jev judges, code decides;
a failed call is recorded as a failure, never as an answer; option names describe content.
"""
from __future__ import annotations

import hashlib
import json

from typesafe_sdk import Choice, TypeSafeClient

DOCS = {"sdk": "https://docs.typesafe.ai/go/python-sdk", "api": "https://docs.typesafe.ai/go/api",
        "choice": "https://docs.typesafe.ai/go/choice", "skill": "https://github.com/typesafe-ai/skills"}
QUESTIONS_VERSION = "v2"
MIN_TOP = 0.6      # top option probability below this -> review
MIN_MARGIN = 0.15  # top minus runner-up below this -> review

QUESTIONS = {
    "arrangement": Choice(
        instructions="Where does the person in the job described in `job.text` do the work?",
        criteria={"onsite": "At the employer's location on every working day.",
                  "remote": "From home or anywhere, never at the location.",
                  "hybrid": "Partly at the location and partly from home.",
                  "unknown": "The text does not say where the work is done, or says contradictory things."}),
    "work_type": Choice(
        instructions="What kind of work is the job described in `job.text`?",
        criteria={"physical": "Mostly hands-on work on the floor, such as picking, production, cleaning, care or driving.",
                  "desk": "Mostly computer or office work at a desk.",
                  "mixed": "A real share of both hands-on work and desk work.",
                  "unknown": "The text does not describe the tasks, or describes contradictory ones."}),
    "shift": Choice(
        instructions="When does the job described in `job.text` take place?",
        criteria={"day": "Daytime hours only.",
                  "evening": "Late work that ends before midnight.",
                  "overnight": "Work that spans midnight, such as a fixed night shift.",
                  "rotating": "Shifts that explicitly change, such as a 2- or 3-ploegendienst or a rota of day and night shifts.",
                  "unknown": "The text gives no working hours, or contradictory ones."}),
}


def input_hash(excerpt: str) -> str:
    qs = {k: q.model_dump(exclude_none=True) for k, q in QUESTIONS.items()}
    return hashlib.sha256(json.dumps([QUESTIONS_VERSION, excerpt, qs], sort_keys=True).encode()).hexdigest()[:16]


def state_for(excerpt: str) -> dict:
    return {"job": {"text": excerpt}}  # Jev labels the job described; no employer metadata in state


def decide(resp, job: dict, shift_rows: list[dict], crosses_midnight) -> dict:
    """Pure decision code: chosen values, distributions, confidence, review_reason."""
    out, dist, reasons, tops = {}, {}, [], []
    for q in QUESTIONS:
        a = resp.choices[q]
        probs = dict(a.probabilities)
        ranked = sorted(probs.values(), reverse=True)
        top, margin = ranked[0], ranked[0] - (ranked[1] if len(ranked) > 1 else 0)
        out[q], dist[q] = a.choice, {k: round(v, 4) for k, v in probs.items()}
        tops.append(top)
        if a.choice == "unknown":
            reasons.append(f"{q}=unknown")
        elif top < MIN_TOP or margin < MIN_MARGIN:
            reasons.append(f"{q} uncertain (top {top:.2f}, margin {margin:.2f})")
    # conflicting metadata
    timed = [s for s in shift_rows if s.get("start") and s.get("end")]
    spans = [crosses_midnight(s["start"], s["end"]) for s in timed]
    if out["shift"] == "overnight" and timed and not any(spans):
        reasons.append("label overnight but source times do not span midnight")
    if any(spans) and out["shift"] in ("day", "evening"):
        reasons.append(f"source times span midnight but label is {out['shift']}")
    if out["arrangement"] == "remote" and out["work_type"] == "physical":
        reasons.append("remote + physical conflict")
    if job.get("site_match") == "ambiguous":
        reasons.append("ad-to-site match ambiguous")
    if job.get("ad_status") == "stale":
        reasons.append("ad is stale")
    return {**out, "confidence": round(min(tops), 4), "label_distributions": json.dumps(dist),
            "review_reason": "; ".join(reasons) or None}


def ask(api_key: str, excerpt: str):
    """One System One request. Raises on any failure (caller records it)."""
    with TypeSafeClient(api_key=api_key) as client:
        return client.system_one(state=state_for(excerpt), questions=QUESTIONS)
