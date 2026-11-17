"""New Company Radar (weekly).

1. Company search (free). The API ANDs all filters and does not return growth
   or funding values, so the radar runs up to two searches that share the base
   filters (size, industry, country):
     - "growth" search: base + 1-year employee growth minimum
     - "funding" search: base + funding date window + funding amount range
   A company qualifies if it comes back from either; the search(es) it came
   from become its reason tags.
2. Anything whose ID is not in the "Companies seen" tab is new.
3. New companies go to the "New Companies" tab with a First Seen date. The
   daily run boosts contacts at companies first seen within radar.boost_days.

Filter names here are INTERNAL. ZoomInfoClient maps them to API fields.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from zi_agent.client import LeadClient
from zi_agent.dedupe import norm_id
from zi_agent.models import NEW_COMPANIES_TAB, Company
from zi_agent.pacing import parse_master_date
from zi_agent.storage import Storage

SEEN_TAB = "Companies seen"
SEEN_HEADERS = ["Company ID", "Company", "First Seen"]
NEW_HEADERS = ["Company ID", "Company", "Website", "Employees", "Country",
               "1y Growth %", "Last Funding Date", "Last Funding Amount", "Reason", "First Seen"]

BASE_KEYS = ("industries", "countries", "employee_min", "employee_max")
GROWTH_KEYS = ("employee_growth_1y_min_pct",)
FUNDING_KEYS = ("funded_within_days", "funding_date_from", "funding_date_to",
                "funding_amount_min", "funding_amount_max")


def resolve_funding_dates(filters: dict, today: dt.date) -> dict:
    """Turn funded_within_days into an explicit funding_date_from (YYYY-MM-DD)."""
    f = {k: v for k, v in (filters or {}).items() if v not in (None, "", [])}
    days = f.pop("funded_within_days", None)
    if days:
        start = today - dt.timedelta(days=int(days))
        given = parse_master_date(f.get("funding_date_from"))
        f["funding_date_from"] = max(filter(None, [start, given])).isoformat()
    return f


def build_queries(filters: dict, today: dt.date) -> dict[str, dict]:
    f = resolve_funding_dates(filters, today)
    base = {k: f[k] for k in BASE_KEYS if k in f}
    queries = {}
    if any(k in f for k in GROWTH_KEYS):
        queries["growth"] = {**base, **{k: f[k] for k in GROWTH_KEYS if k in f}}
    if any(k in f for k in FUNDING_KEYS):
        queries["funding"] = {**base, **{k: f[k] for k in FUNDING_KEYS if k in f}}
    if not queries:
        queries["base"] = base
    return queries


def matches_filters(c: Company, f: dict) -> bool:
    """AND of all given filters, the way the API applies them. Used by the
    MockClient. f must already have funded_within_days resolved."""
    if f.get("employee_min") is not None and (c.employee_count or 0) < int(f["employee_min"]):
        return False
    if f.get("employee_max") is not None and (c.employee_count or 0) > int(f["employee_max"]):
        return False
    if f.get("industries") and c.industry.strip().lower() not in {i.lower() for i in f["industries"]}:
        return False
    if f.get("countries") and c.country.strip().lower() not in {x.lower() for x in f["countries"]}:
        return False
    if f.get("employee_growth_1y_min_pct") is not None:
        if c.employee_growth_1y_pct is None or c.employee_growth_1y_pct < float(f["employee_growth_1y_min_pct"]):
            return False
    if any(f.get(k) is not None for k in FUNDING_KEYS):
        fd = parse_master_date(c.last_funding_date)
        if fd is None:
            return False
        start, end = parse_master_date(f.get("funding_date_from")), parse_master_date(f.get("funding_date_to"))
        if (start and fd < start) or (end and fd > end):
            return False
        amt = c.last_funding_amount or 0
        if f.get("funding_amount_min") is not None and amt < float(f["funding_amount_min"]):
            return False
        if f.get("funding_amount_max") is not None and amt > float(f["funding_amount_max"]):
            return False
    return True


def fmt_money(amount: float) -> str:
    if amount >= 1e9:
        return f"${amount / 1e9:.1f}B"
    if amount >= 1e6:
        return f"${amount / 1e6:.1f}M"
    if amount >= 1e3:
        return f"${amount / 1e3:.0f}K"
    return f"${amount:.0f}"


def reason_tags(c: Company, matched: set[str], queries: dict[str, dict], today: dt.date) -> list[str]:
    tags = []
    if "funding" in matched:
        fd = parse_master_date(c.last_funding_date)
        q = queries["funding"]
        if fd and (today - fd).days <= 90:
            tags.append("funded in last 90 days")
        elif fd:
            tags.append(f"funded {fd.isoformat()}")
        else:  # live API: value not returned, only the window it matched
            start = parse_master_date(q.get("funding_date_from"))
            if start and (today - start).days <= 92:
                tags.append("funded in last 90 days")
            else:
                tags.append(f"funded since {q.get('funding_date_from', '?')}")
        if c.last_funding_amount:
            tags.append(f"raised {fmt_money(c.last_funding_amount)}")
    if "growth" in matched:
        g = c.employee_growth_1y_pct
        gmin = float(queries["growth"]["employee_growth_1y_min_pct"])
        tags.append(f"headcount growth {g:.0f}% in 1y" if g is not None
                    else f"headcount growth over {gmin:.0f}% in 1y")
    return tags or ["matches radar filters"]


@dataclass
class RadarResult:
    found: int = 0
    new: list[Company] = field(default_factory=list)
    already_seen: int = 0
    queries: dict[str, int] = field(default_factory=dict)


def run_radar(client: LeadClient, storage: Storage, filters: dict, max_results: int,
              today: dt.date) -> RadarResult:
    res = RadarResult()
    queries = build_queries(filters, today)
    found: dict[str, Company] = {}
    matched: dict[str, set[str]] = {}
    for name, q in queries.items():
        companies = client.search_companies(q, max_results)
        res.queries[name] = len(companies)
        for c in companies:
            cid = norm_id(c.company_id)
            if cid:
                found.setdefault(cid, c)
                matched.setdefault(cid, set()).add(name)
    res.found = len(found)

    _, seen_rows = storage.read_table(SEEN_TAB)
    seen = {norm_id(r.get("Company ID")) for r in seen_rows}
    new_headers, new_rows = storage.read_table(NEW_COMPANIES_TAB)

    for cid, c in found.items():
        if cid in seen:
            res.already_seen += 1
            continue
        seen.add(cid)
        res.new.append(c)
        seen_rows.append({"Company ID": cid, "Company": c.name, "First Seen": today.isoformat()})
        new_rows.append({
            "Company ID": cid, "Company": c.name, "Website": c.website,
            "Employees": c.employee_count if c.employee_count is not None else "",
            "Country": c.country,
            "1y Growth %": round(c.employee_growth_1y_pct, 1) if c.employee_growth_1y_pct is not None else "",
            "Last Funding Date": c.last_funding_date,
            "Last Funding Amount": fmt_money(c.last_funding_amount) if c.last_funding_amount else "",
            "Reason": "; ".join(reason_tags(c, matched[cid], queries, today)),
            "First Seen": today.isoformat(),
        })

    new_rows.sort(key=lambda r: str(r.get("First Seen") or ""), reverse=True)
    headers = NEW_HEADERS + [h for h in new_headers if h and h not in NEW_HEADERS]
    storage.write_table(SEEN_TAB, SEEN_HEADERS, seen_rows)
    storage.write_table(NEW_COMPANIES_TAB, headers, new_rows)
    return res


def find_similar_companies(client: LeadClient, company_id: str) -> list[Company]:
    """Lookalike ("find similar") search: STUB.

    The official docs list GET /copilot/v1/companies/lookalikes
    (filter[companyId], scope api:recommendations:read), but its credit cost
    is not documented. TODO(verify) cost and response mapping before enabling.
    """
    raise NotImplementedError("Lookalike search is a stub. TODO(verify) credit cost in the official docs.")
