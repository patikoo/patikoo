"""Claude does the research and writing (official `anthropic` Python SDK).

Each task = (1) research with server-side web search / web fetch, then (2) structured-output
extraction into a fixed JSON schema. Server-side refusal fallbacks are on (`fallbacks="default"`).
"""
from __future__ import annotations

import json

import anthropic

DOCS = {
    "messages": "https://platform.claude.com/docs/en/api/messages",
    "web_search": "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool",
    "web_fetch": "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool",
    "structured_outputs": "https://platform.claude.com/docs/en/build-with-claude/structured-outputs",
    "pricing": "https://platform.claude.com/docs/en/about-claude/pricing",
}
PRICE = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
WEB_SEARCH_USD = 0.01
EST = {"research": 0.60, "hours": 0.40, "contact": 0.25, "draft": 0.05}  # pre-call budget guards
BETAS = ["server-side-fallback-2026-07-01"]


def tools(country: str):
    return [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8,
             "user_location": {"type": "approximate", "country": country}},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10}]


S = lambda t: {"type": t}
N = lambda t: {"type": [t, "null"]}


def obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


RESEARCH_SYSTEM = """You verify workplaces and collect their public job ads for a small local vending operator.
Rules:
- Identity: business name and street address must be shown on the employer's OWN website. Give that page URL.
  If you cannot find them there, set verified=false and say why. Never copy them from map listings.
- Job ads: open the employer's ORIGINAL ad (careers page or the agency ad that names this employer). Record its URL.
  Mark ads that say filled, closed, or past their closing date as stale.
- An ad belongs to THIS building only if it names this address/location or a site detail that matches it.
  A matching employer name or city alone is not enough: then site_match = ambiguous and say why.
- Shift times and days: copy only what the ad states explicitly (24h HH:MM, days as Mon..Sun). Never infer
  times from words like "nachtdienst". List every distinct schedule separately.
- Excerpt: at most 300 characters quoted from the ad, about the role and hours.
- If a source cannot be opened, list it under sources_failed with the reason. Never invent a result."""

RESEARCH_SCHEMA = obj({
    "business": obj({"name": N("string"), "address": N("string"), "source_url": N("string"),
                     "verified": S("boolean"), "note": S("string")}),
    "jobs": {"type": "array", "items": obj({
        "original_ad_url": S("string"),
        "ad_status": {"type": "string", "enum": ["active", "stale"]},
        "site_match": {"type": "string", "enum": ["confirmed", "ambiguous"]},
        "match_evidence": S("string"),
        "excerpt": S("string"),
        "schedules": {"type": "array", "items": obj({"start": N("string"), "end": N("string"), "days": N("string"),
                                                      "source_quote": S("string")})},
        "note": S("string")})},
    "sources_failed": {"type": "array", "items": obj({"url": S("string"), "reason": S("string")})},
})

HOURS_SYSTEM = """You verify opening hours of food and convenience venues from BUSINESS-OWNED sources only
(the venue's own website or its chain's official store page). Map listings and directories do not count.
For each venue return the hours exactly as published, in local time. Use always_open for 24/7. Include any
published holiday hours by ISO date. If no business-owned source states the hours, set verified=false."""

HOURS_SCHEMA = obj({"venues": {"type": "array", "items": obj({
    "key": S("string"), "verified": S("boolean"), "source_url": N("string"), "always_open": S("boolean"),
    "weekly": {"type": "array", "items": obj({"day": {"type": "string", "enum": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]},
                                               "open": S("string"), "close": S("string")})},
    "holidays": {"type": "array", "items": obj({"date": S("string"), "closed": S("boolean"),
                                                 "open": N("string"), "close": N("string")})},
    "holidays_same_as_regular": S("boolean"),
    "note": S("string")})}})

CONTACT_SCHEMA = obj({"recipient": N("string"), "contact_source": N("string"), "contact_page": N("string"), "note": S("string")})
DRAFT_SCHEMA = obj({"subject": S("string"), "body": S("string")})


def make_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, base_url="https://api.anthropic.com")


def cost_usd(model: str, usage) -> float:
    pin, pout = PRICE.get(model, PRICE["claude-opus-5-5"])
    c = usage.input_tokens * pin / 1e6 + usage.output_tokens * pout / 1e6
    c += (getattr(usage, "cache_read_input_tokens", 0) or 0) * pin * 0.1 / 1e6
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        c += (getattr(stu, "web_search_requests", 0) or 0) * WEB_SEARCH_USD
    return c


def _text(msg) -> str:
    return "\n".join(b.text for b in msg.content if b.type == "text")


def _check(msg) -> None:
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined: {getattr(msg, 'stop_details', None)}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Claude hit max_tokens")


class Claude:
    def __init__(self, api_key: str, model: str, country: str = "NL"):
        self.c, self.model, self.country = make_client(api_key), model, country

    def _research(self, system: str, prompt: str) -> tuple[str, float]:
        spent, messages = 0.0, [{"role": "user", "content": prompt}]
        while True:
            with self.c.beta.messages.stream(model=self.model, max_tokens=32000, system=system, messages=messages,
                                             tools=tools(self.country), thinking={"type": "adaptive"},
                                             betas=BETAS, fallbacks="default") as s:
                msg = s.get_final_message()
            spent += cost_usd(self.model, msg.usage)
            if msg.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": msg.content})
                continue
            _check(msg)
            return _text(msg), spent

    def _extract(self, schema: dict, text: str) -> tuple[dict, float]:
        msg = self.c.beta.messages.create(
            model=self.model, max_tokens=16000, betas=BETAS, fallbacks="default",
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": "Convert these findings to the schema exactly. Use null where the findings "
                       "do not state a value, and explain in the note.\n\n" + text}])
        _check(msg)
        return json.loads(_text(msg)), cost_usd(self.model, msg.usage)

    def research_site(self, hint: dict) -> tuple[dict, float]:
        t, a = self._research(RESEARCH_SYSTEM, "Workplace (live map hint, do not copy): " + json.dumps(hint) +
                              "\nVerify its identity on its own website, then collect its current public job ads.")
        d, b = self._extract(RESEARCH_SCHEMA, t)
        return d, a + b

    def verify_hours(self, venues: list[dict]) -> tuple[dict, float]:
        t, a = self._research(HOURS_SYSTEM, "Venues (live map hints with keys): " + json.dumps(venues))
        d, b = self._extract(HOURS_SCHEMA, t)
        return d, a + b

    def find_contact(self, site: dict) -> tuple[dict, float]:
        t, a = self._research("Find a PUBLIC facilities, site-services or general contact email for this specific site, "
                              "published by the employer. Give the page URL. If there is no email, give the contact page URL. "
                              "Never guess an address.", json.dumps(site))
        d, b = self._extract(CONTACT_SCHEMA, t)
        return d, a + b

    def draft_email(self, prompt: str) -> tuple[dict, float]:
        msg = self.c.beta.messages.create(model=self.model, max_tokens=4000, betas=BETAS, fallbacks="default",
                                          output_config={"format": {"type": "json_schema", "schema": DRAFT_SCHEMA}},
                                          messages=[{"role": "user", "content": prompt}])
        _check(msg)
        return json.loads(_text(msg)), cost_usd(self.model, msg.usage)

    def ping(self) -> tuple[str, float]:
        msg = self.c.messages.create(model=self.model, max_tokens=64, output_config={"effort": "low"},
                                     messages=[{"role": "user", "content": "Reply with the single word: ready"}])
        return _text(msg).strip(), cost_usd(self.model, msg.usage)
