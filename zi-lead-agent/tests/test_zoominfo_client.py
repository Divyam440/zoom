"""ZoomInfoClient against recorded-style fixtures. No network access."""
import base64
import json
import urllib.parse
from pathlib import Path

import pytest

from zi_agent.calllog import ApiCallLog
from zi_agent.client import (HttpResponse, ZoomInfoApiError, ZoomInfoClient, ZoomInfoConfigError,
                             map_company_filters, map_contact_filters)
from zi_agent.guard import CreditGuard
from zi_agent.scoring import score_contact

FIX = json.loads((Path(__file__).parent / "fixtures" / "zoominfo" / "responses.json").read_text("utf-8"))
BASE = "https://api.zoominfo.com/gtm"


class FakeTransport:
    """Replays queued responses per (method, path) and records every request."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list[HttpResponse]] = {}
        self.calls: list[dict] = []

    def add(self, method, path, body=None, status=200, headers=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.routes.setdefault((method, path), []).append(HttpResponse(status, headers or {}, raw))

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path[len("/gtm"):]
        self.calls.append({"method": method, "path": path, "query": urllib.parse.parse_qs(parsed.query),
                           "headers": headers, "body": body})
        queue = self.routes.get((method, path))
        assert queue, f"unexpected request {method} {path}"
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def bodies(self, path):
        return [json.loads(c["body"]) for c in self.calls if c["path"] == path]


@pytest.fixture
def transport():
    t = FakeTransport()
    t.add("POST", "/oauth/v1/token", FIX["token"])
    return t


def make_client(transport, tmp_path=None, **kw):
    log = ApiCallLog(tmp_path / "api_calls.csv") if tmp_path else None
    return ZoomInfoClient(log, max_requests_per_second=1000, client_id="cid", client_secret="secret",
                          transport=transport, sleep=lambda s: transport.calls.append({"slept": s, "path": ""}),
                          **kw)


def test_requires_credentials(monkeypatch):
    monkeypatch.delenv("ZOOMINFO_CLIENT_ID", raising=False)
    monkeypatch.delenv("ZOOMINFO_CLIENT_SECRET", raising=False)
    with pytest.raises(ZoomInfoConfigError):
        ZoomInfoClient()


def test_auth_is_client_credentials_basic(transport):
    transport.add("GET", "/data/v1/users/usage", FIX["usage"])
    make_client(transport).get_usage()
    tok = transport.calls[0]
    assert tok["path"] == "/oauth/v1/token" and tok["method"] == "POST"
    assert tok["headers"]["Authorization"] == "Basic " + base64.b64encode(b"cid:secret").decode()
    assert tok["body"] == b"grant_type=client_credentials"
    assert transport.calls[1]["headers"]["Authorization"] == "Bearer test-token-abc"


def test_token_reused_until_expiry(transport):
    transport.add("GET", "/data/v1/users/usage", FIX["usage"])
    c = make_client(transport)
    c.get_usage()
    c.get_usage()
    assert sum(1 for x in transport.calls if x["path"] == "/oauth/v1/token") == 1


def test_contact_search_pages_and_maps(transport, tmp_path):
    transport.add("POST", "/data/v1/contacts/search", FIX["contact_search_page1"])
    transport.add("POST", "/data/v1/contacts/search", FIX["contact_search_page2"])
    c = make_client(transport, tmp_path, page_size=2)
    filters = {"job_titles": ["CEO", "VP Sales"], "countries": ["United States"],
               "employee_min": 50, "employee_max": 2000}
    contacts = c.search_contacts(filters, max_results=10)
    # constant page size; page numbers 1 then 2; stops when links.next is null
    searches = [x for x in transport.calls if x["path"] == "/data/v1/contacts/search"]
    assert [s["query"]["page[number]"] for s in searches] == [["1"], ["2"]]
    assert all(s["query"]["page[size]"] == ["2"] for s in searches)
    body = transport.bodies("/data/v1/contacts/search")[0]
    assert body["data"]["type"] == "ContactSearch"
    assert body["data"]["attributes"] == {"jobTitle": "CEO,VP Sales", "country": "United States",
                                          "employeeRangeMin": "50", "employeeRangeMax": "2000"}
    assert [x.contact_id for x in contacts] == ["9001001", "9002002", "9010002"]
    m = contacts[0]
    assert (m.full_name, m.company_id, m.company_name, m.accuracy_score) == \
        ("Maya Patel", "1001", "Northwind Analytics", 95)
    assert m.has_email and m.has_direct_phone and not m.has_mobile_phone
    assert m.filter_matched == ("country", "size")
    rows = ApiCallLog(tmp_path / "api_calls.csv").read()
    assert [(r["endpoint"], r["estimated_credits"], r["mode"]) for r in rows] == \
        [("POST /data/v1/contacts/search", "0", "live")] * 2


def test_search_filter_values_score_without_company_data(transport):
    transport.add("POST", "/data/v1/contacts/search", FIX["contact_search_page2"])
    [h] = make_client(transport).search_contacts({"countries": ["United States"], "employee_min": 50}, 10)
    s = score_contact(h, 0, 5, {"title": 35, "email_hint": 20, "phone_hint": 10, "accuracy": 15,
                                "size_in_range": 10, "country_allowed": 10}, (50, 2000), ["United States"])
    assert "size 10/10 (search filter)" in s.breakdown and "country 10/10 (search filter)" in s.breakdown


def test_config_attributes_override_mapping(transport):
    transport.add("POST", "/data/v1/contacts/search", FIX["contact_search_page2"])
    c = make_client(transport, contact_search_attributes={"jobTitle": "CEO OR Founder", "managementLevel": "C-Level"})
    c.search_contacts({"job_titles": ["CEO"]}, 10)
    attrs = transport.bodies("/data/v1/contacts/search")[0]["data"]["attributes"]
    assert attrs == {"jobTitle": "CEO OR Founder", "managementLevel": "C-Level"}


def test_enrich_requires_permit_and_maps(transport, tmp_path):
    transport.add("POST", "/data/v1/contacts/enrich", FIX["enrich"])
    c = make_client(transport, tmp_path)
    g = CreditGuard(max_enrich_per_run=5, max_enrich_per_day=5, reserve=0, live=True,
                    enrich_enabled=True, remaining_credits=93)
    permit = g.issue_permit(["9001001", "9999999"])
    out = c.enrich_contacts(permit)
    body = transport.bodies("/data/v1/contacts/enrich")[0]
    assert body["data"]["type"] == "ContactEnrich"
    assert body["data"]["attributes"]["matchPersonInput"] == [{"personId": 9001001}, {"personId": 9999999}]
    assert "email" in body["data"]["attributes"]["outputFields"]
    assert len(out) == 1                                   # NoMatch is not returned or charged
    e = out[0]
    assert (e.contact_id, e.company_id, e.email, e.industry) == \
        ("9001001", "1001", "maya.patel@northwind-analytics.example", "Software")
    assert e.poc_linkedin_url == "https://www.linkedin.com/in/maya-patel-mock"
    assert e.company_linkedin_url == "https://www.linkedin.com/company/northwind-mock"
    assert (e.direct_phone, e.company_phone, e.employees) == ("+1 555 0199 001", "+1 555 0101 000", "250")
    row = ApiCallLog(tmp_path / "api_calls.csv").read()[-1]
    assert (row["endpoint"], row["record_count"], row["estimated_credits"]) == \
        ("POST /data/v1/contacts/enrich", "1", "1")


def test_enrich_batches_of_25(transport):
    transport.add("POST", "/data/v1/contacts/enrich", {"data": []})
    g = CreditGuard(max_enrich_per_run=30, max_enrich_per_day=30, reserve=0, live=True,
                    enrich_enabled=True, remaining_credits=100)
    make_client(transport).enrich_contacts(g.issue_permit([str(i) for i in range(1, 31)]))
    sizes = [len(b["data"]["attributes"]["matchPersonInput"]) for b in transport.bodies("/data/v1/contacts/enrich")]
    assert sizes == [25, 5]


def test_usage_reads_record_bucket(transport):
    transport.add("GET", "/data/v1/users/usage", FIX["usage"])
    u = make_client(transport).get_usage()
    assert (u.remaining_credits, u.limit, u.used) == (93, 100, 7)


def test_usage_unknown_bucket_blocks_guard(transport):
    transport.add("GET", "/data/v1/users/usage", FIX["usage"])
    u = make_client(transport, usage_limit_type="bulk").get_usage()
    assert u.remaining_credits is None
    g = CreditGuard(max_enrich_per_run=1, max_enrich_per_day=1, reserve=0, live=True,
                    enrich_enabled=True, remaining_credits=u.remaining_credits)
    assert g.budget()[0] == 0


def test_429_retry_after_then_success(transport):
    transport.add("GET", "/data/v1/users/usage", {}, status=429,
                  headers={"Retry-After": "2", "X-RateLimit-Rejected-Bucket": "second"})
    transport.add("GET", "/data/v1/users/usage", FIX["usage"])
    u = make_client(transport).get_usage()
    assert u.remaining_credits == 93
    assert any(x.get("slept") == 2.0 for x in transport.calls)


def test_429_daily_bucket_stops(transport):
    transport.add("GET", "/data/v1/users/usage", {}, status=429,
                  headers={"Retry-After": "3600", "X-RateLimit-Rejected-Bucket": "day"})
    with pytest.raises(ZoomInfoApiError, match="day rate limit"):
        make_client(transport).get_usage()


def test_401_refreshes_token_once(transport):
    transport.add("GET", "/data/v1/users/usage", {}, status=401)
    transport.add("GET", "/data/v1/users/usage", FIX["usage"])
    assert make_client(transport).get_usage().remaining_credits == 93
    assert sum(1 for x in transport.calls if x["path"] == "/oauth/v1/token") == 2


def test_400_error_has_detail_but_no_secret(transport):
    transport.add("POST", "/data/v1/contacts/search",
                  {"errors": [{"status": "400", "detail": "Invalid field jobTitle"}]}, status=400)
    with pytest.raises(ZoomInfoApiError) as ei:
        make_client(transport).search_contacts({}, 10)
    assert "Invalid field jobTitle" in str(ei.value) and "secret" not in str(ei.value)


def test_company_search_mapping(transport):
    transport.add("POST", "/data/v1/companies/search", FIX["company_search"])
    out = make_client(transport).search_companies(
        {"employee_growth_1y_min_pct": 20, "funding_date_from": "2025-11-16",
         "funding_amount_min": 1_000_000, "funding_amount_max": 100_000_000,
         "countries": ["United States"], "employee_min": 50}, 50)
    attrs = transport.bodies("/data/v1/companies/search")[0]["data"]["attributes"]
    assert attrs == {"oneYearEmployeeGrowthRateMin": "20", "fundingStartDate": "2025-11-16",
                     "fundingAmountMin": "1000", "fundingAmountMax": "100000",   # thousands
                     "country": "United States", "employeeRangeMin": "50"}
    assert (out[0].company_id, out[0].name, out[0].employee_count) == ("2001", "Quillstone Robotics", 140)


def test_mapping_ignores_empty_values():
    assert map_contact_filters({"industries": [], "job_titles": None}) == ({}, ())
    assert map_company_filters({"industries": []}) == {}


def test_first_live_run_capped_at_one():
    g = CreditGuard(max_enrich_per_run=5, max_enrich_per_day=10, reserve=0, live=True,
                    enrich_enabled=True, remaining_credits=100, first_live_run=True)
    assert g.budget()[0] == 1
    assert g.issue_permit(["1", "2", "3"]).contact_ids == ("1",)


def test_call_log_detects_first_live_run(tmp_path):
    log = ApiCallLog(tmp_path / "c.csv")
    log.log("mock:enrich_contacts", 1, 1, "mock")
    log.log("POST /data/v1/contacts/search", 5, 0, "live")
    assert not log.has_live_enrich()
    log.log("POST /data/v1/contacts/enrich", 1, 1, "live")
    assert log.has_live_enrich()
