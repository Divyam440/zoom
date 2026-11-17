import datetime as dt

import pytest

from zi_agent import notify
from zi_agent.calllog import ApiCallLog
from zi_agent.pacing import (compute_pacing, compute_timing, parse_master_date,
                             read_capture_log, working_days_between)
from zi_agent.report import (REFRESH_TAB, compute_weekly, refresh_due_rows, render_markdown,
                             verification_state, write_weekly)

TODAY = dt.date(2026, 11, 16)  # a Monday; Nov 2026 has 11 weekdays up to today


def test_parse_master_date_formats():
    assert parse_master_date("16-11-2026") == TODAY            # capture tool format
    assert parse_master_date("2026-11-16") == TODAY
    assert parse_master_date(dt.datetime(2026, 11, 16, 9, 0)) == TODAY
    assert parse_master_date("") is None
    assert parse_master_date("garbage") is None


def test_working_days():
    assert working_days_between(dt.date(2026, 11, 1), TODAY) == 11


def test_pacing_numbers():
    rows = ([{"Date": "10-11-2026"}] * 950 + [{"Date": "16-11-2026"}] * 50
            + [{"Date": "30-10-2026"}] * 30 + [{"Date": ""}])
    p = compute_pacing(rows, TODAY, budget=2100, working_days_per_month=21)
    assert p.done_month == 1000 and p.done_today == 50
    assert p.daily_target == 100
    assert p.target_to_date == 1100
    assert p.ahead_by == -100
    assert p.target_today == 105          # (2100-950) / 11 days left incl. today
    assert p.projected_month_end == 1909
    text = "\n".join(p.lines())
    assert "100 behind" in text and "approximation" in text


def test_pacing_first_day_of_month_no_division_error():
    p = compute_pacing([], dt.date(2026, 11, 1), 2000, 21)   # a Sunday
    assert p.done_month == 0 and p.projected_month_end == 0
    assert p.target_today == 96


def test_timing_ignores_breaks(tmp_path):
    log = tmp_path / "capture_log.csv"
    log.write_text("timestamp\n"
                   "2026-11-16T09:00:00\n2026-11-16T09:02:00\n2026-11-16T09:05:00\n"
                   "2026-11-16T10:00:00\n"            # 55-minute break, ignored
                   "2026-11-16T10:01:00\n"
                   "2026-11-13T15:00:00\n2026-11-13T15:04:00\n", encoding="utf-8")
    ts = read_capture_log(log)
    assert len(ts) == 7
    t = compute_timing(ts, break_minutes=10)
    assert t.per_day[TODAY] == (120.0, 3)   # 120s, 180s, 60s
    assert t.per_day[dt.date(2026, 11, 13)] == (240.0, 1)
    assert t.overall_avg_seconds == 150.0


def test_timing_missing_file_is_fine(tmp_path):
    assert read_capture_log(tmp_path / "nope.csv") == []
    assert compute_timing([]) is None


def test_refresh_due():
    rows = [{"POC": "A", "Date": "15-11-2025"},    # 366 days old
            {"POC": "B", "Date": "16-11-2025"},    # exactly 365: not yet
            {"POC": "C", "Date": "01-01-2024"},
            {"POC": "D", "Date": ""}]
    due = refresh_due_rows(rows, TODAY)
    assert [r["POC"] for r in due] == ["C", "A"]
    assert due[1]["Sheet Row"] == 2 and due[1]["Days Old"] == 366


def test_verification_state():
    assert verification_state("") == "unchecked"
    assert verification_state("Yes") == "verified"
    assert verification_state("✓") == "verified"
    assert verification_state("bounced") == "failed"


def test_weekly_report(tmp_path, storage):
    storage.ensure_master()
    master = [
        {"POC": "A", "Date": "16-11-2026", "Missing Fields": "Email ID, Company Phone", "Verified": "yes"},
        {"POC": "B", "Date": "12-11-2026", "Missing Fields": "Email ID", "Verified": "no"},
        {"POC": "C", "Date": "11-11-2026", "Missing Fields": "", "Verified": ""},
        {"POC": "D", "Date": "01-11-2026", "Missing Fields": "", "Verified": "yes"},
        {"POC": "E", "Date": "01-01-2025", "Missing Fields": "", "Verified": ""},
    ]
    calls = ApiCallLog(tmp_path / "api_calls.csv")
    calls.log("enrich", 2, 2, "live")
    calls.log("enrich", 5, 5, "mock")   # mock calls never count
    stats = compute_weekly(master, today=TODAY, budget=2000, working_days=21, call_log=calls,
                           new_company_rows=[{"First Seen": "2026-11-14"}, {"First Seen": "2026-10-01"}],
                           capture_log_path=None)
    assert (stats.rows_week, stats.rows_month, stats.rows_total) == (3, 4, 5)
    assert round(stats.missing_pct_week) == 67
    assert stats.top_missing[0] == ("Email ID", 2)
    assert stats.verification_rate == 50
    assert stats.credits_week == 2 and stats.credits_month == 2
    assert stats.new_companies_week == 1
    assert stats.refresh_due == 1
    md = render_markdown(stats)
    assert "Verification rate: **50%**" in md and "approximation" in md

    path = write_weekly(storage, tmp_path / "reports", master, stats)
    assert path.name == "weekly-2026-W47.md" and path.exists()
    _, refresh = storage.read_table(REFRESH_TAB)
    assert [r["POC"] for r in refresh] == ["E"]


def test_notify_none_and_slack_guard(monkeypatch):
    assert notify.send_summary({"channel": "none"}, "s", "t") == "not configured"
    monkeypatch.delenv("ZI_SLACK_WEBHOOK_URL", raising=False)
    with pytest.raises(notify.NotifyError):
        notify.send_summary({"channel": "slack"}, "s", "t")


def test_notify_slack_posts_json(monkeypatch):
    sent = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        sent["url"], sent["body"] = req.full_url, req.data
        return Resp()

    monkeypatch.setenv("ZI_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.send_summary({"channel": "slack"}, "s", "hello") == "sent to Slack"
    assert sent["body"] == b'{"text": "hello"}'


def test_notify_email_uses_smtp(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout): calls.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context): calls.append(("tls",))
        def login(self, u, p): calls.append(("login", u))
        def send_message(self, msg): calls.append(("send", msg["To"]))

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("ZI_SMTP_USERNAME", "me")
    monkeypatch.setenv("ZI_SMTP_PASSWORD", "pw")
    cfg = {"channel": "email", "email": {"smtp_host": "smtp.example", "to_addr": "me@example.com"}}
    assert notify.send_summary(cfg, "s", "t") == "sent by email"
    assert calls == [("connect", "smtp.example", 587), ("tls",), ("login", "me"),
                     ("send", "me@example.com")]
