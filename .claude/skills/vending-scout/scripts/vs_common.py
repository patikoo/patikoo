"""Shared helpers: paths, config, secrets, local time, DB, permanent IDs, budget."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
SCHEMA = SCRIPTS / "schema.sql"

# Tracker folder: $VENDING_SCOUT_DIR, else "./Vending Scout" in the current directory.
TRACKER = Path(os.environ.get("VENDING_SCOUT_DIR", Path.cwd() / "Vending Scout")).resolve()
DB_PATH = TRACKER / "vending_scout.db"
CONFIG_PATH = TRACKER / "config.json"

# Secrets live outside the tracker and outside any report, readable only by the user.
SECRETS_PATH = Path(os.environ.get("VENDING_SCOUT_SECRETS", Path.home() / ".config" / "vending-scout" / "secrets.env"))
SECRET_KEYS = ("ANTHROPIC_API_KEY", "GOOGLE_MAPS_API_KEY", "TYPESAFE_API_KEY")

DEFAULT_CONFIG = {
    "center_query": None,          # town or address the user gave
    "center_lat": None,            # Places lat/lng may be cached max 30 days (Maps terms)
    "center_lng": None,
    "center_fetched_at": None,
    "radius_value": 20,
    "radius_unit": "mi",           # "mi" or "km"; default 20 miles
    "timezone": None,              # IANA name of the user's local zone
    "budget_usd": None,            # spending cap per run; the run stops before passing it
    "claude_model": "claude-opus-5-5",
    "food_radius_m": 1000,         # walking distance from a site to count a food venue
    "site_queries": [
        "distributiecentrum", "logistiek centrum", "ziekenhuis", "fabriek",
        "productiebedrijf", "verpleeghuis", "datacenter", "pakketsorteercentrum",
    ],
}


def now_local(cfg: dict | None = None) -> datetime:
    tz = (cfg or load_config()).get("timezone") or "UTC"
    return datetime.now(ZoneInfo(tz))


def now_iso(cfg: dict | None = None) -> str:
    return now_local(cfg).isoformat(timespec="seconds")


def radius_m(cfg: dict) -> int:
    factor = 1609.344 if cfg["radius_unit"] == "mi" else 1000.0
    return int(round(cfg["radius_value"] * factor))


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    TRACKER.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def load_secrets() -> dict[str, str]:
    """Environment wins over the secrets file. Values are never printed or written to reports."""
    found: dict[str, str] = {}
    if SECRETS_PATH.exists():
        for line in SECRETS_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in SECRET_KEYS and v.strip():
                    found[k.strip()] = v.strip().strip('"').strip("'")
    for k in SECRET_KEYS:
        if os.environ.get(k):
            found[k] = os.environ[k]
    return found


def ensure_secrets_file() -> Path:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS_PATH.parent, stat.S_IRWXU)
    if not SECRETS_PATH.exists():
        SECRETS_PATH.write_text(
            "# Vending Scout secrets. Keep this file private (chmod 600). Never commit it.\n"
            + "".join(f"{k}=\n" for k in SECRET_KEYS)
        )
    os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)
    return SECRETS_PATH


def connect(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA.read_text())
    con.commit()


ID_PREFIX = {"site": "site", "job": "job", "shift": "shift", "food": "food", "out": "out", "run": "run", "chk": "chk"}


def new_id(con: sqlite3.Connection, prefix: str) -> str:
    """Permanent, never-reused IDs like site-001."""
    assert prefix in ID_PREFIX
    con.execute("INSERT OR IGNORE INTO id_counters(prefix, last_value) VALUES (?, 0)", (prefix,))
    con.execute("UPDATE id_counters SET last_value = last_value + 1 WHERE prefix = ?", (prefix,))
    n = con.execute("SELECT last_value FROM id_counters WHERE prefix = ?", (prefix,)).fetchone()[0]
    return f"{prefix}-{n:03d}"


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Running cost estimate. Call `charge` after each paid call and `guard` before the next."""

    def __init__(self, cap_usd: float | None):
        self.cap = cap_usd
        self.spent = 0.0

    def guard(self, next_cost_estimate: float = 0.0) -> None:
        if self.cap is None:
            raise BudgetExceeded("No spending limit set. Ask the user for one before paid calls.")
        if self.spent + next_cost_estimate > self.cap:
            raise BudgetExceeded(f"Budget ${self.cap:.2f} would be passed (spent ~${self.spent:.2f}).")

    def charge(self, usd: float) -> None:
        self.spent += usd
