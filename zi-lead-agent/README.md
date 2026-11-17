# zi-lead-agent

A scheduled helper that prepares your ZoomInfo lead-research work. It uses the
**official ZoomInfo API only**. It does no browser automation or scraping, and
it never opens or views profiles. You still view the profiles yourself in the
web app. The agent decides *which* ones are worth a view, checks for
duplicates, and can fill rows using bulk credits.

| Step | What it does | Credits |
|---|---|---|
| 1 | Contact search with your saved filters | Free |
| 2 | Removes blocklisted companies and duplicates (master sheet, pending rows, queue) | Free |
| 3 | Picks one POC per company using your title rules, and scores each one 0-100 | Free |
| 4 | Writes the ranked **To-View Queue** tab | Free |
| 5 | *(optional, `--live` + `enrich_enabled`)* Enriches the top N rows into the master sheet, listing missing fields and flagging email-domain mismatches | Bulk credits, gated by CreditGuard |
| 6 | Sends a daily summary to you by Slack or email (counts only) | Free |
| Weekly | New Company Radar, weekly report (views, rows, verification rate, credits, capture speed), 12-month Refresh Due tab | Free |

---

## 1. Setup (Windows, one time)

1. Install Python 3.11 or newer and tick **Add Python to PATH**.
2. In a terminal:
   ```
   cd D:\zoominfo\zi-lead-agent
   pip install -r requirements.txt
   copy config.example.yaml config.yaml
   copy .env.example .env
   python -m pytest -q
   python run_agent.py check-config
   python run_agent.py run-daily          # dry-run with mock data
   ```
3. Open `out\lead_agent.mock.xlsx` and look at the **To-View Queue** tab.
4. Edit `config.yaml` (section 3 below). **Replace the example blocklist and
   search filters.** They are set up for the mock data.

### Going live (Milestone 4 checklist)
1. In the ZoomInfo DevPortal, create an app that uses **client credentials**
   and has the data scopes for contact search, enrich, company search and usage.
2. Put `ZOOMINFO_CLIENT_ID` and `ZOOMINFO_CLIENT_SECRET` into `.env`.
3. Run a search-only live run. Searching is free, and enrichment stays off
   while `enrich_enabled: false`:
   ```
   python run_agent.py run-daily --live
   ```
   This writes the real queue to `..\lead_agent.xlsx`. Check that the results
   match your saved search. If a filter value is rejected (HTTP 400), set the
   exact attribute under `zoominfo.contact_search_attributes`.
4. Check the credit bucket. `logs\api_calls.csv` shows the usage call. Confirm
   that `usage_limit_type: record` is the pool your ~100 bulk credits come from.
5. Only then set `enrich_enabled: true`. **The first live enrichment is capped
   at 1 record** no matter what the caps say. Open the new master row and check
   every field before raising `enrich_top_n` or `max_enrich_per_run`.

## 2. Commands

| Command | What it does |
|---|---|
| `python run_agent.py check-config` | Validates the config, shows resolved paths, and says whether each secret is **set** (never shows values) |
| `python run_agent.py run-daily [--live] [--notify]` | Builds the queue, optionally enriches, prints pacing, sends the summary |
| `python run_agent.py radar-weekly [--live]` | New Company Radar → `New Companies` and `Companies seen` tabs |
| `python run_agent.py report-weekly` | `reports\weekly-YYYY-Www.md` plus the `Refresh Due` tab (no API calls) |

- **Dry-run is the default.** It uses the MockClient and writes to
  `out\lead_agent.mock.xlsx`. It still reads your real master sheet, both for
  duplicate checks and for pacing.
- `--notify` sends the daily summary during a dry-run too, which is useful for
  testing Slack or email. Live runs always send it if a channel is configured.
- Exit codes: 0 = OK, 1 = config error, 2 = cannot go live (missing
  credentials), 3 = a workbook is open in Excel, 4 = ZoomInfo API error.

## 3. Config guide (`config.yaml`)

| Key | Meaning |
|---|---|
| `enrich_enabled` | Master switch for spending credits. Live enrichment needs this **and** `--live` **and** room in `caps`. |
| `paths.master_sheet_path` | Your master sheet, default `../leads_master.xlsx`. Created with the right headers if it is missing. |
| `paths.agent_workbook_path` | Where the queue, radar and report tabs go (default `../lead_agent.xlsx`). It is kept separate so it never fights the capture tool or Excel over the master file. |
| `search.filters` | `job_titles`, `management_levels`, `countries`, `employee_min/max`, `industries`, `company_ids` |
| `title_priority` | Ordered best to worst. Each line is a group of aliases. Matching ignores case, punctuation and *of/the/and/for*, and needs whole words. |
| `title_exclude` | Never pick titles containing these words (assistant, intern...). |
| `allowed_countries`, `employee_range` | Used in scoring. |
| `blocklist.companies / domains` | Legal suffixes are ignored. Subdomains are blocked too. |
| `dedupe.skip_companies_in_master` | One POC per company overall. |
| `caps` | `max_enrich_per_run`, `max_enrich_per_day`, `reserve`, `enrich_top_n`, `max_requests_per_second` (≤ 5; the API allows 25). |
| `scoring.weights` | `title`, `email_hint`, `phone_hint`, `accuracy`, `size_in_range`, `country_allowed`; scaled to 100. `new_company_boost` is added for radar companies. |
| `radar.filters` | `employee_growth_1y_min_pct`, `funded_within_days` or `funding_date_from/to`, `funding_amount_min/max` (in dollars), `industries`, `countries`, `employee_min/max`. `boost_days` sets how long the queue boost lasts. |
| `notify` | `channel: none / slack / email`. Secrets go in `.env`. |
| `zoominfo` | `usage_limit_type`, `enrich_output_fields`, and raw `contact_search_attributes` / `company_search_attributes` that override the mapping. |
| `monthly_view_budget`, `working_days_per_month` | Used for pacing (2000 / 21). |

### How things are decided
- **Duplicates:** checked in this order: ZoomInfo Contact ID, then POC + Company
  (ignoring spaces and capital letters, the capture tool's rule), then email.
  The check covers the master sheet, `pending_rows.csv` and the queue.
- **POC pick:** the best title in `title_priority`. Ties go to the higher
  accuracy score. Companies with no allowed title are skipped.
- **Queue re-runs:** `Claimed By` and `Status` are never overwritten. Your
  edits, extra columns and formulas (e.g. `=HYPERLINK`) are kept. Cell colours
  and comments are not kept, so put notes in a column instead.
  - An open row becomes **Done** once that person appears in the master sheet.
  - A **New** row becomes **Skipped** if its company is blocklisted later, or if
    you capture another POC from the same company. Claimed rows are never
    touched.
- **Excel open during a live run:**
  - If the master is open before enrichment, enrichment is skipped and no
    credits are spent.
  - If it gets locked while saving, the enriched rows go to
    `out\enriched_pending.csv`. They're added on the next live run and already
    count for duplicate checks, so credits are never wasted.
- **Dry-run never rewrites your master.** It only creates the file if it's
  missing. The two ZoomInfo ID columns are added on the first live run, after
  all of the capture tool's columns.
- **Network failure during enrich:** the call is logged as worst-case credits,
  so the daily cap stays correct. The first-live-run cap of 1 stays until a
  record actually comes back.
- **Queue size:** each run prints how many days of views the queue covers. It
  warns when the queue holds fewer rows than today's target. Search is free,
  so `search.max_results` defaults to 1000.
- **Pacing:** "views used" is an **approximation**: the number of master rows
  dated this month. The API cannot read view usage. Working days are Mon–Fri.
- **Capture speed:** if `capture_log.csv` exists (one timestamp per line), the
  report shows the average gap between captures. Gaps longer than 10 minutes
  count as breaks.
- **Verification rate:** taken from the capture tool's `Verified` column on
  this week's rows. Blank = not checked; no/n/false/invalid/bounced/wrong =
  failed; anything else = verified.

## 4. Windows Task Scheduler recipe

`scripts\run_daily.bat` and `scripts\run_weekly.bat` change to the project
folder and append their output to `logs\scheduler_*.log`.

Create the tasks in a terminal. Change the times as you like. Add `--live` to
the action only after the live checklist above is done.
```
schtasks /Create /TN "zi-lead-agent daily" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:30 ^
  /TR "\"D:\zoominfo\zi-lead-agent\scripts\run_daily.bat\""

schtasks /Create /TN "zi-lead-agent weekly" /SC WEEKLY /D MON /ST 08:00 ^
  /TR "\"D:\zoominfo\zi-lead-agent\scripts\run_weekly.bat\""
```
To use the GUI instead: Task Scheduler → **Create Task** → Triggers: weekly on
weekdays at 08:30 → Actions: *Start a program* = the `.bat` file, *Start in* =
`D:\zoominfo\zi-lead-agent\` → Settings: tick "Run task as soon as possible
after a scheduled start is missed".

To test a task now: `schtasks /Run /TN "zi-lead-agent daily"`, then read
`logs\scheduler_daily.log`.

Close `lead_agent.xlsx` before the scheduled time. If it is open, the run stops
with exit code 3 and your manual edits stay safe.

## 5. Security checklist
- [ ] `.env` holds the secrets and is listed in `.gitignore`. Never paste
      secrets into config files, chat or tickets.
- [ ] `check-config` only shows whether a secret is *set*, never its value.
- [ ] Logs (`logs\agent.log`, `logs\api_calls.csv`) hold IDs and counts only:
      no names, emails or phone numbers. API error messages never include the
      token or secret.
- [ ] The daily summary holds counts only.
- [ ] Workbooks, logs, reports and `out\` are git-ignored because they contain
      contact data. Keep them on an encrypted disk and limit who can read them.
- [ ] Only official API endpoints are called, with OAuth client credentials.
      No scraping, browser automation, cookies or hidden endpoints.
- [ ] CreditGuard is the only way to get an enrich permit. All three gates
      (`--live`, `enrich_enabled`, caps/reserve) must pass. The first live
      enrichment is capped at 1.
- [ ] Requests stay at ≤ 2/s by default. A 429 response is retried after
      `Retry-After`; an hourly or daily limit stops the run.
- [ ] Rotate the DevPortal client secret if it is ever exposed, and remove
      unused app scopes.
- [ ] No calling, emailing, outreach or dialer features. The only outbound
      message is your own status summary.

## 6. Verified API facts (official docs, 2026-11)
`docs.zoominfo.com` now redirects to `docs.gtm.ai`.

| Item | Value | Status |
|---|---|---|
| Base URL | `https://api.zoominfo.com/gtm` | verified |
| Auth | `POST /oauth/v1/token`, `grant_type=client_credentials`, HTTP Basic → `access_token`, `expires_in`; then `Authorization: Bearer` | verified |
| Contact search | `POST /data/v1/contacts/search`, `data.type=ContactSearch`, `page[number]`/`page[size]≤100`; returns `has*` hints and `contactAccuracyScore`, but **no** company website, size or country | verified |
| Contact enrich | `POST /data/v1/contacts/enrich`, `matchPersonInput` (≤25, `personId`), `outputFields`; response `data[]` with `type` Contact/NoMatch and `meta.matchStatus` | verified |
| Credits | 1 credit the first time a record is enriched in a rolling 12 months; free while "under management"; no-match is not charged | verified |
| Usage | `GET /data/v1/users/usage` → `usage[]` with `limitType`, `totalLimit`, `currentUsage`, `usageRemaining` | verified |
| Company search | `POST /data/v1/companies/search`; filters `oneYearEmployeeGrowthRateMin`, `fundingStartDate/EndDate`, `fundingAmountMin/Max` (thousands), `employeeRangeMin/Max`; growth and funding values are **not** returned | verified |
| Rate limits | 25 req/s by default; 429 with `Retry-After` and `X-RateLimit-Rejected-Bucket` | verified |

### Still marked TODO(verify)
- Value formats for multi-value search filters (comma-separated strings are
  assumed). `zoominfo.*_search_attributes` can override them.
- `firstName`, `lastName` and `companyId` as enrich `outputFields`.
- The exact shape of items in `externalUrls` and `companySocialMediaUrls`.
- That the `record` usage bucket is your bulk-credit pool.
- Whether an enrich response shows which records were actually charged. Until
  then, credits are counted at the worst case of 1 per matched record.
- `data.type` for company search (`CompanySearch` is assumed).
- Lookalike search (`GET /copilot/v1/companies/lookalikes`) exists, but its
  credit cost is not documented, so it remains a stub.

### Known limitation
Live contact search returns no company website. The **domain** blocklist
therefore runs again after enrichment (rows are skipped, but the credit is
already spent), while the **company-name** blocklist runs before. To protect
credits, blocklist companies by name as well.

## 7. Project layout
```
src/zi_agent/   config  models  storage  client  guard  dedupe  blocklist  poc
                scoring  queue  daily  enrich  pacing  radar  report  notify
                calllog  cli  fixtures/mock_contacts.json
tests/          unit + pipeline tests; fixtures/zoominfo/ (recorded-style API responses)
scripts/        Task Scheduler wrappers
```
Storage has an `ExcelStorage` implementation. A `GoogleSheetsStorage` class is
the placeholder for a Google Sheets version with the same interface.
