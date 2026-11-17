"""CreditGuard: the ONLY place credit limits live.

Enrichment needs an EnrichPermit, and only CreditGuard can issue one. Every
client's enrich method redeems the permit before doing anything, so nothing
can call enrich without passing through this class.

A permit is issued only when ALL of these hold:
  - live mode (--live flag)
  - enrich_enabled: true in config
  - remaining credits are known (read from the usage endpoint)
  - max_enrich_per_run, max_enrich_per_day and the reserve all leave room

Credits are counted worst case: 1 credit per record. (Re-enriching a record
within 12 months is free, but that cannot be known before the call.)
"""
from __future__ import annotations

from typing import Iterable

_PERMIT_KEY = object()

# HARD LOCK: this project must never spend ZoomInfo bulk credits. Profiles are
# opened and captured by the user by hand (zoominfo_capture.py). While this is
# True, CreditGuard issues no permits and the live client refuses to enrich,
# whatever the config or CLI flags say. Only tests flip it (monkeypatch).
BULK_CREDITS_LOCKED = True
LOCK_REASON = "bulk credits are permanently disabled for this project (manual capture only)"


class CreditGuardError(RuntimeError):
    pass


class EnrichPermit:
    __slots__ = ("_guard", "_ids", "_redeemed", "_settled")

    def __init__(self, guard: "CreditGuard", contact_ids: tuple[str, ...], _key=None):
        if _key is not _PERMIT_KEY:
            raise CreditGuardError("Enrich permits can only be issued by CreditGuard.issue_permit().")
        self._guard = guard
        self._ids = contact_ids
        self._redeemed = False
        self._settled = False

    @property
    def contact_ids(self) -> tuple[str, ...]:
        return self._ids

    def redeem(self) -> tuple[str, ...]:
        """Called by a client right before the enrich request. Single use."""
        return self._guard._redeem(self)


class CreditGuard:
    def __init__(self, *, max_enrich_per_run: int, max_enrich_per_day: int, reserve: int,
                 used_today: int = 0, live: bool = False, enrich_enabled: bool = False,
                 remaining_credits: int | None = None, first_live_run: bool = False):
        # The very first live enrichment is limited to 1 record.
        self.first_live_run = bool(first_live_run)
        self.max_per_run = min(int(max_enrich_per_run), 1) if first_live_run else int(max_enrich_per_run)
        self.max_per_day = int(max_enrich_per_day)
        self.reserve = int(reserve)
        self.used_today = int(used_today)
        self.live = bool(live)
        self.enrich_enabled = bool(enrich_enabled)
        self.remaining_credits = remaining_credits
        self.used_this_run = 0
        self._outstanding = 0  # reserved by permits not yet settled
        self._issued: set[int] = set()

    @classmethod
    def from_config(cls, caps: dict, *, live: bool, enrich_enabled: bool,
                    used_today: int, remaining_credits: int | None,
                    first_live_run: bool = False) -> "CreditGuard":
        return cls(max_enrich_per_run=caps["max_enrich_per_run"],
                   max_enrich_per_day=caps["max_enrich_per_day"],
                   reserve=caps["reserve"], used_today=used_today, live=live,
                   enrich_enabled=enrich_enabled, remaining_credits=remaining_credits,
                   first_live_run=first_live_run)

    def budget(self) -> tuple[int, str]:
        """How many records may be enriched now, and what limits it."""
        if BULK_CREDITS_LOCKED:
            return 0, LOCK_REASON
        if not self.live:
            return 0, "dry-run (no --live flag)"
        if not self.enrich_enabled:
            return 0, "enrich_enabled is false in config"
        if self.remaining_credits is None:
            return 0, "remaining credits unknown (usage endpoint not read)"
        committed = self.used_this_run + self._outstanding
        limits = {
            "max_enrich_per_run": self.max_per_run - committed,
            "max_enrich_per_day": self.max_per_day - self.used_today - committed,
            "credit reserve": self.remaining_credits - self.reserve - committed,
        }
        name, room = min(limits.items(), key=lambda kv: kv[1])
        room = max(0, room)
        if room == 0:
            return 0, f"{name} reached"
        return room, f"limited by {name}"

    def issue_permit(self, contact_ids: Iterable[str]) -> EnrichPermit:
        ids: list[str] = []
        for cid in contact_ids:
            cid = str(cid).strip()
            if cid and cid not in ids:
                ids.append(cid)
        if not ids:
            raise CreditGuardError("no contact IDs requested")
        room, reason = self.budget()
        if room <= 0:
            raise CreditGuardError(f"enrichment blocked: {reason}")
        permit = EnrichPermit(self, tuple(ids[:room]), _key=_PERMIT_KEY)
        self._outstanding += len(permit.contact_ids)
        self._issued.add(id(permit))
        return permit

    def _redeem(self, permit: EnrichPermit) -> tuple[str, ...]:
        if id(permit) not in self._issued or permit._guard is not self:
            raise CreditGuardError("permit was not issued by this CreditGuard")
        if permit._redeemed:
            raise CreditGuardError("permit already used")
        permit._redeemed = True
        return permit.contact_ids

    def settle(self, permit: EnrichPermit, credits_used: int) -> None:
        """Record what the call actually cost and release the rest of the reservation."""
        if id(permit) not in self._issued or permit._settled:
            raise CreditGuardError("unknown or already settled permit")
        credits_used = max(0, min(int(credits_used), len(permit.contact_ids)))
        permit._settled = True
        self._outstanding -= len(permit.contact_ids)
        self.used_this_run += credits_used
