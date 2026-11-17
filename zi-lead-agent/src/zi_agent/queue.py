"""To-View Queue merge logic.

Rules:
- Existing rows are kept with all their values, including columns the user
  added. Claimed By and Status are never overwritten, with one exception:
  open rows (New/Claimed) whose contact is now in the master sheet become Done.
- New candidates are appended as Status=New.
- Open rows are sorted by Fit Score (highest first) and ranked 1..n;
  Done/Skipped rows go to the bottom without a rank.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from zi_agent.dedupe import SeenIndex, norm_id
from zi_agent.models import (OPEN_STATUSES, QUEUE_HEADERS, STATUS_DONE, STATUS_NEW, STATUS_SKIPPED,
                             Contact)


@dataclass
class QueueCandidate:
    contact: Contact
    score: int
    breakdown: str
    reason: str


@dataclass
class MergeStats:
    added: int = 0
    marked_done: int = 0
    skipped: int = 0
    kept: int = 0
    added_ids: list[str] = field(default_factory=list)


def _as_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _status(row: dict) -> str:
    return str(row.get("Status") or "").strip()


def is_open(row: dict) -> bool:
    return _status(row) in OPEN_STATUSES


def merge_headers(existing_headers: list[str]) -> list[str]:
    """Standard columns first, then any columns the user added."""
    extra = [h for h in existing_headers if h and h not in QUEUE_HEADERS]
    return list(QUEUE_HEADERS) + extra


def _add_reason(row: dict, text: str) -> None:
    row["Reason"] = "; ".join(p for p in (str(row.get("Reason") or ""), text) if p)


def merge_queue(existing_rows: list[dict], candidates: list[QueueCandidate],
                master: SeenIndex, today: dt.date, blocklist=None,
                skip_companies_in_master: bool = False) -> tuple[list[dict], MergeStats]:
    stats = MergeStats()
    rows = [dict(r) for r in existing_rows if any(v not in (None, "") for v in r.values())]
    by_id = {norm_id(r.get("Contact ID")): r for r in rows if norm_id(r.get("Contact ID"))}
    stats.kept = len(rows)

    for r in rows:
        if is_open(r) and master.match(r.get("Contact ID"), r.get("POC"), r.get("Company")):
            r["Status"] = STATUS_DONE
            stats.marked_done += 1
    # Rows nobody has claimed yet are closed when their company became off-limits
    # (blocklisted later, or another POC of that company was captured).
    for r in rows:
        if _status(r) != STATUS_NEW:
            continue
        if blocklist is not None and blocklist.reason(r.get("Company"), ""):
            r["Status"] = STATUS_SKIPPED
            _add_reason(r, "company now blocklisted")
            stats.skipped += 1
        elif skip_companies_in_master and master.has_company(r.get("Company ID"), r.get("Company")):
            r["Status"] = STATUS_SKIPPED
            _add_reason(r, "another POC of this company is already in the master sheet")
            stats.skipped += 1

    for cand in candidates:
        c = cand.contact
        existing = by_id.get(norm_id(c.contact_id))
        if existing is not None:
            # Refresh computed columns only; never touch manual columns.
            existing["Fit Score"] = cand.score
            existing["Score Breakdown"] = cand.breakdown
            continue
        row = {h: "" for h in QUEUE_HEADERS}
        row.update({
            "Fit Score": cand.score,
            "Score Breakdown": cand.breakdown,
            "POC": c.full_name,
            "Job title": c.job_title,
            "Company": c.company_name,
            "Contact ID": norm_id(c.contact_id),
            "Company ID": norm_id(c.company_id),
            "Reason": cand.reason,
            "Claimed By": "",
            "Status": STATUS_NEW,
            "Added On": today.isoformat(),
        })
        rows.append(row)
        by_id[norm_id(c.contact_id)] = row
        stats.added += 1
        stats.added_ids.append(norm_id(c.contact_id))

    open_rows = [r for r in rows if is_open(r)]
    closed_rows = [r for r in rows if not is_open(r)]
    open_rows.sort(key=lambda r: (-_as_int(r.get("Fit Score")), str(r.get("Added On") or ""),
                                  norm_id(r.get("Contact ID"))))
    for i, r in enumerate(open_rows, 1):
        r["Rank"] = i
    for r in closed_rows:
        r["Rank"] = ""
    return open_rows + closed_rows, stats


def open_company_index(rows: list[dict]) -> SeenIndex:
    """Companies that already have an open (New/Claimed) queue row."""
    return SeenIndex.from_queue_rows([r for r in rows if is_open(r)], label="open queue")
