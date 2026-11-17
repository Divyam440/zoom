import datetime as dt

from conftest import make_contact

from zi_agent.dedupe import SeenIndex
from zi_agent.models import QUEUE_TAB
from zi_agent.queue import QueueCandidate, merge_headers, merge_queue

TODAY = dt.date(2026, 11, 16)


def cand(cid, score, company_id=None, **kw):
    c = make_contact(cid=cid, company_id=company_id or f"c{cid}", company=f"Co {cid}",
                     first=f"P{cid}", last="X", **kw)
    return QueueCandidate(c, score, f"breakdown {cid}", "reason")


def test_new_rows_added_sorted_and_ranked():
    rows, stats = merge_queue([], [cand("1", 40), cand("2", 90), cand("3", 60)],
                              SeenIndex("master"), TODAY)
    assert [r["Contact ID"] for r in rows] == ["2", "3", "1"]
    assert [r["Rank"] for r in rows] == [1, 2, 3]
    assert all(r["Status"] == "New" and r["Added On"] == "2026-11-16" for r in rows)
    assert stats.added == 3


def test_manual_edits_preserved_on_rerun():
    first, _ = merge_queue([], [cand("1", 50), cand("2", 80)], SeenIndex("master"), TODAY)
    # user edits
    for r in first:
        if r["Contact ID"] == "1":
            r.update({"Claimed By": "Sisir", "Status": "Claimed", "Reason": "my note", "My Col": "x"})
        if r["Contact ID"] == "2":
            r["Status"] = "Skipped"
    later = TODAY + dt.timedelta(days=1)
    rows, stats = merge_queue(first, [cand("1", 95), cand("3", 70)], SeenIndex("master"), later)
    by = {r["Contact ID"]: r for r in rows}
    assert by["1"]["Claimed By"] == "Sisir"
    assert by["1"]["Status"] == "Claimed"
    assert by["1"]["Reason"] == "my note"
    assert by["1"]["My Col"] == "x"
    assert by["1"]["Added On"] == "2026-11-16"
    assert by["1"]["Fit Score"] == 95          # computed column refreshed
    assert by["2"]["Status"] == "Skipped"      # not reopened
    assert by["2"]["Rank"] == ""               # closed rows unranked, at bottom
    assert rows[-1]["Contact ID"] == "2"
    assert stats.added == 1


def test_queue_contact_now_in_master_marked_done():
    first, _ = merge_queue([], [cand("1", 50), cand("2", 60)], SeenIndex("master"), TODAY)
    next(r for r in first if r["Contact ID"] == "1")["Claimed By"] = "Sisir"
    master = SeenIndex.from_master_rows([{"POC": "P1 X", "Company Name": "Co 1"}])  # captured, no ID
    rows, stats = merge_queue(first, [], master, TODAY)
    by = {r["Contact ID"]: r for r in rows}
    assert by["1"]["Status"] == "Done"
    assert by["1"]["Claimed By"] == "Sisir"
    assert by["2"]["Status"] == "New"
    assert stats.marked_done == 1


def test_extra_user_columns_kept_in_headers():
    headers = merge_headers(["Rank", "Fit Score", "My Col", "Status"])
    assert headers[-1] == "My Col"
    assert headers[:3] == ["Rank", "Fit Score", "Score Breakdown"]


def test_roundtrip_through_excel_preserves_edits(storage):
    rows, _ = merge_queue([], [cand("9001001", 50), cand("2", 80)], SeenIndex("master"), TODAY)
    storage.write_table(QUEUE_TAB, merge_headers([]), rows)
    headers, back = storage.read_table(QUEUE_TAB)
    for r in back:
        if str(r["Contact ID"]) == "9001001":
            r["Claimed By"], r["Status"] = "Sisir", "Claimed"
    storage.write_table(QUEUE_TAB, merge_headers(headers), back)
    headers, back = storage.read_table(QUEUE_TAB)
    rows2, _ = merge_queue(back, [cand("9001001", 99)], SeenIndex("master"), TODAY)
    r = next(x for x in rows2 if x["Contact ID"] == "9001001")
    assert (r["Claimed By"], r["Status"], r["Fit Score"]) == ("Sisir", "Claimed", 99)
