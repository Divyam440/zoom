from conftest import make_contact

from zi_agent.blocklist import Blocklist, normalize_domain
from zi_agent.dedupe import SeenIndex, dedupe_contacts, name_company_key, norm_id


def test_norm_id_handles_excel_numbers():
    assert norm_id(9001001.0) == "9001001"
    assert norm_id(9001001) == "9001001"
    assert norm_id(None) == ""


def test_name_company_key_matches_capture_convention():
    # capture tool: (poc.strip().lower(), company.strip().lower())
    assert name_company_key("  Maya Patel ", "Northwind  Analytics") == ("maya patel", "northwind analytics")


def test_contact_id_match_first():
    master = SeenIndex.from_master_rows([{"ZoomInfo Contact ID": 111.0, "POC": "Other Name",
                                          "Company Name": "Other Co"}])
    res = dedupe_contacts([make_contact(cid="111")], master)
    assert res.kept == []
    assert "contact ID" in res.dropped[0][1]


def test_name_company_fallback_for_rows_without_id():
    # Rows captured by zoominfo_capture.py have no ZoomInfo IDs.
    master = SeenIndex.from_master_rows([{"POC": "ann lee", "Company Name": " ACME "}])
    res = dedupe_contacts([make_contact(cid="999", first="Ann", last="Lee", company="Acme")], master)
    assert res.kept == []
    assert "name + company" in res.dropped[0][1]


def test_queue_and_batch_duplicates():
    queue = SeenIndex.from_queue_rows([{"Contact ID": "2", "POC": "Bo Ng", "Company": "Beta"}])
    contacts = [
        make_contact(cid="1"),
        make_contact(cid="1"),                                  # same ID twice
        make_contact(cid="3", first="Ann", last="Lee"),         # same name+company as #1
        make_contact(cid="2", first="Bo", last="Ng", company="Beta"),  # in queue
        make_contact(cid="4", first="Cy", last="Doe"),
    ]
    res = dedupe_contacts(contacts, SeenIndex("master sheet"), queue)
    assert [c.contact_id for c in res.kept] == ["1", "4"]
    reasons = [r for _, r in res.dropped]
    assert reasons[0] == "contact ID already in this search batch"
    assert reasons[1] == "name + company already in this search batch"
    assert reasons[2] == "contact ID already in queue"


def test_same_name_different_company_is_not_duplicate():
    master = SeenIndex.from_master_rows([{"POC": "Ann Lee", "Company Name": "Other Co"}])
    res = dedupe_contacts([make_contact()], master)
    assert len(res.kept) == 1


def test_pending_rows_count_as_master():
    # pending_rows.csv rows have the same headers as the master
    idx = SeenIndex.from_master_rows([{"POC": "Ann Lee", "Company Name": "Acme", "Email ID": "a@acme.example"}])
    assert idx.match(email="A@acme.example ")


def test_blocklist_name_and_domain():
    bl = Blocklist(companies=["Vantage Retail Group Inc."], domains=["orbitalpay.example"])
    assert bl.reason("Vantage Retail Group", "") == "blocklisted company"
    assert bl.reason("Orbital", "https://www.orbitalpay.example/about") == "blocklisted domain"
    assert bl.reason("Orbital", "eu.orbitalpay.example") == "blocklisted domain"
    assert bl.reason("Orbital", "notorbitalpay.example") is None
    assert normalize_domain("Jane@Mail.Example.com") == "mail.example.com"
