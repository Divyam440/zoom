"""Optional enrichment of the top queue rows (live mode only, via CreditGuard)."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from zi_agent.blocklist import Blocklist, domain_matches, normalize_domain
from zi_agent.client import LeadClient
from zi_agent.dedupe import SeenIndex, norm_id
from zi_agent.guard import CreditGuard, CreditGuardError
from zi_agent.models import (MASTER_DATE_FORMAT, STATUS_DONE, STATUS_NEW, STATUS_SKIPPED,
                             CaptureConventions, EnrichedContact)
from zi_agent.queue import is_open
from zi_agent.storage import Storage, StorageLocked

log = logging.getLogger(__name__)

DOMAIN_MISMATCH_NOTE = "Email domain does not match website"


def email_domain_mismatch(email: str, website: str) -> bool:
    e, w = normalize_domain(email), normalize_domain(website)
    if not e or not w:
        return False
    return not (domain_matches(e, w) or domain_matches(w, e))


def to_master_row(e: EnrichedContact, conv: CaptureConventions, today: dt.date) -> dict:
    row = {h: "" for h in conv.master_headers}
    row.update({
        "Company Name": e.company_name,
        "Employees": e.employees,
        "Industry": e.industry,
        "Website": e.website,
        "Company Linkedin Url": e.company_linkedin_url,
        "POC": e.full_name,
        "Job title": e.job_title,
        "Email ID": e.email,
        "Company Country": e.company_country,
        "Company Phone": e.company_phone or e.direct_phone,
        "Date": today.strftime(MASTER_DATE_FORMAT),
        "Mobile Phone": e.mobile_phone,
        "POC Linkedin Url": e.poc_linkedin_url,
        "ZoomInfo Contact ID": norm_id(e.contact_id),
        "ZoomInfo Company ID": norm_id(e.company_id),
    })
    if "Call Status" in row:
        row["Call Status"] = "Not called"  # same default the capture tool writes
    row["Missing Fields"] = ", ".join(f for f in conv.important_fields if not row.get(f))
    notes = ["Added by zi-lead-agent (API enrich)"]
    if email_domain_mismatch(e.email, e.website):
        notes.append(DOMAIN_MISMATCH_NOTE)
    row["Notes"] = "; ".join(notes)
    return row


def select_for_enrichment(queue_rows: list[dict], top_n: int) -> list[str]:
    """Top N open rows with Status=New (Claimed rows are left to the human)."""
    rows = [r for r in queue_rows if is_open(r) and str(r.get("Status") or "") == STATUS_NEW]

    def rank(r):
        try:
            return int(float(r.get("Rank")))
        except (TypeError, ValueError):
            return 10**9
    rows.sort(key=rank)
    return [norm_id(r.get("Contact ID")) for r in rows[:max(0, top_n)] if norm_id(r.get("Contact ID"))]


@dataclass
class EnrichResult:
    status: str
    appended_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    blocked_ids: list[str] = field(default_factory=list)
    mismatch_ids: list[str] = field(default_factory=list)
    credits_used: int = 0


def run_enrichment(*, client: LeadClient, guard: CreditGuard, storage: Storage,
                   conventions: CaptureConventions, queue_rows: list[dict], top_n: int,
                   master: SeenIndex, today: dt.date, blocklist: Blocklist | None = None) -> EnrichResult:
    """Enrich top queue rows, append new ones to the master, mark them Done in queue_rows."""
    wanted = select_for_enrichment(queue_rows, top_n)
    if not wanted:
        return EnrichResult("nothing to enrich")
    if not storage.master_writable():
        return EnrichResult("skipped: master sheet is open in Excel (no credits spent)")
    try:
        permit = guard.issue_permit(wanted)
    except CreditGuardError as e:
        return EnrichResult(str(e))

    # If the call raises, the permit stays reserved (conservative) for this run.
    records = client.enrich_contacts(permit)
    # Worst case: 1 credit per returned record. TODO(verify) in M4 whether the
    # enrich response says which records were actually charged.
    credits = len(records)
    guard.settle(permit, credits)
    result = EnrichResult("ok", credits_used=credits)

    new_rows = []
    for e in records:
        cid = norm_id(e.contact_id)
        if master.match(cid, e.full_name, e.company_name, e.email):
            result.duplicate_ids.append(cid)
            continue
        # Live search returns no website, so the domain blocklist is re-checked here.
        if blocklist and blocklist.reason(e.company_name, e.website):
            result.blocked_ids.append(cid)
            continue
        row = to_master_row(e, conventions, today)
        if DOMAIN_MISMATCH_NOTE in row["Notes"]:
            result.mismatch_ids.append(cid)
        new_rows.append(row)
        master.add(contact_id=cid, poc=e.full_name, company=e.company_name,
                   email=e.email, company_id=e.company_id)
        result.appended_ids.append(cid)
    if new_rows:
        try:
            storage.append_master(new_rows)
        except StorageLocked:
            # Credits are spent: never lose the data. Added on the next run.
            path = storage.save_enrich_pending(new_rows)
            result.status = f"ok (master was locked; rows saved to {path.name}, added next run)"
    done = set(result.appended_ids) | set(result.duplicate_ids)
    for r in queue_rows:
        rid = norm_id(r.get("Contact ID"))
        if rid in done and is_open(r):
            r["Status"] = STATUS_DONE
        elif rid in result.blocked_ids and is_open(r):
            r["Status"] = STATUS_SKIPPED
            r["Reason"] = (str(r.get("Reason") or "") + "; blocklisted domain (found at enrich)").lstrip("; ")
    log.info("enrich: requested=%s appended=%s duplicates=%s mismatch=%s credits=%s",
             list(permit.contact_ids), result.appended_ids, result.duplicate_ids,
             result.mismatch_ids, credits)
    return result
