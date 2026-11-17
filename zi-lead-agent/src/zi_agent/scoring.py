"""Fit score 0-100 with configurable weights, plus a short breakdown text."""
from __future__ import annotations

from dataclasses import dataclass

from zi_agent.models import Contact

WEIGHT_KEYS = ("title", "email_hint", "phone_hint", "accuracy", "size_in_range", "country_allowed")
_LABELS = {"title": "title", "email_hint": "email", "phone_hint": "phone",
           "accuracy": "acc", "size_in_range": "size", "country_allowed": "country"}


@dataclass
class ScoreResult:
    score: int
    breakdown: str
    parts: dict[str, int]


def _fractions(contact: Contact, rank: int, n_priorities: int,
               employee_range: tuple[int, int], allowed_countries: list[str]) -> tuple[dict, dict]:
    notes = {}
    f = {}
    n = max(n_priorities, 1)
    f["title"] = max(0.0, (n - rank) / n)
    notes["title"] = f"#{rank + 1}"
    f["email_hint"] = 1.0 if contact.has_email else 0.0
    if contact.has_direct_phone or contact.has_mobile_phone:
        f["phone_hint"] = 1.0
    elif contact.has_company_phone:
        f["phone_hint"], notes["phone_hint"] = 0.5, "HQ only"
    else:
        f["phone_hint"] = 0.0
    acc = contact.accuracy_score
    f["accuracy"] = min(max((acc or 0) / 100, 0.0), 1.0)
    notes["accuracy"] = str(acc) if acc is not None else "n/a"
    lo, hi = employee_range
    emp = contact.employee_count
    if emp is None and "size" in contact.filter_matched:
        f["size_in_range"], notes["size_in_range"] = 1.0, "search filter"
    else:
        f["size_in_range"] = 1.0 if emp is not None and lo <= emp <= hi else 0.0
        if emp is None:
            notes["size_in_range"] = "unknown"
    allowed = {c.strip().lower() for c in allowed_countries}
    if not contact.country and "country" in contact.filter_matched:
        f["country_allowed"], notes["country_allowed"] = 1.0, "search filter"
    else:
        f["country_allowed"] = 1.0 if contact.country and contact.country.strip().lower() in allowed else 0.0
    return f, notes


def score_contact(contact: Contact, rank: int, n_priorities: int, weights: dict[str, float],
                  employee_range: tuple[int, int], allowed_countries: list[str],
                  new_company_boost: int = 0, is_new_company: bool = False) -> ScoreResult:
    total_w = sum(weights.get(k, 0) for k in WEIGHT_KEYS) or 1.0
    f, notes = _fractions(contact, rank, n_priorities, employee_range, allowed_countries)
    parts: dict[str, int] = {}
    texts = []
    for k in WEIGHT_KEYS:
        max_pts = round(100 * weights.get(k, 0) / total_w)
        pts = round(100 * weights.get(k, 0) * f[k] / total_w)
        parts[k] = pts
        note = f" ({notes[k]})" if k in notes else ""
        texts.append(f"{_LABELS[k]} {pts}/{max_pts}{note}")
    score = sum(parts.values())
    if is_new_company and new_company_boost:
        parts["new_company"] = new_company_boost
        score += new_company_boost
        texts.append(f"new co +{new_company_boost}")
    score = max(0, min(100, score))
    return ScoreResult(score, " | ".join(texts), parts)
