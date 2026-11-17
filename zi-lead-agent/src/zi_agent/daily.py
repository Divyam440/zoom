"""The daily pipeline: search -> blocklist -> dedupe -> POC pick -> score -> queue."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from zi_agent.blocklist import Blocklist
from zi_agent.client import LeadClient
from zi_agent.config import Config
from zi_agent.dedupe import SeenIndex, dedupe_contacts, norm_id
from zi_agent.models import NEW_COMPANIES_TAB, QUEUE_TAB, STATUSES, Contact
from zi_agent.poc import pick_pocs
from zi_agent.queue import QueueCandidate, merge_headers, merge_queue, open_company_index
from zi_agent.scoring import score_contact
from zi_agent.storage import Storage

log = logging.getLogger(__name__)


@dataclass
class DailyResult:
    searched: int = 0
    blocklisted: list[tuple[Contact, str]] = field(default_factory=list)
    duplicates: list[tuple[Contact, str]] = field(default_factory=list)
    company_already_handled: list[Contact] = field(default_factory=list)
    companies_no_title: list[str] = field(default_factory=list)
    added: int = 0
    marked_done: int = 0
    auto_skipped: int = 0
    open_rows: int = 0
    total_rows: int = 0
    queue_rows: list[dict] = field(default_factory=list)
    queue_headers: list[str] = field(default_factory=list)
    master_index: SeenIndex | None = None


def new_company_ids(storage: Storage, today: dt.date, boost_days: int) -> set[str]:
    """Company IDs the weekly radar first saw within the last boost_days."""
    from zi_agent.pacing import parse_master_date
    _, rows = storage.read_table(NEW_COMPANIES_TAB)
    out = set()
    for r in rows:
        first = parse_master_date(r.get("First Seen"))
        if norm_id(r.get("Company ID")) and first and (today - first).days <= boost_days:
            out.add(norm_id(r.get("Company ID")))
    return out


def build_master_index(storage: Storage) -> SeenIndex:
    rows = storage.read_master() + storage.read_pending()
    return SeenIndex.from_master_rows(rows)


def run_daily(cfg: Config, client: LeadClient, storage: Storage, today: dt.date) -> DailyResult:
    res = DailyResult()

    contacts = client.search_contacts(cfg.search_filters, cfg.max_results)
    res.searched = len(contacts)

    blocklist = Blocklist(cfg.blocked_companies, cfg.blocked_domains)
    contacts, res.blocklisted = blocklist.split(contacts)

    master = build_master_index(storage)
    res.master_index = master
    existing_headers, existing_rows = storage.read_table(QUEUE_TAB)
    queue_all = SeenIndex.from_queue_rows(existing_rows)
    deduped = dedupe_contacts(contacts, master, queue_all)
    res.duplicates = deduped.dropped

    # One POC per company: skip companies with an open queue row (and, if
    # configured, companies already in the master sheet).
    open_cos = open_company_index(existing_rows)
    remaining = []
    for c in deduped.kept:
        if open_cos.has_company(c.company_id, c.company_name) or (
                cfg.skip_companies_in_master and master.has_company(c.company_id, c.company_name)):
            res.company_already_handled.append(c)
        else:
            remaining.append(c)

    picks, res.companies_no_title = pick_pocs(remaining, cfg.title_priority, cfg.title_exclude)

    new_cos = new_company_ids(storage, today, int(cfg.raw["radar"].get("boost_days", 30)))
    n_prio = len(cfg.title_priority)
    candidates = []
    for p in picks:
        is_new = norm_id(p.contact.company_id) in new_cos
        s = score_contact(p.contact, p.rank, n_prio, cfg.weights, cfg.employee_range,
                          cfg.allowed_countries, cfg.new_company_boost, is_new)
        reason = f"Best title at company: '{p.matched_title}' (#{p.rank + 1})"
        if is_new:
            reason += "; new company (radar)"
        candidates.append(QueueCandidate(p.contact, s.score, s.breakdown, reason))

    rows, stats = merge_queue(existing_rows, candidates, master, today, blocklist=blocklist,
                              skip_companies_in_master=cfg.skip_companies_in_master)
    res.added, res.marked_done, res.auto_skipped = stats.added, stats.marked_done, stats.skipped
    res.queue_rows = rows
    res.queue_headers = merge_headers(existing_headers)
    res.open_rows = sum(1 for r in rows if r.get("Rank") not in ("", None))
    res.total_rows = len(rows)
    log.info("daily: searched=%d blocklisted=%d duplicates=%d company_handled=%d no_title=%d "
             "added=%s marked_done=%d", res.searched, len(res.blocklisted), len(res.duplicates),
             len(res.company_already_handled), len(res.companies_no_title), stats.added_ids,
             stats.marked_done)
    return res


def save_queue(storage: Storage, res: DailyResult) -> None:
    storage.write_table(QUEUE_TAB, res.queue_headers, res.queue_rows,
                        validations={"Status": list(STATUSES)})
