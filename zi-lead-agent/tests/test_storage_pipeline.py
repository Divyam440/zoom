import datetime as dt

from openpyxl import Workbook, load_workbook

from conftest import CAPTURE_TOOL

from zi_agent.client import MockClient
from zi_agent.daily import run_daily, save_queue
from zi_agent.dedupe import SeenIndex
from zi_agent.enrich import DOMAIN_MISMATCH_NOTE, email_domain_mismatch, run_enrichment
from zi_agent.guard import CreditGuard
from zi_agent.models import (FALLBACK_CAPTURE_HEADERS, FALLBACK_IMPORTANT_FIELDS, QUEUE_TAB,
                             load_capture_conventions)

TODAY = dt.date(2026, 11, 16)


def test_capture_headers_read_from_capture_tool(conventions):
    assert conventions.source == str(CAPTURE_TOOL)
    assert conventions.headers == FALLBACK_CAPTURE_HEADERS  # fallback still in sync
    assert conventions.important_fields == FALLBACK_IMPORTANT_FIELDS
    mh = conventions.master_headers
    assert mh[:11] == ("Company Name", "Employees", "Industry", "Website", "Company Linkedin Url",
                       "POC", "Job title", "Email ID", "Company Country", "Company Phone", "Date")
    assert mh[-2:] == ("ZoomInfo Contact ID", "ZoomInfo Company ID")


def test_missing_capture_tool_uses_fallback(tmp_path):
    assert load_capture_conventions(tmp_path / "nope.py").source == "fallback"


def test_ensure_master_creates_file(storage):
    assert storage.ensure_master() == "created"
    ws = load_workbook(storage.master_path).active
    assert ws.title == "Leads"
    assert [c.value for c in ws[1]] == list(storage.master_headers)
    assert storage.ensure_master() == "ok"


def test_ensure_master_appends_id_columns_without_reordering(storage, conventions):
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(list(conventions.headers))
    ws.append(["Acme"] + [""] * (len(conventions.headers) - 1))
    wb.save(storage.master_path)
    assert storage.ensure_master() == "upgraded"
    ws = load_workbook(storage.master_path).active
    header = [c.value for c in ws[1]]
    assert header == list(conventions.headers) + ["ZoomInfo Contact ID", "ZoomInfo Company ID"]
    assert ws["A2"].value == "Acme"


def test_agent_tab_does_not_change_master_active_sheet(storage, conventions):
    # Even if someone points the agent workbook at the master file.
    storage.ensure_master()
    storage.workbook_path = storage.master_path
    storage.write_table(QUEUE_TAB, ["Rank"], [{"Rank": 1}])
    wb = load_workbook(storage.master_path)
    assert wb.active.title == "Leads"
    assert wb.sheetnames[0] == "Leads"


def test_daily_dry_run_on_fixtures(cfg, storage):
    storage.ensure_master()
    res = run_daily(cfg, MockClient(), storage, TODAY)
    assert res.searched == 30
    assert len(res.blocklisted) == 4                   # Vantage (name) + Orbital (domain)
    assert len(res.duplicates) == 2                    # repeated ID + name/company dup
    assert len(res.companies_no_title) == 1            # Pinecrest
    ids = {r["Contact ID"]: r for r in res.queue_rows}
    assert set(ids) == {"9001001", "9002002", "9003002", "9006001", "9007004", "9008001", "9010002"}
    assert "9003001" not in ids                        # Amelia (93) beats Oliver (79) on tie
    assert "9007002" not in ids                        # assistant excluded
    assert res.queue_rows[0]["Contact ID"] == "9001001"
    save_queue(storage, res)

    # Re-run: claim one row, capture another into the master by hand.
    _, rows = storage.read_table(QUEUE_TAB)
    for r in rows:
        if r["Contact ID"] == "9002002":
            r["Claimed By"], r["Status"] = "Sisir", "Claimed"
    storage.write_table(QUEUE_TAB, list(rows[0].keys()), rows)
    storage.append_master([{"POC": "Maya Patel", "Company Name": "Northwind Analytics"}])

    res2 = run_daily(cfg, MockClient(), storage, TODAY)
    by = {r["Contact ID"]: r for r in res2.queue_rows}
    assert res2.added == 0
    assert by["9002002"]["Status"] == "Claimed" and by["9002002"]["Claimed By"] == "Sisir"
    assert by["9001001"]["Status"] == "Done"
    assert res2.marked_done == 1


def test_enrichment_through_guard_appends_master(cfg, storage, conventions):
    storage.ensure_master()
    client = MockClient()
    res = run_daily(cfg, client, storage, TODAY)
    master = SeenIndex.from_master_rows(storage.read_master())
    g = CreditGuard(max_enrich_per_run=1, max_enrich_per_day=5, reserve=20, live=True,
                    enrich_enabled=True, remaining_credits=client.get_usage().remaining_credits)
    er = run_enrichment(client=client, guard=g, storage=storage, conventions=conventions,
                        queue_rows=res.queue_rows, top_n=3, master=master, today=TODAY)
    assert er.appended_ids == ["9001001"]              # capped at 1 per run
    assert er.credits_used == 1
    rows = storage.read_master()
    assert len(rows) == 1
    row = rows[0]
    assert row["ZoomInfo Contact ID"] == "9001001"
    assert row["Email ID"] == "maya.patel@northwind-analytics.example"
    assert row["Date"] == "16-11-2026"
    assert next(r for r in res.queue_rows if r["Contact ID"] == "9001001")["Status"] == "Done"


def test_enrichment_flags_missing_and_domain_mismatch(cfg, storage, conventions):
    storage.ensure_master()
    client = MockClient()
    queue = [{"Rank": 1, "Contact ID": "9010002", "Status": "New"},
             {"Rank": 2, "Contact ID": "9003002", "Status": "New"}]
    g = CreditGuard(max_enrich_per_run=5, max_enrich_per_day=5, reserve=0, live=True,
                    enrich_enabled=True, remaining_credits=100)
    er = run_enrichment(client=client, guard=g, storage=storage, conventions=conventions,
                        queue_rows=queue, top_n=5, master=SeenIndex("master"), today=TODAY)
    assert er.mismatch_ids == ["9010002"]
    rows = {r["ZoomInfo Contact ID"]: r for r in storage.read_master()}
    assert DOMAIN_MISMATCH_NOTE in rows["9010002"]["Notes"]
    assert "Email ID" in rows["9003002"]["Missing Fields"]


def test_enrichment_blocked_in_dry_run(cfg, storage, conventions):
    g = CreditGuard(max_enrich_per_run=5, max_enrich_per_day=5, reserve=0, live=False,
                    enrich_enabled=True, remaining_credits=100)
    er = run_enrichment(client=MockClient(), guard=g, storage=storage, conventions=conventions,
                        queue_rows=[{"Rank": 1, "Contact ID": "9001001", "Status": "New"}],
                        top_n=1, master=SeenIndex("master"), today=TODAY)
    assert "dry-run" in er.status and er.appended_ids == []
    assert storage.read_master() == []


def test_email_domain_mismatch():
    assert not email_domain_mismatch("a@acme.example", "https://www.acme.example")
    assert not email_domain_mismatch("a@mail.acme.example", "acme.example")
    assert email_domain_mismatch("a@gmail.example", "acme.example")
    assert not email_domain_mismatch("", "acme.example")
