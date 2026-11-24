"""Claude does the research and writing (official `anthropic` Python SDK).

Two steps per site: (1) research with Anthropic's server-side web search / web fetch tools,
(2) convert the findings into a fixed JSON schema with structured outputs (no tools).
Server-side refusal fallbacks are enabled (`fallbacks="default"`).
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
WEB_SEARCH_USD = 0.01           # $10 per 1,000 searches
EST_SITE_USD = 0.60             # pre-call guard estimate for one site's research + extraction
BETAS = ["server-side-fallback-2026-07-01"]
TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 6,
     "user_location": {"type": "approximate", "country": "NL"}},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6},
]

RESEARCH_SYSTEM = """You research workplaces as possible vending machine locations for a small local operator.
Rules:
- Business name and address must come from the business's OWN website (give the exact page URL). Do not copy them from Google Maps.
- Only report job ads you actually opened, with their URL. Quote at most 300 characters of the ad that describe the role and hours.
- If an ad lists several schedules (for example a day rota and a night rota), list each one separately.
- Night headcount: report a number only if a source states it (give the URL and quote). Never estimate.
- Cafeteria / existing vending: report only what a source says; otherwise say unknown.
- Say plainly when something could not be found."""

EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["business", "jobs", "headcount", "cafeteria", "vending_contract", "contact"],
    "properties": {
        "business": {"type": "object", "additionalProperties": False,
                     "required": ["name", "address", "source_url", "note"],
                     "properties": {"name": {"type": ["string", "null"]}, "address": {"type": ["string", "null"]},
                                    "source_url": {"type": ["string", "null"]}, "note": {"type": "string"}}},
        "jobs": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["source_url", "ad_status", "excerpt", "schedules", "note"],
            "properties": {
                "source_url": {"type": "string"},
                "ad_status": {"type": "string", "enum": ["active", "expired", "unknown"]},
                "excerpt": {"type": "string"},
                "schedules": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["start", "end", "days", "note"],
                    "properties": {"start": {"type": ["string", "null"], "description": "HH:MM 24h"},
                                   "end": {"type": ["string", "null"], "description": "HH:MM 24h"},
                                   "days": {"type": ["string", "null"], "description": "Comma list of Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
                                   "note": {"type": "string"}}}},
                "note": {"type": "string"}}}},
        "headcount": {"type": "object", "additionalProperties": False, "required": ["night_count", "source", "note"],
                      "properties": {"night_count": {"type": ["integer", "null"]}, "source": {"type": ["string", "null"]},
                                     "note": {"type": "string"}}},
        "cafeteria": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "vending_contract": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "contact": {"type": "object", "additionalProperties": False, "required": ["recipient", "source", "note"],
                    "properties": {"recipient": {"type": ["string", "null"]}, "source": {"type": ["string", "null"]},
                                   "note": {"type": "string"}}},
    },
}


def make_client(api_key: str) -> anthropic.Anthropic:
    # Pin the public endpoint so a stray ANTHROPIC_BASE_URL never redirects the user's key.
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
        raise RuntimeError("Claude hit max_tokens; rerun this site.")


def research_site(client, model: str, hint: dict) -> tuple[dict, float]:
    """`hint` is live, unstored context: {'website': ..., 'label': ..., 'town': ...}. Returns (data, usd)."""
    spent = 0.0
    messages = [{"role": "user", "content":
                 f"Workplace to research (from a live map lookup, not to be copied): {json.dumps(hint)}\n"
                 "Find: official name and address on its own website; current job ads for this location "
                 "(company careers page, werk.nl, indeed.nl, agencies) with shift times; any stated number of night-shift "
                 "staff; whether there is a staff canteen and whether a vending operator already serves the site; "
                 "a published general or facilities contact."}]
    while True:
        with client.beta.messages.stream(model=model, max_tokens=32000, system=RESEARCH_SYSTEM, messages=messages,
                                         tools=TOOLS, thinking={"type": "adaptive"}, betas=BETAS,
                                         fallbacks="default") as s:
            msg = s.get_final_message()
        spent += cost_usd(model, msg.usage)
        if msg.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": msg.content})
            continue
        _check(msg)
        break
    findings = _text(msg)
    ext = client.beta.messages.create(
        model=model, max_tokens=16000, betas=BETAS, fallbacks="default",
        output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
        messages=[{"role": "user", "content": "Convert these research findings to the schema. Use null where the "
                   "findings do not state a value and explain why in the note.\n\n" + findings}])
    spent += cost_usd(model, ext.usage)
    _check(ext)
    return json.loads(_text(ext)), spent


def draft_outreach(client, model: str, site: dict, summary: str, language: str = "nl") -> tuple[dict, float]:
    schema = {"type": "object", "additionalProperties": False, "required": ["subject", "body"],
              "properties": {"subject": {"type": "string"}, "body": {"type": "string"}}}
    msg = client.beta.messages.create(
        model=model, max_tokens=4000, betas=BETAS, fallbacks="default",
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content":
                   f"Write a short, polite first email (language: {language}) from a local vending operator to {site['name']} "
                   f"offering to place a vending machine for night-shift staff at no cost to them. Facts you may use: {summary}. "
                   "No invented numbers or claims. Under 150 words. Leave [Your name] and [phone] placeholders."}])
    _check(msg)
    return json.loads(_text(msg)), cost_usd(model, msg.usage)


def ping(client, model: str) -> tuple[str, float]:
    msg = client.messages.create(model=model, max_tokens=64, output_config={"effort": "low"},
                                 messages=[{"role": "user", "content": "Reply with the single word: ready"}])
    return _text(msg).strip(), cost_usd(model, msg.usage)
