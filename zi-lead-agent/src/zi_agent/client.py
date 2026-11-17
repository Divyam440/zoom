"""Lead-source client interface, MockClient, and the ZoomInfoClient placeholder.

All ZoomInfo-specific details (auth, endpoint paths, field names) stay inside
ZoomInfoClient. The rest of the app only sees the internal models.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass as _dataclass
from pathlib import Path

from zi_agent.blocklist import normalize_domain
from zi_agent.calllog import ApiCallLog
from zi_agent import guard as guard_mod
from zi_agent.guard import EnrichPermit
from zi_agent.models import Company, Contact, EnrichedContact, UsageInfo

DEFAULT_FIXTURES = Path(__file__).parent / "fixtures" / "mock_contacts.json"


class RateLimiter:
    """Simple minimum-interval limiter. The API allows 25 req/s; we default to 2."""

    def __init__(self, max_per_second: float):
        self.min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class LeadClient(ABC):
    mode = "abstract"

    def __init__(self, call_log: ApiCallLog | None = None, max_requests_per_second: float = 2):
        self.call_log = call_log
        self.limiter = RateLimiter(max_requests_per_second)

    def _log(self, endpoint: str, record_count: int, credits: int) -> None:
        if self.call_log:
            self.call_log.log(endpoint, record_count, credits, self.mode)

    @abstractmethod
    def search_contacts(self, filters: dict, max_results: int) -> list[Contact]:
        """Free. Results have data hints, never emails or phone numbers."""

    @abstractmethod
    def enrich_contacts(self, permit: EnrichPermit) -> list[EnrichedContact]:
        """Uses bulk credits. Must call permit.redeem() first."""

    @abstractmethod
    def get_usage(self) -> UsageInfo:
        """Remaining bulk credits from the usage endpoint."""

    def search_companies(self, filters: dict, max_results: int) -> list[Company]:
        raise NotImplementedError("Company search arrives in Milestone 3 (radar).")


class MockClient(LeadClient):
    """Offline client backed by a JSON fixture. Never touches the network."""
    mode = "mock"

    def __init__(self, fixtures_path: str | Path | None = None, call_log: ApiCallLog | None = None,
                 max_requests_per_second: float = 1000, today=None):
        super().__init__(call_log, max_requests_per_second)
        self.today = today  # fixes "funded N days ago" fixture dates in tests
        path = Path(fixtures_path) if fixtures_path else DEFAULT_FIXTURES
        data = json.loads(path.read_text(encoding="utf-8"))
        self._companies = {str(c["company_id"]): c for c in data["companies"]}
        self._contacts = data["contacts"]
        self._usage = data.get("usage", {})
        self._radar = data.get("radar_companies", [])

    def _contact(self, raw: dict) -> Contact:
        co = self._companies.get(str(raw["company_id"]), {})
        merged = {**{k: co.get(k) for k in ("company_name", "website", "employee_count",
                                            "industry", "country")}, **raw}
        merged = {k: v for k, v in merged.items() if v is not None}
        return Contact.from_dict(merged)

    def search_contacts(self, filters: dict, max_results: int) -> list[Contact]:
        self.limiter.wait()
        results = [self._contact(r) for r in self._contacts][:max_results]
        self._log("mock:search_contacts", len(results), 0)
        return results

    def enrich_contacts(self, permit: EnrichPermit) -> list[EnrichedContact]:
        ids = permit.redeem()
        self.limiter.wait()
        out = []
        for cid in ids:
            raw = next((r for r in self._contacts if str(r["contact_id"]) == cid), None)
            if raw is None:
                continue
            c = self._contact(raw)
            co = self._companies.get(c.company_id, {})
            domain = normalize_domain(c.website)
            first, last = c.first_name.lower(), c.last_name.lower()
            email = raw.get("mock_email") or (f"{first}.{last}@{domain}" if c.has_email and domain else "")
            out.append(EnrichedContact(
                contact_id=c.contact_id, company_id=c.company_id,
                first_name=c.first_name, last_name=c.last_name, job_title=c.job_title,
                company_name=c.company_name,
                employees=str(c.employee_count) if c.employee_count is not None else "",
                industry=c.industry, website=c.website,
                company_linkedin_url=co.get("linkedin_url", ""),
                email=email, company_country=c.country,
                company_phone=co.get("phone", "") if c.has_company_phone else "",
                mobile_phone="+1 555 0100 " + cid[-3:] if c.has_mobile_phone else "",
                direct_phone="+1 555 0199 " + cid[-3:] if c.has_direct_phone else "",
                poc_linkedin_url=f"https://www.linkedin.com/in/{first}-{last}-mock",
            ))
        self._log("mock:enrich_contacts", len(out), len(out))
        return out

    def search_companies(self, filters: dict, max_results: int) -> list[Company]:
        """Simulates server-side filtering with radar.matches_filters."""
        import datetime as dt
        from zi_agent.radar import matches_filters
        self.limiter.wait()
        today = self.today or dt.date.today()
        out = []
        for raw in self._radar:
            days_ago = raw.get("last_funding_days_ago")
            c = Company(
                company_id=str(raw["company_id"]), name=raw["name"], website=raw.get("website", ""),
                employee_count=raw.get("employee_count"), industry=raw.get("industry", ""),
                country=raw.get("country", ""), employee_growth_1y_pct=raw.get("employee_growth_1y_pct"),
                last_funding_date=(today - dt.timedelta(days=days_ago)).isoformat()
                if days_ago is not None else "",
                last_funding_amount=raw.get("last_funding_amount"))
            if matches_filters(c, filters):
                out.append(c)
        out = out[:max_results]
        self._log("mock:search_companies", len(out), 0)
        return out

    def get_usage(self) -> UsageInfo:
        self.limiter.wait()
        self._log("mock:usage", 0, 0)
        return UsageInfo(remaining_credits=self._usage.get("remaining"),
                         limit=self._usage.get("limit"), used=self._usage.get("used"))


# ===========================================================================
# Live client - official ZoomInfo GTM API (docs.zoominfo.com -> docs.gtm.ai)
# ===========================================================================
# Verified in the official docs (2026-11):
#   base URL      https://api.zoominfo.com/gtm
#   auth          POST /oauth/v1/token, grant_type=client_credentials,
#                 HTTP Basic client_id:client_secret -> access_token, expires_in
#                 then "Authorization: Bearer <token>"
#   contacts      POST /data/v1/contacts/search   body data.type=ContactSearch
#                 paging query page[number] (from 1), page[size] (1-100)
#   enrich        POST /data/v1/contacts/enrich   data.type=ContactEnrich,
#                 matchPersonInput (1-25 items, personId), outputFields
#                 response data[] items type Contact|NoMatch, meta.matchStatus
#   companies     POST /data/v1/companies/search
#   usage         GET  /data/v1/users/usage -> data[].attributes.usage[]
#                 {limitType, totalLimit, currentUsage, usageRemaining}
#   rate limits   429 + Retry-After, X-RateLimit-Rejected-Bucket (second|hour|day)
# Sources: docs.gtm.ai/docs/client-credentials-flow, /reference/searchinterface_searchcontact,
#   /reference/enrichinterface_enrichcontact, /reference/searchinterface_searchcompany,
#   /reference/userinterface_userusage, /docs/rate-limits, /docs/credit-usage-and-limits
BASE_URL = "https://api.zoominfo.com/gtm"
TOKEN_PATH = "/oauth/v1/token"
CONTACT_SEARCH_PATH = "/data/v1/contacts/search"
CONTACT_ENRICH_PATH = "/data/v1/contacts/enrich"
COMPANY_SEARCH_PATH = "/data/v1/companies/search"
USAGE_PATH = "/data/v1/users/usage"
ENRICH_BATCH_MAX = 25
PAGE_SIZE_MAX = 100
JSONAPI = "application/vnd.api+json"

# Verified output field names, except where marked.
DEFAULT_ENRICH_OUTPUT_FIELDS = [
    "firstName", "lastName",  # TODO(verify): not in the field list I checked; names are essential
    "companyId",              # TODO(verify)
    "jobTitle", "email", "phone", "mobilePhone", "companyName", "companyWebsite",
    "companyEmployeeCount", "companyPrimaryIndustry", "companyPhone", "country",
    "companyCountry", "externalUrls", "companySocialMediaUrls",
]
_UNCHARGED_STATUSES = {"NO_MATCH", "LIMIT_EXCEEDED", "OPT_OUT"}


class ZoomInfoConfigError(RuntimeError):
    pass


class ZoomInfoApiError(RuntimeError):
    pass


@_dataclass
class HttpResponse:
    status: int
    headers: dict
    body: bytes

    def header(self, name: str) -> str:
        low = {k.lower(): v for k, v in (self.headers or {}).items()}
        return str(low.get(name.lower(), ""))

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8")) if self.body else {}


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None,
                     timeout: float = 30) -> HttpResponse:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return HttpResponse(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, dict(e.headers or {}), e.read() if e.fp else b"")
    except (urllib.error.URLError, OSError) as e:  # timeout, DNS, connection reset
        raise ZoomInfoApiError(f"network error: {type(e).__name__}") from None


def _sanitize(obj):
    """API text must never become an Excel formula ('=...' -> literal text)."""
    from dataclasses import fields as _fields
    for f in _fields(obj):
        v = getattr(obj, f.name)
        if isinstance(v, str) and v.startswith("="):
            setattr(obj, f.name, "'" + v)
    return obj


def _int_or_none(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _csv(v) -> str:
    return ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)


def map_contact_filters(filters: dict) -> tuple[dict, tuple[str, ...]]:
    """Internal filter names -> Contact Search attributes.
    Field NAMES are verified. TODO(verify): value formats (comma-separated
    strings are assumed for multi-value fields). Exact attributes can always be
    given in config zoominfo.contact_search_attributes, which override these."""
    attrs: dict = {}
    matched: list[str] = []
    f = {k: v for k, v in (filters or {}).items() if v not in (None, "", [])}
    if "job_titles" in f:
        attrs["jobTitle"] = _csv(f["job_titles"])
    if "management_levels" in f:
        attrs["managementLevel"] = _csv(f["management_levels"])
    if "countries" in f:
        attrs["country"] = _csv(f["countries"])
        matched.append("country")
    if "employee_min" in f:
        attrs["employeeRangeMin"] = str(f["employee_min"])
    if "employee_max" in f:
        attrs["employeeRangeMax"] = str(f["employee_max"])
    if "employee_min" in f or "employee_max" in f:
        matched.append("size")
    if "industries" in f:
        attrs["industryKeywords"] = _csv(f["industries"])
    if "company_ids" in f:
        attrs["companyId"] = _csv(f["company_ids"])
    return attrs, tuple(matched)


def map_company_filters(filters: dict) -> dict:
    """Internal radar filters -> Company Search attributes (names verified;
    TODO(verify) value formats). Funding amounts are sent in thousands, as the
    docs specify."""
    attrs: dict = {}
    f = {k: v for k, v in (filters or {}).items() if v not in (None, "", [])}
    if "employee_growth_1y_min_pct" in f:
        attrs["oneYearEmployeeGrowthRateMin"] = str(f["employee_growth_1y_min_pct"])
    if "funding_date_from" in f:
        attrs["fundingStartDate"] = str(f["funding_date_from"])
    if "funding_date_to" in f:
        attrs["fundingEndDate"] = str(f["funding_date_to"])
    if "funding_amount_min" in f:
        attrs["fundingAmountMin"] = str(int(float(f["funding_amount_min"]) // 1000))
    if "funding_amount_max" in f:
        attrs["fundingAmountMax"] = str(int(float(f["funding_amount_max"]) // 1000))
    if "industries" in f:
        attrs["industryKeywords"] = _csv(f["industries"])
    if "countries" in f:
        attrs["country"] = _csv(f["countries"])
    if "employee_min" in f:
        attrs["employeeRangeMin"] = str(f["employee_min"])
    if "employee_max" in f:
        attrs["employeeRangeMax"] = str(f["employee_max"])
    return attrs


def _text(v) -> str:
    """Flatten a value that may be a string, number, dict {name|value|url} or list."""
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("name", "value", "url", "number"):
            if v.get(k):
                return str(v[k])
        return ""
    if isinstance(v, list):
        return _text(v[0]) if v else ""
    return str(v)


def _find_url(v, needle: str) -> str:
    """Pick a LinkedIn URL out of externalUrls/companySocialMediaUrls.
    TODO(verify): exact item shape; strings and {type,url} dicts are handled."""
    items = v if isinstance(v, list) else [v]
    for it in items:
        url = _text(it) if not isinstance(it, str) else it
        if needle in url.lower():
            return url
    return ""


class ZoomInfoClient(LeadClient):
    """Official ZoomInfo API client (OAuth client credentials from the DevPortal)."""
    mode = "live"

    def __init__(self, call_log: ApiCallLog | None = None, max_requests_per_second: float = 2, *,
                 client_id: str | None = None, client_secret: str | None = None,
                 transport=None, sleep=time.sleep, clock=time.monotonic,
                 usage_limit_type: str = "record", enrich_output_fields: list[str] | None = None,
                 contact_search_attributes: dict | None = None,
                 company_search_attributes: dict | None = None, base_url: str = BASE_URL,
                 page_size: int = PAGE_SIZE_MAX):
        super().__init__(call_log, max_requests_per_second)
        self._client_id = client_id or os.environ.get("ZOOMINFO_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get("ZOOMINFO_CLIENT_SECRET", "")
        if not self._client_id or not self._client_secret:
            raise ZoomInfoConfigError(
                "ZOOMINFO_CLIENT_ID and ZOOMINFO_CLIENT_SECRET must be set (environment or .env).")
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self.base_url = base_url.rstrip("/")
        self.page_size = max(1, min(int(page_size), PAGE_SIZE_MAX))
        self.usage_limit_type = usage_limit_type
        self.enrich_output_fields = list(enrich_output_fields or DEFAULT_ENRICH_OUTPUT_FIELDS)
        self.contact_search_attributes = dict(contact_search_attributes or {})
        self.company_search_attributes = dict(company_search_attributes or {})

    # -- auth -----------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and self._clock() < self._token_expires:
            return self._token
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        self.limiter.wait()
        resp = self._transport(
            "POST", self.base_url + TOKEN_PATH,
            {"Authorization": f"Basic {basic}", "Accept": "application/json",
             "Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode({"grant_type": "client_credentials"}).encode())
        if resp.status != 200:
            raise ZoomInfoApiError(f"authentication failed: HTTP {resp.status}")
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise ZoomInfoApiError("authentication response had no access_token")
        expires_in = float(data.get("expires_in") or 300)
        self._token = token
        self._token_expires = self._clock() + max(expires_in - 60, expires_in / 2)
        return token

    # -- transport with retries -------------------------------------------------
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> dict:
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        payload = json.dumps(body).encode() if body is not None else None
        refreshed = False
        for attempt in range(5):
            token = self._get_token()
            self.limiter.wait()
            headers = {"Authorization": f"Bearer {token}", "Accept": JSONAPI}
            if payload is not None:
                headers["Content-Type"] = JSONAPI
            resp = self._transport(method, url, headers, payload)
            if resp.status == 401 and not refreshed:
                self._token, refreshed = None, True
                continue
            if resp.status == 429:
                bucket = resp.header("X-RateLimit-Rejected-Bucket")
                try:
                    wait = float(resp.header("Retry-After"))
                except ValueError:
                    wait = min(5.0, 2.0 ** attempt)
                if bucket in ("hour", "day") or wait > 60:
                    raise ZoomInfoApiError(f"{bucket or 'API'} rate limit reached; retry after {wait:.0f}s")
                self._sleep(max(wait, 1.0))
                continue
            if resp.status >= 500:
                self._sleep(min(5.0, 2.0 ** attempt))
                continue
            if resp.status >= 400:
                raise ZoomInfoApiError(f"{method} {path} -> HTTP {resp.status}: {self._error_detail(resp)}")
            return resp.json()
        raise ZoomInfoApiError(f"{method} {path}: gave up after retries")

    @staticmethod
    def _error_detail(resp: HttpResponse) -> str:
        try:
            errs = resp.json().get("errors") or []
            if errs:
                return str(errs[0].get("detail") or errs[0].get("title") or "")[:200]
        except (ValueError, AttributeError):
            pass
        return ""

    # -- contact search (free) --------------------------------------------------
    def search_contacts(self, filters: dict, max_results: int) -> list[Contact]:
        attrs, matched = map_contact_filters(filters)
        attrs.update(self.contact_search_attributes)
        size = max(1, min(self.page_size, max_results))
        out: list[Contact] = []
        page = 1
        while len(out) < max_results:
            data = self._request("POST", CONTACT_SEARCH_PATH,
                                 params={"page[number]": page, "page[size]": size},
                                 body={"data": {"type": "ContactSearch", "attributes": attrs}})
            items = data.get("data") or []
            self._log("POST " + CONTACT_SEARCH_PATH, len(items), 0)
            out.extend(self._map_contact(i, matched) for i in items)
            if len(items) < size or not (data.get("links") or {}).get("next"):
                break
            page += 1
        return out[:max_results]

    @staticmethod
    def _map_contact(item: dict, matched: tuple[str, ...]) -> Contact:
        a = item.get("attributes") or {}
        co = a.get("company") or {}
        return _sanitize(Contact(
            contact_id=str(item.get("id", "")), first_name=a.get("firstName") or "",
            last_name=a.get("lastName") or "", job_title=a.get("jobTitle") or "",
            company_id=str(co.get("id") or ""), company_name=co.get("name") or "",
            accuracy_score=a.get("contactAccuracyScore"),
            has_email=bool(a.get("hasEmail")), has_direct_phone=bool(a.get("hasDirectPhone")),
            has_mobile_phone=bool(a.get("hasMobilePhone")),
            has_company_phone=bool(a.get("hasCompanyPhone")),
            filter_matched=matched))

    # -- enrich (bulk credits; CreditGuard permit required) ---------------------
    def enrich_contacts(self, permit: EnrichPermit) -> list[EnrichedContact]:
        # Last line of defence: no enrich request is ever sent while locked.
        if guard_mod.BULK_CREDITS_LOCKED:
            raise guard_mod.CreditGuardError(f"enrichment blocked: {guard_mod.LOCK_REASON}")
        ids = permit.redeem()
        out: list[EnrichedContact] = []
        for start in range(0, len(ids), ENRICH_BATCH_MAX):
            batch = ids[start:start + ENRICH_BATCH_MAX]
            body = {"data": {"type": "ContactEnrich", "attributes": {
                "matchPersonInput": [{"personId": int(i) if i.isdigit() else i} for i in batch],
                "outputFields": self.enrich_output_fields}}}
            try:
                data = self._request("POST", CONTACT_ENRICH_PATH, body=body)
            except ZoomInfoApiError:
                # Unknown whether ZoomInfo charged: log the worst case so the
                # daily cap stays correct on a re-run, then stop.
                self._log("POST " + CONTACT_ENRICH_PATH + " (failed)", len(batch), len(batch))
                raise
            items = data.get("data") or []
            charged = [i for i in items if i.get("type") == "Contact"
                       and (i.get("meta") or {}).get("matchStatus") not in _UNCHARGED_STATUSES]
            # Worst case: every matched record charged (re-enrich within 12 months is
            # free, but the response does not say which were free).
            self._log("POST " + CONTACT_ENRICH_PATH, len(charged), len(charged))
            out.extend(self._map_enriched(i) for i in charged)
            if any((i.get("meta") or {}).get("matchStatus") == "LIMIT_EXCEEDED" for i in items):
                break
        return out

    @staticmethod
    def _map_enriched(item: dict) -> EnrichedContact:
        a = item.get("attributes") or {}
        emp = a.get("companyEmployeeCount")
        return _sanitize(EnrichedContact(
            contact_id=str(item.get("id", "")),
            company_id=str(a.get("companyId") or (a.get("company") or {}).get("id") or ""),
            first_name=a.get("firstName") or "", last_name=a.get("lastName") or "",
            job_title=a.get("jobTitle") or "", company_name=a.get("companyName") or "",
            employees=str(emp) if emp not in (None, "") else "",
            industry=_text(a.get("companyPrimaryIndustry")),
            website=_text(a.get("companyWebsite")),
            company_linkedin_url=_find_url(a.get("companySocialMediaUrls"), "linkedin.com/company"),
            email=_text(a.get("email")),
            company_country=_text(a.get("companyCountry")) or _text(a.get("country")),
            company_phone=_text(a.get("companyPhone")),
            mobile_phone=_text(a.get("mobilePhone")),
            direct_phone=_text(a.get("phone")),
            poc_linkedin_url=_find_url(a.get("externalUrls"), "linkedin.com/in")))

    # -- usage (free) ------------------------------------------------------------
    def get_usage(self) -> UsageInfo:
        data = self._request("GET", USAGE_PATH)
        self._log("GET " + USAGE_PATH, 0, 0)
        for d in data.get("data") or []:
            for u in (d.get("attributes") or {}).get("usage") or []:
                # TODO(verify): that limitType "record" is the bulk-credit pool.
                if str(u.get("limitType")) == self.usage_limit_type:
                    # Unparsable -> None -> CreditGuard blocks enrichment.
                    return UsageInfo(_int_or_none(u.get("usageRemaining")),
                                     _int_or_none(u.get("totalLimit")), _int_or_none(u.get("currentUsage")))
        return UsageInfo(remaining_credits=None)  # unknown -> CreditGuard blocks enrich

    # -- company search (free) ---------------------------------------------------
    def search_companies(self, filters: dict, max_results: int) -> list[Company]:
        attrs = map_company_filters(filters)
        attrs.update(self.company_search_attributes)
        size = max(1, min(self.page_size, max_results))
        out: list[Company] = []
        page = 1
        while len(out) < max_results:
            data = self._request("POST", COMPANY_SEARCH_PATH,
                                 params={"page[number]": page, "page[size]": size},
                                 # TODO(verify): data.type value for company search
                                 body={"data": {"type": "CompanySearch", "attributes": attrs}})
            items = data.get("data") or []
            self._log("POST " + COMPANY_SEARCH_PATH, len(items), 0)
            for i in items:
                a = i.get("attributes") or {}
                out.append(_sanitize(Company(company_id=str(i.get("id", "")), name=a.get("name") or "",
                                             website=_text(a.get("website")),
                                             employee_count=_int_or_none(a.get("employeeCount")),
                                             country=_text(a.get("country")))))
            if len(items) < size or not (data.get("links") or {}).get("next"):
                break
            page += 1
        return out[:max_results]
