"""Blocklist by company name and by website domain (subdomains included)."""
from __future__ import annotations

import re

from zi_agent.models import Contact

_LEGAL_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co",
                   "gmbh", "plc", "llp", "sa", "ag", "bv", "pty", "group"}


def normalize_company(name) -> str:
    """'Acme, Inc.' == 'ACME Inc' == 'Acme'. Unicode letters are kept."""
    words = re.sub(r"[\W_]+", " ", str(name or "").lower()).split()
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalize_domain(value) -> str:
    """'https://www.Example.com/about' -> 'example.com'. Emails -> their domain."""
    s = str(value or "").strip().lower()
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/")[0].split("?")[0].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    return s.strip(".")


def domain_matches(domain: str, blocked: str) -> bool:
    return bool(domain) and (domain == blocked or domain.endswith("." + blocked))


class Blocklist:
    def __init__(self, companies=(), domains=()):
        self.companies = {normalize_company(c) for c in companies if normalize_company(c)}
        self.domains = {normalize_domain(d) for d in domains if normalize_domain(d)}

    def reason(self, company_name: str, website: str) -> str | None:
        if normalize_company(company_name) in self.companies:
            return "blocklisted company"
        dom = normalize_domain(website)
        if any(domain_matches(dom, b) for b in self.domains):
            return "blocklisted domain"
        return None

    def split(self, contacts: list[Contact]) -> tuple[list[Contact], list[tuple[Contact, str]]]:
        kept, blocked = [], []
        for c in contacts:
            r = self.reason(c.company_name, c.website)
            if r:
                blocked.append((c, r))
            else:
                kept.append(c)
        return kept, blocked
