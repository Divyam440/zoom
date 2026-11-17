"""Weekly markdown report and the 12-month "Refresh Due" tab."""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from zi_agent.calllog import ApiCallLog
from zi_agent.dedupe import norm_id
from zi_agent.models import NEW_COMPANIES_TAB
from zi_agent.pacing import (APPROX_NOTE, Pacing, TimingStats, compute_pacing, compute_timing,
                             fmt_seconds, parse_master_date, read_capture_log)
from zi_agent.storage import Storage

REFRESH_TAB = "Refresh Due"
REFRESH_HEADERS = ["Sheet Row", "POC", "Company Name", "Date", "Days Old",
                   "ZoomInfo Contact ID", "Status"]
REFRESH_AFTER_DAYS = 365

_NO ={"no", "n", "false", "0", "invalid", "bounced", "wrong"}


def verification_state(value) -> str:
    """'verified', 'failed' or 'unchecked' from the capture tool's Verified column."""
    s = str(value or "").strip().lower()
    if not s:
        return "unchecked"
    if s in _NO:
        return "failed"
    return "verified"  # yes/y/true/✓ or any other note the user typed


def refresh_due_rows(master_rows: list[dict], today: dt.date,
                     days: int = REFRESH_AFTER_DAYS) -> list[dict]:
    out = []
    for i, r in enumerate(master_rows, start=2):  # row 1 is the header
        d = parse_master_date(r.get("Date"))
        if d and (today - d).days > days:
            out.append({
                "Sheet Row": i,
                "POC": r.get("POC") or "",
                "Company Name": r.get("Company Name") or "",
                "Date": d.isoformat(),
                "Days Old": (today - d).days,
                "ZoomInfo Contact ID": norm_id(r.get("ZoomInfo Contact ID")),
                "Status": "Refresh due",
            })
    out.sort(key=lambda r: -r["Days Old"])
    return out


@dataclass
class WeeklyStats:
    today: dt.date
    week_start: dt.date
    pacing: Pacing
    rows_week: int
    rows_month: int
    rows_total: int
    missing_pct_week: float | None
    missing_pct_total: float | None
    top_missing: list[tuple[str, int]]
    verified: int
    failed: int
    unchecked: int
    credits_week: int
    credits_month: int
    new_companies_week: int
    timing: TimingStats | None
    refresh_due: int
    notes: list[str] = field(default_factory=list)

    @property
    def verification_rate(self) -> float | None:
        checked = self.verified + self.failed
        return 100 * self.verified / checked if checked else None


def _missing_list(row: dict) -> list[str]:
    return [m.strip() for m in str(row.get("Missing Fields") or "").split(",") if m.strip()]


def compute_weekly(master_rows: list[dict], *, today: dt.date, budget: int, working_days: int,
                   call_log: ApiCallLog, new_company_rows: list[dict],
                   capture_log_path: Path | None, break_minutes: float = 10) -> WeeklyStats:
    week_start = today - dt.timedelta(days=6)
    month_start = today.replace(day=1)
    dated = [(parse_master_date(r.get("Date")), r) for r in master_rows]
    week_rows = [r for d, r in dated if d and week_start <= d <= today]
    month_rows = [r for d, r in dated if d and month_start <= d <= today]

    def pct_missing(rows):
        return 100 * sum(1 for r in rows if _missing_list(r)) / len(rows) if rows else None

    top = Counter(m for r in week_rows for m in _missing_list(r)).most_common(5)
    states = Counter(verification_state(r.get("Verified")) for r in week_rows)

    start_w = dt.datetime.combine(week_start, dt.time.min)
    start_m = dt.datetime.combine(month_start, dt.time.min)
    end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min)

    new_week = 0
    for r in new_company_rows:
        d = parse_master_date(r.get("First Seen"))
        if d and week_start <= d <= today:
            new_week += 1

    timing = None
    if capture_log_path is not None:
        timing = compute_timing(read_capture_log(capture_log_path), break_minutes, since=week_start)

    return WeeklyStats(
        today=today, week_start=week_start,
        pacing=compute_pacing(master_rows, today, budget, working_days),
        rows_week=len(week_rows), rows_month=len(month_rows), rows_total=len(master_rows),
        missing_pct_week=pct_missing(week_rows), missing_pct_total=pct_missing(master_rows),
        top_missing=top, verified=states["verified"], failed=states["failed"],
        unchecked=states["unchecked"],
        credits_week=call_log.credits_between(start_w, end, "live"),
        credits_month=call_log.credits_between(start_m, end, "live"),
        new_companies_week=new_week, timing=timing,
        refresh_due=len(refresh_due_rows(master_rows, today)),
    )


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f}%"


def render_markdown(s: WeeklyStats) -> str:
    p = s.pacing
    lines = [
        f"# Weekly lead report: {s.week_start.isoformat()} to {s.today.isoformat()}",
        "",
        "## Views (approximate)",
        f"- Views used this month: **{p.done_month} of {p.budget}**",
        f"- Pace: {abs(p.ahead_by):.0f} {'ahead' if p.ahead_by >= 0 else 'behind'} "
        f"(target to date {p.target_to_date:.0f}, working day {p.elapsed_working_days} of {p.working_days})",
        f"- Projected month-end total: {p.projected_month_end}",
        f"- _{APPROX_NOTE}_",
        "",
        "## Rows",
        f"- Rows added this week: **{s.rows_week}** (this month: {s.rows_month}, sheet total: {s.rows_total})",
        f"- Rows with missing fields this week: {_pct(s.missing_pct_week)} "
        f"(whole sheet: {_pct(s.missing_pct_total)})",
    ]
    if s.top_missing:
        lines.append("- Top missing fields this week: "
                     + ", ".join(f"{name} ({n})" for name, n in s.top_missing))
    lines += [
        "",
        "## Verification (from the 'Verified' column, this week's rows)",
        f"- Verification rate: **{_pct(s.verification_rate)}** "
        f"({s.verified} verified, {s.failed} failed, {s.unchecked} not checked yet)",
        "",
        "## API credits",
        f"- Enrich credits used (estimated, live calls): this week {s.credits_week}, "
        f"this month {s.credits_month}",
        "",
        "## New Company Radar",
        f"- New companies found this week: {s.new_companies_week}",
        "",
        "## Capture speed",
    ]
    if s.timing and s.timing.overall_avg_seconds is not None:
        lines.append(f"- Average time between captures: **{fmt_seconds(s.timing.overall_avg_seconds)}** "
                     "(longer gaps ignored as breaks)")
        for day, (avg, n) in sorted(s.timing.per_day.items()):
            lines.append(f"  - {day.isoformat()}: {fmt_seconds(avg)} over {n + 1} captures")
    else:
        lines.append("- No capture_log.csv data for this week.")
    lines += [
        "",
        "## Maintenance",
        f"- Rows older than 365 days (Refresh due): **{s.refresh_due}** (see the '{REFRESH_TAB}' tab)",
        "",
    ]
    return "\n".join(lines)


def write_weekly(storage: Storage, reports_dir: Path, master_rows: list[dict], stats: WeeklyStats) -> Path:
    storage.write_table(REFRESH_TAB, REFRESH_HEADERS, refresh_due_rows(master_rows, stats.today))
    reports_dir.mkdir(parents=True, exist_ok=True)
    year, week, _ = stats.today.isocalendar()
    path = reports_dir / f"weekly-{year}-W{week:02d}.md"
    path.write_text(render_markdown(stats), encoding="utf-8")
    return path


def new_company_rows(storage: Storage) -> list[dict]:
    return storage.read_table(NEW_COMPANIES_TAB)[1]
