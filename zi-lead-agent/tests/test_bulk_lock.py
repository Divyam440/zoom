"""The project must never spend ZoomInfo bulk credits. These run with the real lock ON."""
import pytest

from zi_agent import guard
from zi_agent.client import ZoomInfoClient
from zi_agent.guard import CreditGuard, CreditGuardError, EnrichPermit


def _wide_open_guard():
    return CreditGuard(max_enrich_per_run=100, max_enrich_per_day=100, reserve=0,
                       live=True, enrich_enabled=True, remaining_credits=10_000)


def test_lock_is_on_by_default():
    assert guard.BULK_CREDITS_LOCKED is True


def test_guard_gives_no_budget_even_with_everything_enabled():
    room, why = _wide_open_guard().budget()
    assert room == 0 and "permanently disabled" in why


def test_guard_refuses_to_issue_permit():
    with pytest.raises(CreditGuardError):
        _wide_open_guard().issue_permit(["1", "2"])


def test_permit_cannot_be_forged():
    with pytest.raises(CreditGuardError):
        EnrichPermit(_wide_open_guard(), ("1",))


def test_live_client_never_sends_an_enrich_request():
    calls = []

    def transport(*a, **k):
        calls.append(a)
        raise AssertionError("no network call may happen")

    client = ZoomInfoClient(client_id="x", client_secret="y", transport=transport)
    permit = object.__new__(EnrichPermit)  # even a bypassed permit must not work
    with pytest.raises(CreditGuardError):
        client.enrich_contacts(permit)
    assert calls == []


def test_config_enrich_enabled_is_ignored(cfg):
    cfg.raw["enrich_enabled"] = True
    assert cfg.enrich_enabled is False
