import datetime as dt

import pytest

from zi_agent.client import MockClient
from zi_agent.daily import run_daily
from zi_agent.models import NEW_COMPANIES_TAB, Company
from zi_agent.radar import (SEEN_TAB, build_queries, find_similar_companies, matches_filters,
                            reason_tags, run_radar)

TODAY = dt.date(2026, 11, 16)


def test_queries_split_growth_and_funding(cfg):
    q = build_queries(cfg.raw["radar"]["filters"], TODAY)
    assert set(q) == {"growth", "funding"}
    assert q["growth"]["employee_growth_1y_min_pct"] == 20
    assert "funding_amount_min" not in q["growth"]
    assert q["funding"]["funding_date_from"] == "2025-11-16"   # 365 days back
    for name in q:
        assert q[name]["employee_min"] == 50 and q[name]["employee_max"] == 2000


def test_matches_filters_is_and():
    c = Company("x", "X", employee_count=100, country="Canada", employee_growth_1y_pct=25)
    assert matches_filters(c, {"employee_min": 50, "employee_growth_1y_min_pct": 20})
    assert not matches_filters(c, {"employee_min": 50, "employee_growth_1y_min_pct": 30})
    assert not matches_filters(c, {"funding_date_from": "2026-01-01"})   # no funding data


def test_radar_new_then_seen(cfg, storage):
    client = MockClient(today=TODAY)
    radar = cfg.raw["radar"]
    res = run_radar(client, storage, radar["filters"], 100, TODAY)
    assert res.queries == {"growth": 4, "funding": 4}
    assert sorted(c.company_id for c in res.new) == ["c-1001", "c-1007", "c-2001", "c-2002",
                                                     "c-2003", "c-2006"]
    _, rows = storage.read_table(NEW_COMPANIES_TAB)
    by = {r["Company ID"]: r for r in rows}
    assert by["c-1001"]["Reason"] == ("funded in last 90 days; raised $18.0M; "
                                      "headcount growth 32% in 1y")
    assert by["c-2006"]["Reason"] == "headcount growth 21% in 1y"
    assert by["c-1007"]["Reason"].startswith("funded 2026-")
    assert by["c-1001"]["First Seen"] == "2026-11-16"
    assert "c-2004" not in by and "c-2005" not in by

    # user edits survive; second run finds nothing new
    rows[0]["My Note"] = "call later"
    storage.write_table(NEW_COMPANIES_TAB, list(rows[0].keys()), rows)
    res2 = run_radar(client, storage, radar["filters"], 100, TODAY + dt.timedelta(days=7))
    assert res2.new == [] and res2.already_seen == 6
    headers, rows2 = storage.read_table(NEW_COMPANIES_TAB)
    assert "My Note" in headers and len(rows2) == 6
    assert len(storage.read_table(SEEN_TAB)[1]) == 6


def test_reason_tags_without_values_live_style():
    c = Company("x", "X")  # live API returns no growth/funding values
    queries = {"growth": {"employee_growth_1y_min_pct": 20},
               "funding": {"funding_date_from": "2026-08-18"}}
    tags = reason_tags(c, {"growth", "funding"}, queries, TODAY)
    assert tags == ["funded in last 90 days", "headcount growth over 20% in 1y"]


def test_new_company_boost_in_daily_queue(cfg, storage):
    storage.ensure_master()
    run_radar(MockClient(today=TODAY), storage, cfg.raw["radar"]["filters"], 100, TODAY)
    res = run_daily(cfg, MockClient(), storage, TODAY)
    by = {r["Contact ID"]: r for r in res.queue_rows}
    assert "new company (radar)" in by["9007004"]["Reason"]       # Lumen
    assert by["9007004"]["Fit Score"] == 79                       # 69 + 10
    assert "new co +10" in by["9007004"]["Score Breakdown"]
    assert "new company" not in by["9002002"]["Reason"]           # Bluepeak not new

    # boost expires after boost_days
    later = TODAY + dt.timedelta(days=31)
    storage.write_table("To-View Queue", ["Rank"], [])
    res2 = run_daily(cfg, MockClient(), storage, later)
    by2 = {r["Contact ID"]: r for r in res2.queue_rows}
    assert by2["9007004"]["Fit Score"] == 69


def test_lookalike_is_stub():
    with pytest.raises(NotImplementedError):
        find_similar_companies(MockClient(), "c-1001")
