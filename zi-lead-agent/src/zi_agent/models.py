"""Data models and the column layouts shared with zoominfo_capture.py.

The master-sheet headers come from zoominfo_capture.py (read with `ast`, so the
capture tool's code is never executed). The two ZoomInfo ID columns are added
AFTER the capture tool's columns; the first 11 are never reordered.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field, fields
from pathlib import Path

# Mirror of zoominfo_capture.HEADERS / IMPORTANT_FIELDS, used only if the
# capture tool cannot be read. tests/test_models.py checks they still match.
FALLBACK_CAPTURE_HEADERS = (
    "Company Name", "Employees", "Industry", "Website", "Company Linkedin Url",
    "POC", "Job title", "Email ID", "Company Country", "Company Phone", "Date",
    "Mobile Phone", "POC Linkedin Url", "Missing Fields",
    "Call Status", "Verified", "Notes",
)
FALLBACK_IMPORTANT_FIELDS = (
    "Company Name", "Employees", "Industry", "Website", "Company Linkedin Url",
    "POC", "Job title", "Email ID", "Company Country", "Company Phone",
)
ZI_ID_HEADERS = ("ZoomInfo Contact ID", "ZoomInfo Company ID")
MASTER_DATE_FORMAT = "%d-%m-%Y"  # same as zoominfo_capture.parse_profile

# --- To-View Queue tab ---------------------------------------------------
QUEUE_TAB = "To-View Queue"
QUEUE_HEADERS = (
    "Rank", "Fit Score", "Score Breakdown", "POC", "Job title", "Company",
    "Contact ID", "Company ID", "Reason", "Claimed By", "Status", "Added On",
)
STATUS_NEW, STATUS_CLAIMED, STATUS_DONE, STATUS_SKIPPED = "New", "Claimed", "Done", "Skipped"
STATUSES = (STATUS_NEW, STATUS_CLAIMED, STATUS_DONE, STATUS_SKIPPED)
OPEN_STATUSES = {STATUS_NEW, STATUS_CLAIMED, ""}

NEW_COMPANIES_TAB = "New Companies"


@dataclass(frozen=True)
class CaptureConventions:
    headers: tuple[str, ...]
    important_fields: tuple[str, ...]
    source: str  # path it was read from, or "fallback"

    @property
    def master_headers(self) -> tuple[str, ...]:
        return self.headers + tuple(h for h in ZI_ID_HEADERS if h not in self.headers)


def load_capture_conventions(path: str | Path | None) -> CaptureConventions:
    """Read HEADERS and IMPORTANT_FIELDS literals from zoominfo_capture.py."""
    fallback = CaptureConventions(FALLBACK_CAPTURE_HEADERS, FALLBACK_IMPORTANT_FIELDS, "fallback")
    if not path or not Path(path).exists():
        return fallback
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return fallback
    found: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("HEADERS", "IMPORTANT_FIELDS"):
                try:
                    found[name] = tuple(str(x) for x in ast.literal_eval(node.value))
                except ValueError:
                    pass
    if "HEADERS" not in found:
        return fallback
    return CaptureConventions(
        headers=found["HEADERS"],
        important_fields=found.get("IMPORTANT_FIELDS", FALLBACK_IMPORTANT_FIELDS),
        source=str(path),
    )


# --- API-facing models (internal shape; clients map API fields to these) ---
@dataclass
class Contact:
    """A contact search result. Search results carry no emails or phones,
    only hints about which data exists."""
    contact_id: str
    first_name: str
    last_name: str
    job_title: str
    company_id: str
    company_name: str
    website: str = ""
    employee_count: int | None = None
    industry: str = ""
    country: str = ""
    accuracy_score: int | None = None
    has_email: bool = False
    has_direct_phone: bool = False
    has_mobile_phone: bool = False
    has_company_phone: bool = False
    # Things the search filter already guaranteed although the value is not
    # returned (live contact search has no employee count/country values).
    filter_matched: tuple[str, ...] = ()

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    @classmethod
    def from_dict(cls, d: dict) -> "Contact":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["contact_id"] = str(data["contact_id"])
        data["company_id"] = str(data.get("company_id", ""))
        return cls(**data)


@dataclass
class EnrichedContact:
    """Full record returned by an enrich call."""
    contact_id: str
    company_id: str
    first_name: str = ""
    last_name: str = ""
    job_title: str = ""
    company_name: str = ""
    employees: str = ""
    industry: str = ""
    website: str = ""
    company_linkedin_url: str = ""
    email: str = ""
    company_country: str = ""
    company_phone: str = ""
    mobile_phone: str = ""
    direct_phone: str = ""
    poc_linkedin_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


@dataclass
class Company:
    """A company search result (used by the radar in Milestone 3)."""
    company_id: str
    name: str
    website: str = ""
    employee_count: int | None = None
    industry: str = ""
    country: str = ""
    employee_growth_1y_pct: float | None = None
    last_funding_date: str = ""
    last_funding_amount: float | None = None


@dataclass
class UsageInfo:
    """Bulk-credit usage as reported by the API usage endpoint."""
    remaining_credits: int | None
    limit: int | None = None
    used: int | None = None
