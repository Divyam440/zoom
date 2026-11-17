import pytest

from zi_agent.client import MockClient
from zi_agent.guard import CreditGuard, CreditGuardError, EnrichPermit


def guard(**kw):
    base = dict(max_enrich_per_run=5, max_enrich_per_day=10, reserve=20, used_today=0,
                live=True, enrich_enabled=True, remaining_credits=100)
    base.update(kw)
    return CreditGuard(**base)


def test_dry_run_blocks():
    g = guard(live=False)
    assert g.budget() == (0, "dry-run (no --live flag)")
    with pytest.raises(CreditGuardError):
        g.issue_permit(["1"])


def test_enrich_disabled_blocks():
    g = guard(enrich_enabled=False)
    assert g.budget()[0] == 0
    assert "enrich_enabled" in g.budget()[1]


def test_unknown_remaining_blocks():
    g = guard(remaining_credits=None)
    assert g.budget()[0] == 0


def test_per_run_cap_truncates():
    g = guard(max_enrich_per_run=2)
    p = g.issue_permit(["1", "2", "3", "4"])
    assert p.contact_ids == ("1", "2")
    with pytest.raises(CreditGuardError):
        g.issue_permit(["5"])  # reservation counts until settled


def test_per_day_cap_uses_used_today():
    g = guard(max_enrich_per_day=10, used_today=9)
    assert g.budget() == (1, "limited by max_enrich_per_day")


def test_reserve_stops_spending():
    g = guard(remaining_credits=22, reserve=20)
    assert g.budget()[0] == 2
    g2 = guard(remaining_credits=20, reserve=20)
    assert g2.budget() == (0, "credit reserve reached")
    g3 = guard(remaining_credits=5, reserve=20)
    assert g3.budget()[0] == 0


def test_settle_releases_unused_reservation():
    g = guard(max_enrich_per_run=3)
    p = g.issue_permit(["1", "2", "3"])
    assert g.budget()[0] == 0
    p.redeem()
    g.settle(p, credits_used=1)  # two were free (already enriched this year)
    assert g.used_this_run == 1
    assert g.budget()[0] == 2


def test_permit_cannot_be_forged_or_reused():
    g = guard()
    with pytest.raises(CreditGuardError):
        EnrichPermit(g, ("1",))
    p = g.issue_permit(["1"])
    p.redeem()
    with pytest.raises(CreditGuardError):
        p.redeem()


def test_permit_from_other_guard_rejected():
    g1, g2 = guard(), guard()
    p = g1.issue_permit(["1"])
    with pytest.raises(CreditGuardError):
        g2._redeem(p)


def test_duplicate_ids_counted_once():
    g = guard(max_enrich_per_run=5)
    p = g.issue_permit(["1", "1", " 1 ", "2"])
    assert p.contact_ids == ("1", "2")


def test_client_enrich_requires_permit():
    client = MockClient()
    with pytest.raises(AttributeError):
        client.enrich_contacts(["9001001"])  # a plain list is not a permit
    g = guard()
    p = g.issue_permit(["9001001"])
    out = client.enrich_contacts(p)
    assert [e.contact_id for e in out] == ["9001001"]
    with pytest.raises(CreditGuardError):
        client.enrich_contacts(p)  # single use
