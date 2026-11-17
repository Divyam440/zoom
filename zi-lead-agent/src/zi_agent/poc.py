"""Pick ONE point of contact per company.

Highest title in title_priority wins; ties go to the higher accuracy score.
Companies with no allowed title are skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from zi_agent.dedupe import collapse, norm_id
from zi_agent.models import Contact

_STOPWORDS = {"of", "the", "and", "for"}


def normalize_title(title) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", str(title or "").lower())
    return " ".join(w for w in s.split() if w not in _STOPWORDS)


def title_rank(title, priority: list[list[str]], exclude: list[str] = ()) -> tuple[int, str] | None:
    """Return (rank, matched alias) with rank 0 = best, or None if not allowed."""
    padded = f" {normalize_title(title)} "
    if not padded.strip():
        return None
    for word in exclude:
        if f" {normalize_title(word)} " in padded:
            return None
    for rank, aliases in enumerate(priority):
        for alias in aliases:
            na = normalize_title(alias)
            if na and f" {na} " in padded:
                return rank, alias
    return None


def company_key(c: Contact) -> str:
    return norm_id(c.company_id) or "name:" + collapse(c.company_name)


@dataclass
class PocPick:
    contact: Contact
    rank: int
    matched_title: str


def pick_pocs(contacts: list[Contact], priority: list[list[str]],
              exclude: list[str] = ()) -> tuple[list[PocPick], list[str]]:
    """Return (one pick per company, company keys skipped for no allowed title)."""
    by_company: dict[str, list[Contact]] = {}
    for c in contacts:
        by_company.setdefault(company_key(c), []).append(c)

    picks: list[PocPick] = []
    skipped: list[str] = []
    for key, group in by_company.items():
        ranked = []
        for c in group:
            r = title_rank(c.job_title, priority, exclude)
            if r is not None:
                ranked.append(PocPick(c, r[0], r[1]))
        if not ranked:
            skipped.append(key)
            continue
        ranked.sort(key=lambda p: (p.rank, -(p.contact.accuracy_score or 0), p.contact.contact_id))
        picks.append(ranked[0])
    return picks, skipped
