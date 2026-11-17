"""View-credit pacing and capture timing.

Views cannot be read from the API. "Views used this month" is APPROXIMATED as
the number of master-sheet rows dated in the current calendar month.
Working days are Monday-Friday.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path

APPROX_NOTE = ("Views used is an approximation: rows added to the master sheet this "
               "calendar month (ZoomInfo does not expose view usage through the API).")

_DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y")


def parse_master_date(value) -> dt.date | None:
    """The capture tool writes dd-mm-yyyy; Excel may hand back datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip().split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def working_days_between(start: dt.date, end: dt.date) -> int:
    """Mon-Fri days from start to end inclusive."""
    if end < start:
        return 0
    days = (end - start).days + 1
    return sum(1 for i in range(days) if (start + dt.timedelta(days=i)).weekday() < 5)


@dataclass
class Pacing:
    today: dt.date
    budget: int
    working_days: int
    done_month: int
    done_today: int
    elapsed_working_days: int
    daily_target: float
    target_to_date: float
    ahead_by: float           # negative = behind
    target_today: int         # what to do today to finish the budget on time
    projected_month_end: int

    def lines(self) -> list[str]:
        status = "ahead" if self.ahead_by >= 0 else "behind"
        return [
            f"Views used this month (approx.) : {self.done_month} of {self.budget}",
            f"Done today                      : {self.done_today}",
            f"Target for today                : {self.target_today} "
            f"(plain daily pace {self.daily_target:.0f})",
            f"Pace                            : {abs(self.ahead_by):.0f} {status} "
            f"(target to date {self.target_to_date:.0f}, working day "
            f"{self.elapsed_working_days} of {self.working_days})",
            f"Projected month-end total       : {self.projected_month_end}",
            f"Note: {APPROX_NOTE}",
        ]


def compute_pacing(master_rows: list[dict], today: dt.date, budget: int,
                   working_days_per_month: int) -> Pacing:
    month_start = today.replace(day=1)
    dates = [parse_master_date(r.get("Date")) for r in master_rows]
    done_month = sum(1 for d in dates if d and month_start <= d <= today)
    done_today = sum(1 for d in dates if d == today)

    elapsed = min(working_days_between(month_start, today), working_days_per_month)
    # Count today as a working day for targets even on a weekend run.
    elapsed_incl_today = max(elapsed, 1)
    daily_target = budget / working_days_per_month
    target_to_date = daily_target * elapsed_incl_today
    done_before_today = done_month - done_today
    days_left_incl_today = max(working_days_per_month - elapsed_incl_today + 1, 1)
    target_today = max(0, math.ceil((budget - done_before_today) / days_left_incl_today))
    projected = round(done_month / elapsed_incl_today * working_days_per_month)
    return Pacing(today, budget, working_days_per_month, done_month, done_today,
                  elapsed_incl_today, daily_target, target_to_date,
                  done_month - target_to_date, target_today, projected)


# -- timing log ---------------------------------------------------------------
@dataclass
class TimingStats:
    per_day: dict[dt.date, tuple[float, int]]  # date -> (avg seconds, intervals counted)
    overall_avg_seconds: float | None
    captures: int


def _parse_ts(s: str) -> dt.datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def read_capture_log(path: str | Path) -> list[dt.datetime]:
    """One timestamp per line/row. A header row and unparsable lines are ignored."""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if row:
                ts = _parse_ts(row[0])
                if ts:
                    out.append(ts)
    return sorted(out)


def compute_timing(timestamps: list[dt.datetime], break_minutes: float = 10,
                   since: dt.date | None = None) -> TimingStats | None:
    if since:
        timestamps = [t for t in timestamps if t.date() >= since]
    if len(timestamps) < 2:
        return None
    by_day: dict[dt.date, list[dt.datetime]] = {}
    for t in sorted(timestamps):
        by_day.setdefault(t.date(), []).append(t)
    per_day = {}
    all_gaps: list[float] = []
    limit = break_minutes * 60
    for day, ts in by_day.items():
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
        gaps = [g for g in gaps if 0 < g <= limit]
        if gaps:
            per_day[day] = (sum(gaps) / len(gaps), len(gaps))
            all_gaps.extend(gaps)
    overall = sum(all_gaps) / len(all_gaps) if all_gaps else None
    return TimingStats(per_day, overall, len(timestamps))


def fmt_seconds(s: float | None) -> str:
    if s is None:
        return "n/a"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s" if m else f"{sec}s"
