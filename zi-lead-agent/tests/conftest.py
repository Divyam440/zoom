import shutil
from pathlib import Path

import pytest

from zi_agent.config import load_config
from zi_agent.models import Contact, load_capture_conventions
from zi_agent.storage import ExcelStorage

PROJECT = Path(__file__).resolve().parents[1]
CAPTURE_TOOL = PROJECT.parent / "zoominfo_capture.py"


def make_contact(cid="1", first="Ann", last="Lee", title="CEO", company_id="c1",
                 company="Acme", **kw) -> Contact:
    defaults = dict(website="acme.example", employee_count=200, industry="Software",
                    country="United States", accuracy_score=80, has_email=True,
                    has_direct_phone=True, has_mobile_phone=False, has_company_phone=True)
    defaults.update(kw)
    return Contact(contact_id=cid, first_name=first, last_name=last, job_title=title,
                   company_id=company_id, company_name=company, **defaults)


@pytest.fixture(autouse=True)
def _unlock_bulk_credits_for_logic_tests(request, monkeypatch):
    """The production lock is ON. Older tests exercise CreditGuard/enrich logic
    against fakes, so they run with the lock off. test_bulk_lock.py keeps it on."""
    if request.module.__name__.endswith("test_bulk_lock"):
        return
    from zi_agent import guard
    monkeypatch.setattr(guard, "BULK_CREDITS_LOCKED", False)


@pytest.fixture
def cfg(tmp_path):
    """Example config with all paths redirected into tmp_path."""
    src = PROJECT / "config.example.yaml"
    dst = tmp_path / "config.yaml"
    shutil.copy(src, dst)
    c = load_config(dst)
    c.raw["paths"].update({
        "master_sheet_path": str(tmp_path / "leads_master.xlsx"),
        "pending_rows_path": str(tmp_path / "pending_rows.csv"),
        "capture_tool_path": str(CAPTURE_TOOL),
        "agent_workbook_path": str(tmp_path / "lead_agent.xlsx"),
        "mock_workbook_path": str(tmp_path / "lead_agent.mock.xlsx"),
        "enrich_pending_path": str(tmp_path / "enriched_pending.csv"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
        "capture_log_path": str(tmp_path / "capture_log.csv"),
        "env_file": str(tmp_path / ".env"),
    })
    import yaml
    dst.write_text(yaml.safe_dump(c.raw), encoding="utf-8")
    return c


@pytest.fixture
def conventions():
    return load_capture_conventions(CAPTURE_TOOL)


@pytest.fixture
def storage(cfg, conventions):
    return ExcelStorage(cfg.path("master_sheet_path"), cfg.path("mock_workbook_path"),
                        conventions.master_headers, cfg.path("pending_rows_path"),
                        conventions.important_fields,
                        enrich_pending_path=cfg.path("enrich_pending_path"))
