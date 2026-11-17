"""Duplicate detection against the master sheet, pending rows and the queue.

Order of checks: ZoomInfo Contact ID first, then normalized POC name + company.
The name+company key extends zoominfo_capture._dedupe_key (strip + lowercase):
it also ignores punctuation and legal suffixes ("Acme, Inc." == "Acme Inc"),
so anything the capture tool treats as a duplicate is also a duplicate here,
plus spelling variants between the API and copied pages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from zi_agent.blocklist import normalize_company
from zi_agent.models import Contact


def collapse(value) -> str:
    return " ".join(str(value or "").split()).lower()


def normalize_person(value) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", str(value or "").lower()).split())


def norm_id(value) -> str:
    """IDs may come back from Excel as int/float; normalize to a plain string."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def name_company_key(poc, company) -> tuple[str, str]:
    return normalize_person(poc), normalize_company(company)


@dataclass
class SeenIndex:
    """Everything already handled, for fast duplicate checks."""
    label: str
    contact_ids: set[str] = field(default_factory=set)
    name_company: set[tuple[str, str]] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    company_ids: set[str] = field(default_factory=set)
    company_names: set[str] = field(default_factory=set)

    def add(self, *, contact_id="", poc="", company="", email="", company_id="") -> None:
        cid = norm_id(contact_id)
        if cid:
            self.contact_ids.add(cid)
        key = name_company_key(poc, company)
        if key[0] and key[1]:
            self.name_company.add(key)
        if email and str(email).strip():
            self.emails.add(str(email).strip().lower())
        coid = norm_id(company_id)
        if coid:
            self.company_ids.add(coid)
        if normalize_company(company):
            self.company_names.add(normalize_company(company))

    def match(self, contact_id="", poc="", company="", email="") -> str | None:
        """Return why this person is a duplicate, or None."""
        cid = norm_id(contact_id)
        if cid and cid in self.contact_ids:
            return f"contact ID already in {self.label}"
        key = name_company_key(poc, company)
        if key[0] and key[1] and key in self.name_company:
            return f"name + company already in {self.label}"
        if email and str(email).strip().lower() in self.emails:
            return f"email already in {self.label}"
        return None

    def match_contact(self, c: Contact) -> str | None:
        return self.match(c.contact_id, c.full_name, c.company_name)

    def has_company(self, company_id="", company_name="") -> bool:
        coid = norm_id(company_id)
        if coid and coid in self.company_ids:
            return True
        name = normalize_company(company_name)
        return bool(name) and name in self.company_names

    # -- builders ---------------------------------------------------------
    @classmethod
    def from_master_rows(cls, rows: Iterable[dict], label: str = "master sheet") -> "SeenIndex":
        idx = cls(label)
        for r in rows:
            idx.add(contact_id=r.get("ZoomInfo Contact ID"), poc=r.get("POC"),
                    company=r.get("Company Name"), email=r.get("Email ID"),
                    company_id=r.get("ZoomInfo Company ID"))
        return idx

    @classmethod
    def from_queue_rows(cls, rows: Iterable[dict], label: str = "queue") -> "SeenIndex":
        idx = cls(label)
        for r in rows:
            idx.add(contact_id=r.get("Contact ID"), poc=r.get("POC"),
                    company=r.get("Company"), company_id=r.get("Company ID"))
        return idx


@dataclass
class DedupeResult:
    kept: list[Contact]
    dropped: list[tuple[Contact, str]]


def dedupe_contacts(contacts: Iterable[Contact], *indexes: SeenIndex) -> DedupeResult:
    """Drop contacts found in any index, and duplicates within the batch itself."""
    batch = SeenIndex("this search batch")
    kept: list[Contact] = []
    dropped: list[tuple[Contact, str]] = []
    for c in contacts:
        reason = None
        for idx in indexes:
            reason = idx.match_contact(c)
            if reason:
                break
        reason = reason or batch.match_contact(c)
        if reason:
            dropped.append((c, reason))
            continue
        batch.add(contact_id=c.contact_id, poc=c.full_name, company=c.company_name)
        kept.append(c)
    return DedupeResult(kept, dropped)
