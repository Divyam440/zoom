"""Command line: check-config, run-daily, radar-weekly, report-weekly.

Dry-run with the MockClient is the default. --live enables real API calls
(free search); enrichment additionally needs enrich_enabled: true and room in
CreditGuard.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

from zi_agent.calllog import ApiCallLog
from zi_agent.config import (NOTIFY_ENV_VARS, SECRET_ENV_VARS, Config, load_config, load_dotenv,
                             validate)
from zi_agent.models import load_capture_conventions
from zi_agent.storage import ExcelStorage, StorageLocked

log = logging.getLogger("zi_agent")


def _setup_logging(cfg: Config) -> None:
    logs = cfg.path("logs_dir")
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(logs / "agent.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("zi_agent")
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def _build_storage(cfg: Config, live: bool) -> ExcelStorage:
    conv = load_capture_conventions(cfg.path("capture_tool_path"))
    workbook = cfg.path("agent_workbook_path") if live else cfg.path("mock_workbook_path")
    return ExcelStorage(cfg.path("master_sheet_path"), workbook, conv.master_headers,
                        cfg.path("pending_rows_path"), conv.important_fields,
                        enrich_pending_path=cfg.path("enrich_pending_path"))


def _build_client(cfg: Config, live: bool, call_log: ApiCallLog):
    if live:
        from zi_agent.client import ZoomInfoClient
        zi = cfg.raw["zoominfo"]
        return ZoomInfoClient(call_log=call_log,
                              max_requests_per_second=float(cfg.caps["max_requests_per_second"]),
                              usage_limit_type=str(zi.get("usage_limit_type") or "record"),
                              enrich_output_fields=zi.get("enrich_output_fields") or None,
                              contact_search_attributes=zi.get("contact_search_attributes") or {},
                              company_search_attributes=zi.get("company_search_attributes") or {})
    from zi_agent.client import MockClient
    fixtures = cfg.raw["paths"].get("mock_fixtures_path") or None
    return MockClient(cfg.path("mock_fixtures_path") if fixtures else None, call_log=call_log)


# -- commands ------------------------------------------------------------
def cmd_check_config(cfg: Config, args) -> int:
    errors, warnings = validate(cfg)
    conv = load_capture_conventions(cfg.path("capture_tool_path"))
    print(f"Config file      : {cfg.source or '(none found - using defaults)'}")
    print(f"Master sheet     : {cfg.path('master_sheet_path')} "
          f"({'exists' if cfg.path('master_sheet_path').exists() else 'missing - will be created'})")
    print(f"Agent workbook   : {cfg.path('agent_workbook_path')} (live)")
    print(f"Mock workbook    : {cfg.path('mock_workbook_path')} (dry-run)")
    print(f"Capture headers  : {len(conv.headers)} columns from {conv.source}")
    print(f"Master headers   : {len(conv.master_headers)} columns (+ ZoomInfo Contact ID, ZoomInfo Company ID)")
    print(f"Title priorities : {len(cfg.title_priority)} groups")
    print(f"Blocklist        : {len(cfg.blocked_companies)} companies, {len(cfg.blocked_domains)} domains")
    caps = cfg.caps
    print(f"Caps             : per run {caps['max_enrich_per_run']}, per day {caps['max_enrich_per_day']}, "
          f"reserve {caps['reserve']}, top N {caps['enrich_top_n']}, {caps['max_requests_per_second']} req/s")
    print(f"enrich_enabled   : {cfg.enrich_enabled}")
    env_file = cfg.path("env_file")
    print(f".env file        : {'found' if env_file.exists() else 'not found'}")
    print(f"Daily summary    : {cfg.raw['notify'].get('channel', 'none')}")
    for name in SECRET_ENV_VARS + NOTIFY_ENV_VARS:  # presence only, never values
        print(f"  {name:<22}: {'set' if os.environ.get(name) else 'not set'}")
    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    print("Config OK" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


def cmd_run_daily(cfg: Config, args) -> int:
    from zi_agent.daily import run_daily, save_queue
    from zi_agent.enrich import run_enrichment
    from zi_agent.guard import CreditGuard

    errors, _ = validate(cfg)
    if errors:
        print("Config has errors; run check-config.")
        return 1
    live = bool(args.live)
    today = dt.date.today()
    call_log = ApiCallLog(cfg.path("logs_dir") / "api_calls.csv")
    storage = _build_storage(cfg, live)
    conv = load_capture_conventions(cfg.path("capture_tool_path"))

    # Dry-run never rewrites an existing master; live runs add the ID columns.
    master_state = storage.ensure_master(upgrade=live)
    from zi_agent.blocklist import Blocklist
    from zi_agent.client import ZoomInfoApiError
    try:
        client = _build_client(cfg, live, call_log)
    except RuntimeError as e:  # missing credentials etc.
        print(f"Cannot run live: {e}")
        return 2

    mode = "LIVE" if live else "DRY-RUN (mock data, no real API calls)"
    print(f"== zi-lead-agent run-daily | {mode} | {today.isoformat()} ==")
    if master_state == "created":
        print(f"Created empty master sheet: {storage.master_path}")
    elif master_state == "upgraded":
        print("Added ZoomInfo Contact ID / Company ID columns to the master sheet.")
    elif master_state == "needs-upgrade":
        print("Note: the ZoomInfo ID columns will be added to the master sheet on the first live run.")
    elif master_state == "locked":
        print("WARNING: master sheet is open in Excel; could not check its headers.")

    if live:  # enriched rows saved aside on an earlier run (master was open)
        try:
            moved = storage.flush_enrich_pending()
            if moved:
                print(f"Added {moved} saved-aside enriched row(s) to the master sheet.")
        except StorageLocked:
            print("WARNING: saved-aside enriched rows are still waiting; close the master sheet.")

    try:
        res = run_daily(cfg, client, storage, today)
    except ZoomInfoApiError as e:
        print(f"ERROR from ZoomInfo API: {e}")
        return 4

    # Optional enrichment, gated by CreditGuard.
    enrich_line = "skipped (dry-run)"
    if live:
        try:
            remaining = client.get_usage().remaining_credits if cfg.enrich_enabled else None
            first = not call_log.has_live_enrich()
            guard = CreditGuard.from_config(cfg.caps, live=live, enrich_enabled=cfg.enrich_enabled,
                                            used_today=call_log.credits_on(today, "live"),
                                            remaining_credits=remaining, first_live_run=first)
            room, why = guard.budget()
            if room <= 0:
                enrich_line = f"skipped: {why}"
            else:
                er = run_enrichment(client=client, guard=guard, storage=storage, conventions=conv,
                                    queue_rows=res.queue_rows, top_n=int(cfg.caps["enrich_top_n"]),
                                    master=res.master_index, today=today,
                                    blocklist=Blocklist(cfg.blocked_companies, cfg.blocked_domains))
                enrich_line = (f"{er.status}: {len(er.appended_ids)} added to master, "
                               f"{len(er.duplicate_ids)} already there, {len(er.blocked_ids)} blocklisted, "
                               f"{len(er.mismatch_ids)} email/domain mismatch, ~{er.credits_used} credit(s)"
                               + (" [first live run: capped at 1]" if first else ""))
        except StorageLocked as e:
            enrich_line = f"FAILED: {e}"
        except ZoomInfoApiError as e:
            enrich_line = f"FAILED (API): {e}"

    try:
        save_queue(storage, res)
    except StorageLocked as e:
        print(f"ERROR: {e}")
        return 3

    print(f"Search results           : {res.searched}")
    print(f"Blocklisted              : {len(res.blocklisted)}")
    print(f"Duplicates removed       : {len(res.duplicates)}")
    reasons: dict[str, int] = {}
    for _, r in res.duplicates:
        reasons[r] = reasons.get(r, 0) + 1
    for r, n in sorted(reasons.items()):
        print(f"    - {r}: {n}")
    print(f"Company already handled  : {len(res.company_already_handled)} contact(s)")
    print(f"Companies w/o allowed title: {len(res.companies_no_title)}")
    print(f"Added to queue           : {res.added}")
    print(f"Marked Done (now in master): {res.marked_done}")
    print(f"Auto-skipped (company blocklisted / already in master): {res.auto_skipped}")
    print(f"Open queue rows          : {res.open_rows} (of {res.total_rows})")
    print(f"Enrichment               : {enrich_line}")
    print(f"Queue written to         : {storage.workbook_path} -> tab 'To-View Queue'")

    # Pacing (from the real master sheet, even in dry-run).
    from zi_agent.pacing import compute_pacing
    pacing = compute_pacing(storage.read_master(), today, cfg.monthly_view_budget,
                            cfg.working_days_per_month)
    print("-- Credit pacing --")
    for line in pacing.lines():
        print(line)
    days_of_work = res.open_rows / pacing.daily_target if pacing.daily_target else 0
    print(f"Queue covers             : ~{days_of_work:.1f} day(s) of views at {pacing.daily_target:.0f}/day")
    if res.open_rows < pacing.target_today:
        print("WARNING: fewer open queue rows than today's target. Widen search.filters "
              "(more titles/countries/industries) or raise search.max_results.")

    # Daily summary to the owner: counts only, no contact data.
    from zi_agent.notify import NotifyError, build_summary, send_summary
    tag = "" if live else "[DRY-RUN, mock data] "
    summary = build_summary(
        title=f"{tag}zi-lead-agent daily run {today.isoformat()}",
        lines=[f"{res.added} new contacts queued; {res.open_rows} open in To-View Queue",
               f"{len(res.duplicates)} duplicates and {len(res.blocklisted)} blocklisted removed",
               f"Enrichment: {enrich_line}",
               f"Views this month (approx.): {pacing.done_month}/{pacing.budget}; "
               f"target today {pacing.target_today}; projected {pacing.projected_month_end}"])
    if live or args.notify:
        try:
            status = send_summary(cfg.raw["notify"], f"{tag}Lead agent daily summary", summary)
        except NotifyError as e:
            status = f"FAILED ({e})"
        print(f"Daily summary            : {status}")
    log.info("daily summary built (sent=%s)", live or args.notify)
    return 0


def cmd_report_weekly(cfg: Config, args) -> int:
    from zi_agent.report import compute_weekly, new_company_rows, write_weekly
    today = dt.date.today()
    # Reads the real master + logs; no API calls. Tabs go to the live agent workbook.
    storage = _build_storage(cfg, live=True)
    master = storage.read_master()
    capture_log = cfg.path("capture_log_path")
    stats = compute_weekly(
        master, today=today, budget=cfg.monthly_view_budget,
        working_days=cfg.working_days_per_month,
        call_log=ApiCallLog(cfg.path("logs_dir") / "api_calls.csv"),
        new_company_rows=new_company_rows(storage),
        capture_log_path=capture_log if capture_log.exists() else None,
        break_minutes=float(cfg.raw["timing"].get("break_minutes", 10)))
    try:
        path = write_weekly(storage, cfg.path("reports_dir"), master, stats)
    except StorageLocked as e:
        print(f"ERROR: {e}")
        return 3
    print(f"Weekly report written : {path}")
    print(f"Refresh Due tab       : {storage.workbook_path} ({stats.refresh_due} rows)")
    print(f"Views this month (approx.): {stats.pacing.done_month}/{stats.pacing.budget}, "
          f"rows this week {stats.rows_week}, verification rate "
          f"{'n/a' if stats.verification_rate is None else f'{stats.verification_rate:.0f}%'}")
    return 0


def cmd_radar_weekly(cfg: Config, args) -> int:
    from zi_agent.radar import run_radar
    live = bool(args.live)
    today = dt.date.today()
    call_log = ApiCallLog(cfg.path("logs_dir") / "api_calls.csv")
    storage = _build_storage(cfg, live)
    from zi_agent.client import ZoomInfoApiError
    try:
        client = _build_client(cfg, live, call_log)
    except RuntimeError as e:
        print(f"Cannot run live: {e}")
        return 2
    radar = cfg.raw["radar"]
    try:
        res = run_radar(client, storage, radar.get("filters") or {},
                        int(radar.get("max_results", 100)), today)
    except StorageLocked as e:
        print(f"ERROR: {e}")
        return 3
    except ZoomInfoApiError as e:
        print(f"ERROR from ZoomInfo API: {e}")
        return 4
    mode = "LIVE" if live else "DRY-RUN (mock data)"
    print(f"== zi-lead-agent radar-weekly | {mode} | {today.isoformat()} ==")
    for name, n in res.queries.items():
        print(f"{'Search ' + repr(name):<24}: {n} companies")
    print(f"Unique companies found  : {res.found}")
    print(f"Already seen            : {res.already_seen}")
    print(f"New this week           : {len(res.new)}")
    print(f"Written to              : {storage.workbook_path} -> tabs 'New Companies', 'Companies seen'")
    print(f"New companies get +{cfg.new_company_boost} in the daily queue for "
          f"{radar.get('boost_days', 30)} days.")
    log.info("radar: found=%d new=%s", res.found, [c.company_id for c in res.new])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zi-agent", description=__doc__.splitlines()[0])
    p.add_argument("--config", default=None, help="path to config.yaml (default: ./config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="validate config and show resolved paths")
    d = sub.add_parser("run-daily", help="build the To-View Queue")
    d.add_argument("--live", action="store_true", help="use the real ZoomInfo API")
    d.add_argument("--notify", action="store_true",
                   help="send the daily summary even in dry-run (to test Slack/email)")
    r = sub.add_parser("radar-weekly", help="New Company Radar")
    r.add_argument("--live", action="store_true")
    sub.add_parser("report-weekly", help="weekly markdown report + Refresh Due tab")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config) if args.config else Path(__file__).resolve().parents[2] / "config.yaml"
    cfg = load_config(cfg_path)
    load_dotenv(cfg.path("env_file"))
    _setup_logging(cfg)
    commands = {
        "check-config": cmd_check_config,
        "run-daily": cmd_run_daily,
        "radar-weekly": cmd_radar_weekly,
        "report-weekly": cmd_report_weekly,
    }
    log.info("command=%s live=%s", args.command, getattr(args, "live", False))
    return commands[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
