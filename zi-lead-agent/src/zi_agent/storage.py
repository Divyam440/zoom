"""Storage interface + Excel implementation.

The master sheet is the ACTIVE sheet of leads_master.xlsx, exactly as
zoominfo_capture.py reads and writes it. Agent tabs (queue, radar, reports)
go into a separate workbook by default so the agent never fights the capture
tool or an open Excel window over the master file.

To add Google Sheets later: implement the Storage methods in a new class
(see GoogleSheetsStorage) and pick it in cli._build_storage().
"""
from __future__ import annotations

import csv
import datetime as dt
from abc import ABC, abstractmethod
from pathlib import Path

from zi_agent.models import ZI_ID_HEADERS


class StorageLocked(RuntimeError):
    """The file is open in Excel (or otherwise locked)."""


class Storage(ABC):
    @abstractmethod
    def ensure_master(self) -> str:
        """Create the master if missing, or append missing ZoomInfo ID columns.
        Returns 'created', 'upgraded', 'ok' or 'locked'."""

    @abstractmethod
    def read_master(self) -> list[dict]: ...

    @abstractmethod
    def append_master(self, rows: list[dict]) -> None: ...

    @abstractmethod
    def read_pending(self) -> list[dict]:
        """Rows the capture tool parked while Excel was open."""

    @abstractmethod
    def read_table(self, name: str) -> tuple[list[str], list[dict]]: ...

    @abstractmethod
    def write_table(self, name: str, headers: list[str], rows: list[dict],
                    validations: dict[str, list[str]] | None = None) -> None: ...


# -- Excel helpers ---------------------------------------------------------
_HEADER_FILL = "1F4E78"  # same look as zoominfo_capture.py
_MISSING_FILL = "FFF2CC"


def _style_header(cells) -> None:
    from openpyxl.styles import Font, PatternFill
    for cell in cells:
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=_HEADER_FILL)


def _cell_out(value):
    """Write IDs/strings as text so Excel doesn't mangle long numbers."""
    if value is None:
        return ""
    if isinstance(value, (int, float, dt.date, dt.datetime)):
        return value
    return str(value)


def _rows_from_sheet(ws) -> tuple[list[str], list[dict]]:
    it = ws.iter_rows(values_only=True)
    header_row = next(it, None) or ()
    headers = [str(h).strip() if h is not None else "" for h in header_row]
    rows = []
    for values in it:
        if values is None or all(v in (None, "") for v in values):
            continue
        rows.append({h: (values[i] if i < len(values) else None)
                     for i, h in enumerate(headers) if h})
    return headers, rows


class ExcelStorage(Storage):
    def __init__(self, master_path: str | Path, workbook_path: str | Path,
                 master_headers: tuple[str, ...], pending_path: str | Path | None = None,
                 important_fields: tuple[str, ...] = (),
                 enrich_pending_path: str | Path | None = None):
        self.master_path = Path(master_path)
        self.workbook_path = Path(workbook_path)
        self.pending_path = Path(pending_path) if pending_path else None
        self.enrich_pending_path = Path(enrich_pending_path) if enrich_pending_path else None
        self.master_headers = tuple(master_headers)
        self.important_fields = tuple(important_fields)

    # -- master ------------------------------------------------------------
    def ensure_master(self, upgrade: bool = True) -> str:
        """Returns 'created', 'upgraded', 'ok', 'needs-upgrade' (upgrade=False) or 'locked'."""
        from openpyxl import Workbook, load_workbook
        try:
            if not self.master_path.exists():
                self.master_path.parent.mkdir(parents=True, exist_ok=True)
                wb = Workbook()
                ws = wb.active
                ws.title = "Leads"
                ws.append(list(self.master_headers))
                _style_header(ws[1])
                ws.freeze_panes = "A2"
                wb.save(self.master_path)
                return "created"
            if self.missing_id_columns() == []:
                return "ok"
            if not upgrade:
                return "needs-upgrade"
            wb = load_workbook(self.master_path)
            ws = wb.active
            header = [c.value for c in ws[1]]
            capture = [h for h in self.master_headers if h not in ZI_ID_HEADERS]
            # The capture tool writes its columns by POSITION, so fill any blank
            # header cells it owns and put the ID columns after all of them.
            for i, name in enumerate(capture, 1):
                if i > len(header) or header[i - 1] in (None, ""):
                    ws.cell(row=1, column=i, value=name)
            header = [c.value for c in ws[1]]
            last = max([i for i, h in enumerate(header, 1) if h not in (None, "")] + [len(capture)])
            missing = [h for h in ZI_ID_HEADERS if h not in header]
            for offset, h in enumerate(missing, 1):
                ws.cell(row=1, column=last + offset, value=h)
            _style_header([c for c in ws[1] if c.value not in (None, "")])
            wb.save(self.master_path)
            return "upgraded"
        except PermissionError:
            return "locked"

    def missing_id_columns(self) -> list[str]:
        if not self.master_path.exists():
            return list(ZI_ID_HEADERS)
        from openpyxl import load_workbook
        wb = load_workbook(self.master_path, read_only=True)
        try:
            header = next(wb.active.iter_rows(min_row=1, max_row=1, values_only=True), ()) or ()
        finally:
            wb.close()
        return [h for h in ZI_ID_HEADERS if h not in header]

    def master_writable(self) -> bool:
        """False when the master is open in Excel (Excel denies other writers)."""
        if not self.master_path.exists():
            return True
        try:
            with open(self.master_path, "r+b"):
                return True
        except PermissionError:
            return False

    def read_master(self) -> list[dict]:
        if not self.master_path.exists():
            return []
        from openpyxl import load_workbook
        wb = load_workbook(self.master_path, read_only=True, data_only=True)
        try:
            return _rows_from_sheet(wb.active)[1]
        finally:
            wb.close()

    def append_master(self, rows: list[dict]) -> None:
        from openpyxl import load_workbook
        from openpyxl.styles import Font, PatternFill
        if self.ensure_master() == "locked" or not self.master_writable():
            raise StorageLocked(f"{self.master_path.name} is open in Excel; close it and re-run.")
        try:
            wb = load_workbook(self.master_path)
            ws = wb.active
            header = [c.value for c in ws[1]]
            yellow = PatternFill("solid", fgColor=_MISSING_FILL)
            for row in rows:
                ws.append([_cell_out(row.get(h, "")) if h else "" for h in header])
                for cell in ws[ws.max_row]:
                    cell.font = Font(name="Arial")
                    if row.get("Missing Fields"):
                        cell.fill = yellow
            wb.save(self.master_path)
        except PermissionError as e:
            raise StorageLocked(f"{self.master_path.name} is open in Excel; close it and re-run.") from e

    def read_pending(self) -> list[dict]:
        """Capture tool's pending rows plus the agent's own enriched-but-unsaved rows."""
        rows: list[dict] = []
        for path in (self.pending_path, self.enrich_pending_path):
            if path and path.exists():
                with open(path, newline="", encoding="utf-8") as f:
                    rows.extend(csv.DictReader(f))
        return rows

    # -- enriched rows that could not be saved (credits already spent) ---------
    def save_enrich_pending(self, rows: list[dict]) -> Path:
        if not self.enrich_pending_path:
            raise StorageLocked("no enrich_pending_path configured")
        path = self.enrich_pending_path
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(self.master_headers), extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({h: r.get(h, "") for h in self.master_headers})
        return path

    def flush_enrich_pending(self) -> int:
        """Move saved-aside enriched rows into the master. Returns rows moved."""
        path = self.enrich_pending_path
        if not path or not path.exists():
            return 0
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows:
            self.append_master(rows)  # raises StorageLocked if Excel has it open
        path.unlink()
        return len(rows)

    # -- agent tabs ----------------------------------------------------------
    def read_table(self, name: str) -> tuple[list[str], list[dict]]:
        if not self.workbook_path.exists():
            return [], []
        from openpyxl import load_workbook
        # Not data_only: formulas the user typed (e.g. =HYPERLINK) are kept as formulas.
        wb = load_workbook(self.workbook_path, read_only=True)
        try:
            if name not in wb.sheetnames:
                return [], []
            return _rows_from_sheet(wb[name])
        finally:
            wb.close()

    def write_table(self, name: str, headers: list[str], rows: list[dict],
                    validations: dict[str, list[str]] | None = None) -> None:
        from openpyxl import Workbook, load_workbook
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation

        if self.workbook_path.exists():
            wb = load_workbook(self.workbook_path)
            active_idx = wb.index(wb.active)
        else:
            self.workbook_path.parent.mkdir(parents=True, exist_ok=True)
            wb = Workbook()
            active_idx = None
        if name in wb.sheetnames:
            pos = wb.sheetnames.index(name)
            del wb[name]
            ws = wb.create_sheet(name, pos)
        else:
            ws = wb.create_sheet(name)
        if active_idx is None and "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
            del wb["Sheet"]  # default empty sheet of a new workbook
        wb.active = active_idx if active_idx is not None else wb.sheetnames.index(name)

        ws.append(list(headers))
        _style_header(ws[1])
        for row in rows:
            ws.append([_cell_out(row.get(h)) for h in headers])
        ws.freeze_panes = "A2"
        for i, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(r.get(h) or "")) for r in rows[:200]])
            ws.column_dimensions[get_column_letter(i)].width = min(max(10, longest + 2), 60)
        for col_name, options in (validations or {}).items():
            if col_name in headers:
                col = get_column_letter(headers.index(col_name) + 1)
                dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=True)
                ws.add_data_validation(dv)
                dv.add(f"{col}2:{col}{max(len(rows) + 200, 500)}")
        try:
            wb.save(self.workbook_path)
        except PermissionError as e:
            raise StorageLocked(f"{self.workbook_path.name} is open in Excel; close it and re-run.") from e


class GoogleSheetsStorage(Storage):  # pragma: no cover - placeholder
    """Future implementation. Same interface: master = the leads worksheet,
    tables = other worksheets in an agent spreadsheet."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Google Sheets storage is not implemented yet.")

    ensure_master = read_master = append_master = read_pending = read_table = write_table = None
