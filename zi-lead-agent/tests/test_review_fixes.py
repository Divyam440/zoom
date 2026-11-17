"""Regression tests for the issues found in the independent review."""
import datetime as dt

import pytest
from openpyxl import Workbook, load_workbook

from conftest import make_contact

from zi_agent import cli
from zi_agent.blocklist import Blocklist
from zi_agent.calllog import ApiCallLog
from zi_agent.client import MockClient, ZoomInfoApiError, ZoomInfoClient, _sanitize, urllib_transport
from zi_agent.daily import build_master_index
from zi_agent.dedupe import SeenIndex, dedupe_contacts
from zi_agent.enrich import run_enrichment
from zi_agent.guard import CreditGuard
from zi_agent.models import QUEUE_TAB, Contact
from zi_agent.queue import QueueCandidate, merge_headers, merge_queue
from zi_agent.storage import StorageLocked

TODAY = dt.date(2026, 11, 16)
QUEUE = [{"Rank": 1, "Contact ID": "9001001", "Status": "New", "Company": "Northwind Analytics"}]


def live_guard(**kw):
    base = dict(max_enrich_per_run=5, max_enrich_per_day=5, reserve=0, live=True,
                enrich_enabled=True, remaining_credits=100)
    base.update(kw)
    return CreditGuard(**base)


# -- 1. master open in Excel ---------------------------------------------------
def test_locked_master_blocks_before_spending(storage, conventions, monkeypatch, tmp_path):
    storage.ensure_master()
    monkeypatch.setattr(storage, "master_writable", lambda: False)
    log = ApiCallLog(tmp_path / "calls.csv")
    g = live_guard()
    er = run_enrichment(client=MockClient(call_log=log), guard=g, storage=storage,
                        conventions=conventions, queue_rows=[dict(QUEUE[0])], top_n=1,
                        master=SeenIndex("master"), today=TODAY)
    assert "open in Excel" in er.status and er.credits_used == 0
    assert log.read() == []                      # no enrich call at all


def test_locked_on_save_keeps_rows_and_flushes_later(storage, conventions, monkeypatch):
    storage.ensure_master()
    real_append = storage.append_master

    def locked(rows):
        raise StorageLocked("open")
    monkeypatch.setattr(storage, "append_master", locked)
    queue = [dict(QUEUE[0])]
    er = run_enrichment(client=MockClient(), guard=live_guard(), storage=storage,
                        conventions=conventions, queue_rows=queue, top_n=1,
                        master=SeenIndex("master"), today=TODAY)
    assert "saved to enriched_pending.csv" in er.status
    assert queue[0]["Status"] == "Done"
    # the saved-aside row already counts for dedupe
    assert build_master_index(storage).match("9001001")
    monkeypatch.setattr(storage, "append_master", real_append)
    assert storage.flush_enrich_pending() == 1
    assert storage.read_master()[0]["ZoomInfo Contact ID"] == "9001001"
    assert not storage.enrich_pending_path.exists()


# -- 2. network errors ----------------------------------------------------------
def test_network_error_becomes_api_error():
    with pytest.raises(ZoomInfoApiError, match="network error"):
        urllib_transport("GET", "http://127.0.0.1:9/nothing", {}, None, timeout=1)


def test_failed_enrich_logs_worst_case_credits(tmp_path):
    def transport(method, url, headers, body):
        if url.endswith("/oauth/v1/token"):
            from zi_agent.client import HttpResponse
            return HttpResponse(200, {}, b'{"access_token":"t","expires_in":1000}')
        raise ZoomInfoApiError("network error: TimeoutError")
    log = ApiCallLog(tmp_path / "calls.csv")
    c = ZoomInfoClient(log, 1000, client_id="a", client_secret="b", transport=transport)
    with pytest.raises(ZoomInfoApiError):
        c.enrich_contacts(live_guard().issue_permit(["1", "2"]))
    assert log.credits_on(dt.date.today()) == 2  # daily cap stays honest
    assert not log.has_live_enrich()             # first-run cap of 1 still applies


# -- 3. company-name variants ---------------------------------------------------
def test_company_and_name_variants_are_duplicates():
    master = SeenIndex.from_master_rows([{"POC": "Jane  Doe", "Company Name": "Acme, Inc."}])
    c = make_contact(cid="5", first="Jane", last="Doe", company="ACME Inc")
    assert dedupe_contacts([c], master).kept == []
    assert master.has_company("", "Acme")
    assert master.match(poc="jane doe", company="Acme LLC")


# -- 4/5. queue rows closed when company becomes off-limits -----------------------
def test_new_rows_skipped_for_blocklisted_or_handled_company():
    rows, _ = merge_queue([], [QueueCandidate(make_contact(cid="1", company="Acme", company_id="c1"), 50, "", "r"),
                               QueueCandidate(make_contact(cid="2", company="Beta", company_id="c2"), 60, "", "r"),
                               QueueCandidate(make_contact(cid="3", company="Gamma", company_id="c3"), 70, "", "r")],
                          SeenIndex("master"), TODAY)
    next(r for r in rows if r["Contact ID"] == "3")["Status"] = "Claimed"
    master = SeenIndex.from_master_rows([{"POC": "Someone Else", "Company Name": "Beta Ltd"},
                                         {"POC": "Other", "Company Name": "Gamma"}])
    rows2, stats = merge_queue(rows, [], master, TODAY, blocklist=Blocklist(["Acme"]),
                               skip_companies_in_master=True)
    by = {r["Contact ID"]: r for r in rows2}
    assert by["1"]["Status"] == "Skipped" and "blocklisted" in by["1"]["Reason"]
    assert by["2"]["Status"] == "Skipped" and "already in the master" in by["2"]["Reason"]
    assert by["3"]["Status"] == "Claimed"       # a human claimed it: untouched
    assert stats.skipped == 2


# -- 6. ID columns placed after the capture tool's columns ------------------------
def test_legacy_master_gets_ids_after_capture_columns(storage, conventions):
    wb = Workbook()
    ws = wb.active
    ws.append(list(conventions.headers[:11]))   # only the original 11 headers
    ws.append(["Acme"] + [""] * 16)             # capture tool wrote 17 values
    wb.save(storage.master_path)
    assert storage.ensure_master() == "upgraded"
    header = [c.value for c in load_workbook(storage.master_path).active[1]]
    assert header == list(conventions.headers) + ["ZoomInfo Contact ID", "ZoomInfo Company ID"]


# -- 7. dry-run never rewrites an existing master ----------------------------------
def test_dry_run_does_not_touch_existing_master(storage, conventions):
    wb = Workbook()
    wb.active.append(list(conventions.headers))
    wb.save(storage.master_path)
    before = storage.master_path.stat().st_mtime_ns
    assert storage.ensure_master(upgrade=False) == "needs-upgrade"
    assert storage.master_path.stat().st_mtime_ns == before


# -- 8. user formulas in the queue survive a re-run --------------------------------
def test_queue_formulas_preserved(storage):
    rows, _ = merge_queue([], [QueueCandidate(make_contact(cid="1"), 50, "", "r")], SeenIndex("m"), TODAY)
    rows[0]["Link"] = '=HYPERLINK("https://example.com","open")'
    storage.write_table(QUEUE_TAB, merge_headers(["Link"]), rows)
    headers, back = storage.read_table(QUEUE_TAB)
    rows2, _ = merge_queue(back, [], SeenIndex("m"), TODAY)
    storage.write_table(QUEUE_TAB, merge_headers(headers), rows2)
    cell = load_workbook(storage.workbook_path)[QUEUE_TAB]["M2"]
    assert cell.value.startswith("=HYPERLINK") and cell.data_type == "f"


# -- 9. NoMatch first run keeps the cap of 1 --------------------------------------
def test_nomatch_does_not_end_first_run_cap(tmp_path):
    log = ApiCallLog(tmp_path / "c.csv")
    log.log("POST /data/v1/contacts/enrich", 0, 0, "live")
    assert not log.has_live_enrich()


# -- 10. usage values that are not integers --------------------------------------
def test_usage_string_values(tmp_path):
    from zi_agent.client import HttpResponse

    def transport(method, url, headers, body):
        if "token" in url:
            return HttpResponse(200, {}, b'{"access_token":"t","expires_in":1000}')
        return HttpResponse(200, {}, b'{"data":[{"attributes":{"usage":[{"limitType":"record",'
                                     b'"totalLimit":"100","currentUsage":"13","usageRemaining":"87"}]}}]}')
    u = ZoomInfoClient(None, 1000, client_id="a", client_secret="b", transport=transport).get_usage()
    assert (u.remaining_credits, u.limit, u.used) == (87, 100, 13)


# -- 11. formula injection from API text ------------------------------------------
def test_api_text_cannot_become_formula(storage):
    c = _sanitize(Contact("1", "=cmd|' /C calc'!A0", "X", "CEO", "c1", "=HYPERLINK(1)"))
    assert c.first_name.startswith("'=") and c.company_name.startswith("'=")
    rows, _ = merge_queue([], [QueueCandidate(c, 50, "", "r")], SeenIndex("m"), TODAY)
    storage.write_table(QUEUE_TAB, merge_headers([]), rows)
    ws = load_workbook(storage.workbook_path)[QUEUE_TAB]
    assert all(cell.data_type != "f" for cell in ws[2])


# -- CLI smoke test ---------------------------------------------------------------
def test_cli_dry_run_end_to_end(cfg, capsys):
    assert cli.main(["--config", str(cfg.source), "run-daily"]) == 0
    out = capsys.readouterr().out
    assert "Added to queue           : 7" in out and "Queue covers" in out
    assert cli.main(["--config", str(cfg.source), "radar-weekly"]) == 0
    assert cli.main(["--config", str(cfg.source), "report-weekly"]) == 0
    assert cli.main(["--config", str(cfg.source), "check-config"]) == 0
