from conftest import make_contact

from zi_agent.poc import pick_pocs, title_rank
from zi_agent.scoring import score_contact

PRIORITY = [["CEO", "Chief Executive Officer"], ["Founder", "Co-Founder"],
            ["VP Sales", "Vice President Sales"], ["Head Sales", "Director Sales"]]
EXCLUDE = ["assistant", "former"]
WEIGHTS = {"title": 35, "email_hint": 20, "phone_hint": 10, "accuracy": 15,
           "size_in_range": 10, "country_allowed": 10}


def test_title_rank_matching():
    assert title_rank("Chief Executive Officer", PRIORITY, EXCLUDE)[0] == 0
    assert title_rank("Founder and CEO", PRIORITY, EXCLUDE)[0] == 0
    assert title_rank("Co-Founder", PRIORITY, EXCLUDE)[0] == 1
    assert title_rank("Vice President, Sales", PRIORITY, EXCLUDE)[0] == 2
    assert title_rank("VP of Sales", PRIORITY, EXCLUDE)[0] == 2
    assert title_rank("Director of Sales", PRIORITY, EXCLUDE)[0] == 3
    assert title_rank("Executive Assistant to the CEO", PRIORITY, EXCLUDE) is None
    assert title_rank("Software Engineer", PRIORITY, EXCLUDE) is None
    assert title_rank("CEOs Office Manager", PRIORITY, EXCLUDE) is None  # whole words only


def test_pick_highest_title_per_company():
    contacts = [make_contact(cid="1", title="VP Sales", accuracy_score=99),
                make_contact(cid="2", title="CEO", accuracy_score=50),
                make_contact(cid="3", title="Founder", company_id="c2", company="Beta")]
    picks, skipped = pick_pocs(contacts, PRIORITY, EXCLUDE)
    assert {p.contact.contact_id for p in picks} == {"2", "3"}
    assert skipped == []


def test_tie_break_by_accuracy():
    contacts = [make_contact(cid="1", title="VP Sales", accuracy_score=70),
                make_contact(cid="2", title="Vice President of Sales", accuracy_score=91)]
    picks, _ = pick_pocs(contacts, PRIORITY, EXCLUDE)
    assert picks[0].contact.contact_id == "2"


def test_company_without_allowed_title_skipped():
    contacts = [make_contact(cid="1", title="Accountant"),
                make_contact(cid="2", title="Executive Assistant to the CEO")]
    picks, skipped = pick_pocs(contacts, PRIORITY, EXCLUDE)
    assert picks == [] and skipped == ["c1"]


def score(contact, rank=0, **kw):
    return score_contact(contact, rank, len(PRIORITY), WEIGHTS, (50, 2000),
                         ["United States"], **kw)


def test_perfect_contact_scores_100():
    s = score(make_contact(accuracy_score=100))
    assert s.score == 100
    assert s.breakdown.startswith("title 35/35 (#1) | email 20/20 | phone 10/10")


def test_score_components():
    c = make_contact(has_email=False, has_direct_phone=False, has_company_phone=True,
                     accuracy_score=60, employee_count=10, country="Germany")
    s = score(c, rank=2)
    # title: 35*(4-2)/4=17.5->18, email 0, phone 5, acc 9, size 0, country 0
    assert s.parts == {"title": 18, "email_hint": 0, "phone_hint": 5, "accuracy": 9,
                       "size_in_range": 0, "country_allowed": 0}
    assert s.score == 32
    assert "phone 5/10 (HQ only)" in s.breakdown
    assert "acc 9/15 (60)" in s.breakdown


def test_weights_scale_to_100():
    w = {k: v * 3 for k, v in WEIGHTS.items()}
    s = score_contact(make_contact(accuracy_score=100), 0, len(PRIORITY), w, (50, 2000), ["United States"])
    assert s.score == 100


def test_new_company_boost_capped():
    s = score(make_contact(accuracy_score=100), new_company_boost=10, is_new_company=True)
    assert s.score == 100
    s2 = score(make_contact(has_email=False), new_company_boost=10, is_new_company=True)
    assert s2.score == score(make_contact(has_email=False)).score + 10
    assert "new co +10" in s2.breakdown


def test_unknown_size_and_accuracy():
    s = score(make_contact(employee_count=None, accuracy_score=None))
    assert s.parts["size_in_range"] == 0 and s.parts["accuracy"] == 0
    assert "(unknown)" in s.breakdown and "(n/a)" in s.breakdown
