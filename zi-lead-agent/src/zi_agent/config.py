"""Loads config.yaml (merged over defaults), resolves paths, reads .env.

Relative paths in the config are resolved against the folder that holds the
config file, so `../leads_master.xlsx` means "the workspace root".
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "enrich_enabled": False,
    "paths": {
        "master_sheet_path": "../leads_master.xlsx",
        "pending_rows_path": "../pending_rows.csv",
        "capture_tool_path": "../zoominfo_capture.py",
        "agent_workbook_path": "../lead_agent.xlsx",
        "mock_workbook_path": "out/lead_agent.mock.xlsx",
        "enrich_pending_path": "out/enriched_pending.csv",  # enriched rows saved aside if master is locked
        "mock_fixtures_path": "",  # empty = bundled fixtures
        "logs_dir": "logs",
        "reports_dir": "reports",
        "capture_log_path": "../capture_log.csv",
        "env_file": ".env",
    },
    "search": {
        # Internal filter names. ZoomInfoClient maps them to API fields (TODO(verify) in M4).
        "filters": {},
        "max_results": 1000,  # search is free; deeper paging keeps the queue full
    },
    "title_priority": [],
    "title_exclude": ["assistant", "intern", "former", "retired"],
    "allowed_countries": [],
    "employee_range": {"min": 0, "max": 1_000_000},
    "blocklist": {"companies": [], "domains": []},
    "dedupe": {"skip_companies_in_master": True},
    "caps": {
        "max_enrich_per_run": 1,
        "max_enrich_per_day": 5,
        "reserve": 20,
        "enrich_top_n": 1,
        "max_requests_per_second": 2,
    },
    "scoring": {
        "weights": {
            "title": 35, "email_hint": 20, "phone_hint": 10,
            "accuracy": 15, "size_in_range": 10, "country_allowed": 10,
        },
        "new_company_boost": 10,
    },
    "radar": {
        "enabled": False,
        "filters": {},
        "boost_days": 30,
    },
    "notify": {
        "channel": "none",  # none | slack | email
        "slack_webhook_env": "ZI_SLACK_WEBHOOK_URL",
        "email": {"smtp_host": "", "smtp_port": 587, "use_tls": True,
                  "from_addr": "", "to_addr": "",
                  "username_env": "ZI_SMTP_USERNAME", "password_env": "ZI_SMTP_PASSWORD"},
    },
    "timing": {"break_minutes": 10},
    "zoominfo": {
        "usage_limit_type": "record",      # TODO(verify) that this is the bulk-credit pool
        "enrich_output_fields": [],        # empty = client defaults
        "contact_search_attributes": {},   # exact API attributes; override the mapped filters
        "company_search_attributes": {},
    },
    "monthly_view_budget": 2000,
    "working_days_per_month": 21,
}

# OAuth client-credentials app (DevPortal) - read by ZoomInfoClient only.
SECRET_ENV_VARS = ("ZOOMINFO_CLIENT_ID", "ZOOMINFO_CLIENT_SECRET")
NOTIFY_ENV_VARS = ("ZI_SLACK_WEBHOOK_URL", "ZI_SMTP_USERNAME", "ZI_SMTP_PASSWORD")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


@dataclass
class Config:
    raw: dict
    base_dir: Path
    source: Path | None = None

    # -- paths ---------------------------------------------------------
    def path(self, key: str) -> Path:
        value = self.raw["paths"].get(key) or ""
        p = Path(value)
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    # -- typed accessors ----------------------------------------------
    @property
    def enrich_enabled(self) -> bool:
        from zi_agent import guard
        return bool(self.raw.get("enrich_enabled")) and not guard.BULK_CREDITS_LOCKED

    @property
    def search_filters(self) -> dict:
        return dict(self.raw["search"].get("filters") or {})

    @property
    def max_results(self) -> int:
        return int(self.raw["search"].get("max_results") or 200)

    @property
    def title_priority(self) -> list[list[str]]:
        """Each entry is a group of aliases; index 0 is the best title."""
        groups = []
        for entry in self.raw.get("title_priority") or []:
            if isinstance(entry, str):
                groups.append([entry])
            else:
                groups.append([str(a) for a in entry])
        return groups

    @property
    def title_exclude(self) -> list[str]:
        return [str(x) for x in self.raw.get("title_exclude") or []]

    @property
    def allowed_countries(self) -> list[str]:
        return [str(x) for x in self.raw.get("allowed_countries") or []]

    @property
    def employee_range(self) -> tuple[int, int]:
        r = self.raw.get("employee_range") or {}
        return int(r.get("min", 0)), int(r.get("max", 1_000_000))

    @property
    def blocked_companies(self) -> list[str]:
        return [str(x) for x in self.raw["blocklist"].get("companies") or []]

    @property
    def blocked_domains(self) -> list[str]:
        return [str(x) for x in self.raw["blocklist"].get("domains") or []]

    @property
    def skip_companies_in_master(self) -> bool:
        return bool(self.raw["dedupe"].get("skip_companies_in_master", True))

    @property
    def caps(self) -> dict:
        return dict(self.raw["caps"])

    @property
    def weights(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.raw["scoring"]["weights"].items()}

    @property
    def new_company_boost(self) -> int:
        return int(self.raw["scoring"].get("new_company_boost", 0))

    @property
    def monthly_view_budget(self) -> int:
        return int(self.raw.get("monthly_view_budget", 2000))

    @property
    def working_days_per_month(self) -> int:
        return int(self.raw.get("working_days_per_month", 21))


def load_config(path: str | Path | None = None) -> Config:
    """Load config.yaml. Falls back to defaults if the file is missing."""
    if path is None:
        path = Path.cwd() / "config.yaml"
    path = Path(path).resolve()
    user = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        if not isinstance(user, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
    return Config(raw=_deep_merge(DEFAULTS, user), base_dir=path.parent,
                  source=path if path.exists() else None)


def validate(cfg: Config) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not cfg.title_priority:
        errors.append("title_priority is empty: no POC could ever be picked.")
    lo, hi = cfg.employee_range
    if lo > hi:
        errors.append(f"employee_range.min ({lo}) is greater than max ({hi}).")
    w = cfg.weights
    expected = {"title", "email_hint", "phone_hint", "accuracy", "size_in_range", "country_allowed"}
    if set(w) != expected:
        errors.append(f"scoring.weights must have exactly these keys: {sorted(expected)}")
    if any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
        errors.append("scoring.weights must be non-negative and sum to more than 0.")
    caps = cfg.caps
    for key in ("max_enrich_per_run", "max_enrich_per_day", "reserve", "enrich_top_n"):
        try:
            if int(caps.get(key, -1)) < 0:
                errors.append(f"caps.{key} must be 0 or more.")
        except (TypeError, ValueError):
            errors.append(f"caps.{key} must be a whole number.")
    if not errors:
        if int(caps["max_enrich_per_run"]) > int(caps["max_enrich_per_day"]):
            warnings.append("caps.max_enrich_per_run is larger than max_enrich_per_day; the daily cap wins.")
    rps = float(caps.get("max_requests_per_second", 2))
    if rps <= 0 or rps > 5:
        errors.append("caps.max_requests_per_second must be between 0 and 5 (API limit is 25/s; stay far below).")
    if cfg.monthly_view_budget <= 0:
        errors.append("monthly_view_budget must be more than 0.")
    if not 1 <= cfg.working_days_per_month <= 31:
        errors.append("working_days_per_month must be between 1 and 31.")
    channel = str(cfg.raw["notify"].get("channel") or "none").lower()
    if channel not in ("none", "slack", "email"):
        errors.append("notify.channel must be none, slack or email.")
    if channel == "email" and not (cfg.raw["notify"]["email"].get("smtp_host")
                                   and cfg.raw["notify"]["email"].get("to_addr")):
        errors.append("notify.channel is email but notify.email.smtp_host / to_addr are empty.")
    if not cfg.allowed_countries:
        warnings.append("allowed_countries is empty: no contact gets the country points.")
    if cfg.raw.get("enrich_enabled") and not cfg.enrich_enabled:
        warnings.append("enrich_enabled is true in config but IGNORED: bulk credits are hard-locked off.")
    if cfg.enrich_enabled:
        warnings.append("enrich_enabled is true: a run with --live may spend bulk credits (within caps).")
    return errors, warnings


def load_dotenv(path: str | Path) -> list[str]:
    """Minimal .env reader. Never overrides variables that are already set.
    Returns the names it set (never the values)."""
    path = Path(path)
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ and value:
            os.environ[key] = value
            loaded.append(key)
    return loaded
