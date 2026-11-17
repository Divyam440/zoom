"""logs/api_calls.csv: one row per API call. IDs and counts only, never PII."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

FIELDS = ("timestamp", "endpoint", "record_count", "estimated_credits", "mode")


class ApiCallLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def log(self, endpoint: str, record_count: int, estimated_credits: int, mode: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow({
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                "endpoint": endpoint,
                "record_count": int(record_count),
                "estimated_credits": int(estimated_credits),
                "mode": mode,
            })

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def credits_between(self, start: dt.datetime, end: dt.datetime, mode: str = "live") -> int:
        total = 0
        for r in self.read():
            if r.get("mode") != mode:
                continue
            try:
                ts = dt.datetime.fromisoformat(r["timestamp"])
                credits = int(r.get("estimated_credits") or 0)
            except (KeyError, ValueError):
                continue
            if start <= ts < end:
                total += credits
        return total

    def has_live_enrich(self) -> bool:
        """True once a live enrich call has actually returned at least one record.
        (No-match or failed calls don't count: nothing was checked yet.)"""
        for r in self.read():
            ep = r.get("endpoint") or ""
            if r.get("mode") == "live" and "enrich" in ep and "(failed)" not in ep:
                try:
                    if int(r.get("record_count") or 0) > 0:
                        return True
                except ValueError:
                    continue
        return False

    def credits_on(self, day: dt.date, mode: str = "live") -> int:
        start = dt.datetime.combine(day, dt.time.min)
        return self.credits_between(start, start + dt.timedelta(days=1), mode)
