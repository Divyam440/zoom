"""
ZoomInfo Profile Capture Helper
================================

What it does
------------
You open a contact profile in ZoomInfo yourself, then press ONE hotkey
(default: Ctrl+Alt+Z). The tool presses Ctrl+A, Ctrl+C for you, reads the copied
text from the clipboard, pulls out the fields, and appends one clean row to
leads_master.xlsx. A small popup tells you the result.
(Set AUTO_COPY = False to copy yourself with Ctrl+A, Ctrl+C instead.)

What it does NOT do
-------------------
It never opens, clicks, reads or requests anything on ZoomInfo by itself.
Every capture starts with a person opening a profile AND pressing the hotkey.
Keep it that way: do not add auto-triggering on page load.
No ZoomInfo API call and no bulk credit is ever used by this tool.

Windows only (uses the Windows clipboard and Windows global hotkeys; no extra packages needed for hotkeys).
"""

import csv
import datetime as dt
import os
import re
import sys
import time
import unicodedata
from urllib.parse import unquote

# ----------------------------------------------------------------------
# CONFIG - change these if needed
# ----------------------------------------------------------------------
HOTKEY = "ctrl+alt+z"              # capture a profile
PRECHECK_HOTKEY = "ctrl+alt+x"     # check a search-results page for duplicates
LINKEDIN_HOTKEY = "ctrl+alt+l"     # on the person's LinkedIn tab: save its URL into their row
AUTO_COPY = True                   # hotkey presses Ctrl+A, Ctrl+C itself (False = you copy first)
COPY_TIMEOUT = 2.0                 # seconds to wait for the copy to land on the clipboard
POPUP = True                       # small on-screen popup instead of watching the console
# All files live next to this script, NOT in whatever folder the tool was started from.
# (Starting it from another folder after a restart used to create/read a different leads_master.xlsx.)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE_DIR, "leads_master.xlsx")
PENDING_FILE = os.path.join(BASE_DIR, "pending_rows.csv")      # used when the Excel file is open/locked
JOURNAL_FILE = os.path.join(BASE_DIR, "captured_log.csv")      # every captured row, append-only, never deleted
BACKUP_DIR = os.path.join(BASE_DIR, "backups")                 # timestamped copies of leads_master.xlsx
BACKUPS_KEEP = 50
BACKUP_EVERY_MIN = 5                                           # at most one backup per 5 minutes
LOG_FILE = os.path.join(BASE_DIR, "capture_errors.log")        # full details of any error
DEBUG_DIR = os.path.join(BASE_DIR, "debug_captures")           # raw copies saved when fields are missing
SAVE_DEBUG_ON_MISSING = True

# Your 11 headers first (exact order), then helpful extras
HEADERS = [
    "Company Name", "Employees", "Industry", "Website", "Company Linkedin Url",
    "POC", "Job title", "Email ID", "Company Country", "Company Phone", "Date",
    "Mobile Phone", "POC Linkedin Url", "Missing Fields",
    "Call Status", "Verified", "Notes",
]
# Sheet layout
SHEET_NAME = "Leads"
FONT_NAME = "Arial"
HEADER_FILL = "1F4E78"                 # dark blue header, white bold text
MISSING_FILL = "FFF2CC"                # light yellow = row has missing fields
MISSING_BGR = 255 + 242 * 256 + 204 * 65536   # the same yellow as Excel (COM) wants it
COLUMN_WIDTHS = {
    "Company Name": 30, "Employees": 13, "Industry": 24, "Website": 26, "Company Linkedin Url": 36,
    "POC": 24, "Job title": 32, "Email ID": 32, "Company Country": 20, "Company Phone": 19,
    "Date": 12, "Mobile Phone": 19, "POC Linkedin Url": 36, "Missing Fields": 36,
    "Call Status": 22, "Verified": 10, "Notes": 40,
}
LINK_COLUMNS = {"Website", "Company Linkedin Url", "POC Linkedin Url"}   # + Email ID as mailto:
CALL_STATUS_CHOICES = ["Not called", "No answer", "Call back", "Interested", "Not interested",
                       "Meeting booked", "Wrong number"]
VERIFIED_CHOICES = ["Yes", "No"]
VALIDATION_ROWS = 20000                # dropdowns cover this many rows

# Fields that, if empty, get the row highlighted and listed in "Missing Fields"
IMPORTANT_FIELDS = [
    "Company Name", "Employees", "Industry", "Website", "Company Linkedin Url",
    "POC", "Job title", "Email ID", "Company Country", "Company Phone",
    "POC Linkedin Url",
]

# ----------------------------------------------------------------------
# PARSING (pure functions, no Windows dependency, easy to test)
# ----------------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(\+?\(?\d[\d\s().\-]{6,}\d)")
PHONE_TAG_RE = re.compile(r"\((HQ|M|D|B|O)\)", re.I)
DOMAIN_RE = re.compile(r"^(https?://)?(www\.)?[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+(/\S*)?$")
EMPLOYEES_RE = re.compile(r"\d[\d,]*\s*(-|–|to)?\s*[\d,]*\+?")
LI_PERSON_RE = re.compile(r"linkedin\.com/in/[^\s\"'&?#<>]+", re.I)
LI_COMPANY_RE = re.compile(r"linkedin\.com/company/[^\s\"'&?#<>]+", re.I)
LI_COMPANY_ADDRESS_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)?linkedin\.com/(?:company|school)/([^/?#\s\"'<>]+)", re.I)
LI_ADDRESS_RE = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)?linkedin\.com/in/([^/?#\s\"'<>]+)", re.I)

SESSION_KEYS = set()   # (poc, company) captured this session, for duplicate checks
LAST_CAPTURE = {}      # the person captured last: key, poc, company

# Words that are UI labels, never real values
UI_LABELS = {
    "contact search", "/", "suggest update", "tag", "track contact", "export",
    "contact profile", "overview", "org chart", "employees", "tabs", "...",
    "contact details", "emails", "phone numbers", "tags", "add tag", "about",
    "show more", "location", "local", "hq", "similar contacts", "website",
    "website:", "industry", "revenue", "employment history", "education",
    "activity feed", "web references", "navigation", "home", "advanced search",
    "signals", "lists and records", "automations", "alerts", "sales",
}


def _clean_lines(text):
    lines = [ln.strip() for ln in (text or "").replace("\r", "\n").split("\n")]
    return [ln for ln in lines if ln]


def _is_label(line):
    return line.strip().lower().rstrip(":") in UI_LABELS or line.strip().lower() in UI_LABELS


def _value_after(lines, label, validator=None):
    """Find `label` and return the value on the same line or the next line.
    Tries every occurrence and returns the first one that passes `validator`."""
    lab = label.lower()
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low == lab or low == lab + ":":
            candidates = lines[i + 1:i + 3]
        elif low.startswith(lab + ":") or low.startswith(lab + " "):
            candidates = [ln[len(label):].lstrip(": ").strip()] + lines[i + 1:i + 2]
        else:
            continue
        for cand in candidates:
            if not cand or _is_label(cand):
                continue
            if validator is None or validator(cand):
                return cand
            break
    return ""


def _looks_like_domain(s):
    s = s.strip()
    return bool(DOMAIN_RE.match(s)) and "@" not in s and " " not in s


def _looks_like_employees(s):
    return bool(EMPLOYEES_RE.fullmatch(s.strip()))


def _not_label(s):
    return not _is_label(s)


def parse_name(lines):
    """Name comes right after the 'Contact Search' breadcrumb."""
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low.startswith("contact search"):
            rest = ln[len("contact search"):].strip(" /")
            if rest and not _is_label(rest):
                return rest, i
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip("/ ") and not _is_label(nxt):
                    return nxt.strip("/ ").strip(), i
    return "", -1


def parse_title(lines, name, start_idx):
    """Title is the line right after the second appearance of the name."""
    if not name:
        return ""
    seen = 0
    for i in range(max(start_idx, 0), len(lines)):
        if lines[i] == name:
            seen += 1
            if seen >= 2 and i + 1 < len(lines):
                title = lines[i + 1]
                if _is_label(title) or EMAIL_RE.search(title):
                    return ""
                # "Chief Executive Officer At XYZ" -> "Chief Executive Officer"
                return re.split(r"\s+at\s+", title, maxsplit=1, flags=re.I)[0].strip()
    return ""


def parse_company(lines, name, title):
    """Company name is the last real line before the 'Website' label."""
    for i, ln in enumerate(lines):
        if ln.lower().startswith("website"):
            for j in range(i - 1, max(i - 6, -1), -1):
                cand = lines[j]
                if _is_label(cand) or cand == name:
                    continue
                if title and cand.lower().startswith(title.lower()):
                    continue
                if EMAIL_RE.search(cand) or PHONE_RE.fullmatch(cand):
                    continue
                return cand
            break
    return ""


def _section(lines, start_label, stop_labels):
    """Lines between start_label and the first of stop_labels."""
    out, inside = [], False
    stops = [s.lower() for s in stop_labels]
    for ln in lines:
        low = ln.lower().rstrip(":")
        if not inside and low == start_label.lower():
            inside = True
            continue
        if inside:
            if any(low.startswith(s) for s in stops):
                break
            out.append(ln)
    return out


def parse_email(lines, website):
    sec = _section(lines, "Emails", ["Phone numbers", "Location", "Tags"])
    pool = EMAIL_RE.findall("\n".join(sec)) or EMAIL_RE.findall("\n".join(lines))
    pool = [e for e in pool if "zoominfo" not in e.lower()]
    if not pool:
        return ""
    # Prefer an email on the company's own domain
    dom = re.sub(r"^(https?://)?(www\.)?", "", website or "").split("/")[0].lower()
    if dom:
        for e in pool:
            if e.lower().endswith("@" + dom) or e.lower().endswith("." + dom):
                return e
    return pool[0]


def parse_phones(lines):
    """Returns (company_phone, mobile_phone, direct_phone).
    Company phone = the (HQ) number only. A direct/business/other line (D, B, O) belongs to the
    PERSON, so it is returned separately and never placed in Company Phone. An untagged number
    is used for Company Phone only when the profile has no tagged numbers at all."""
    sec = _section(lines, "Phone numbers", ["Location", "Tags", "About", "Similar Contacts"])
    hq, mobile, other, direct = "", "", "", ""
    for i, ln in enumerate(sec):
        m = PHONE_RE.search(ln)
        if not m:
            continue
        phone = m.group(1).strip()
        tag_m = PHONE_TAG_RE.search(ln[m.end():]) or (
            PHONE_TAG_RE.fullmatch(sec[i + 1].strip()) if i + 1 < len(sec) else None)
        tag = tag_m.group(1).upper() if tag_m else ""
        # clean a trailing "(" that the regex may grab before a tag
        phone = phone.rstrip(" (")
        if tag == "HQ" and not hq:
            hq = phone
        elif tag == "M" and not mobile:
            mobile = phone
        elif tag in ("D", "B", "O"):
            direct = direct or phone
        elif not tag and not other:
            other = phone
    return hq or other, mobile, direct


def parse_country(lines):
    """Country = last comma part of the HQ address (falls back to Local)."""
    sec = _section(lines, "Location", [
        "Current Role", "Current Company", "Notice Provided", "Employment History",
        "Education", "Web References", "Similar Contacts", "Tags", "About"])

    def address_for(label):
        for i, ln in enumerate(sec):
            if ln.upper() == label:
                parts = []
                for nxt in sec[i + 1:]:
                    if nxt.upper() in ("HQ", "LOCAL"):
                        break
                    parts.append(nxt)
                return " ".join(parts)
            if ln.upper().startswith(label + " "):
                return ln[len(label):].strip()
        return ""

    addr = address_for("HQ") or address_for("LOCAL")
    if not addr:
        return ""
    last = addr.split(",")[-1].strip()
    return re.sub(r"\d", "", last).strip(" -") or last


def _fold(s):
    """Lower-case, accents removed, letters/digits only, split into words."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.findall(r"[a-z0-9]+", s)


def linkedin_matches_name(name, text):
    """True if any word (3+ letters) of `name` appears as a word in `text`
    (a LinkedIn URL or a browser tab title). Names we cannot compare (e.g. Arabic
    script) return True, because there is nothing to check against."""
    tokens = [t for t in _fold(name) if len(t) >= 3 and not t.isdigit()]
    if not tokens:
        return True
    words = set(_fold(text))
    return any(t in words for t in tokens)


def normalize_linkedin_url(raw):
    """Any text containing a linkedin.com/in/... link -> clean profile URL, else ''."""
    m = LI_ADDRESS_RE.search((raw or "").strip())
    return "https://www.linkedin.com/in/" + m.group(1) if m else ""


def normalize_linkedin_company_url(raw):
    """Any text containing a linkedin.com/company/... link -> clean company URL, else ''."""
    m = LI_COMPANY_ADDRESS_RE.search((raw or "").strip())
    return "https://www.linkedin.com/company/" + m.group(1) if m else ""


def parse_linkedin(html, text, name=""):
    """Returns (person_url, company_url). With `name`, the person URL must contain
    one of the person's name words, so a Similar Contacts link can never be taken
    for the profile owner. An empty person URL is then filled in later with the
    LinkedIn hotkey."""
    blob = unquote(html or "") + "\n" + (text or "")
    person = None
    for m in LI_PERSON_RE.finditer(blob):
        if not name or linkedin_matches_name(name, m.group(0)):
            person = m
            break
    company = LI_COMPANY_RE.search(blob)
    def norm(m):
        if not m:
            return ""
        path = m.group(0).split("linkedin.com", 1)[1].rstrip("/")
        return "https://www.linkedin.com" + path
    return norm(person), norm(company)


def parse_profile(text, html=""):
    lines = _clean_lines(text)
    name, idx = parse_name(lines)
    title = parse_title(lines, name, idx)
    company = parse_company(lines, name, title)
    website = _value_after(lines, "Website", _looks_like_domain)
    industry = _value_after(lines, "Industry", _not_label)
    employees = _value_after(lines, "Employees", _looks_like_employees)
    email = parse_email(lines, website)
    hq_phone, mobile, direct = parse_phones(lines)
    country = parse_country(lines)
    li_person, li_company = parse_linkedin(html, text, name)

    row = {
        "Company Name": company,
        "Employees": employees,
        "Industry": industry,
        "Website": website,
        "Company Linkedin Url": li_company,
        "POC": name,
        "Job title": title,
        "Email ID": email,
        "Company Country": country,
        "Company Phone": hq_phone,
        "Date": dt.date.today().strftime("%d-%m-%Y"),
        "Mobile Phone": mobile,
        "POC Linkedin Url": li_person,
        "Call Status": "Not called",
        "Verified": "",
        # The person's own line, kept for you but never mixed into Company Phone
        "Notes": f"Direct phone: {direct}" if direct and not hq_phone else "",
    }
    missing = [f for f in IMPORTANT_FIELDS if not row.get(f)]
    row["Missing Fields"] = ", ".join(missing)
    return row


# ----------------------------------------------------------------------
# EXCEL OUTPUT
# ----------------------------------------------------------------------
def _dedupe_key(row):
    return (str(row.get("POC") or "").strip().lower(), str(row.get("Company Name") or "").strip().lower())


def _unique_last(rows):
    """Drop repeated people (same POC + Company), keeping the LAST version of each (it has the
    latest LinkedIn URLs etc.) but in first-seen order."""
    latest, order = {}, []
    for r in rows:
        k = _dedupe_key(r)
        if k not in latest:
            order.append(k)
        latest[k] = r
    return [latest[k] for k in order]


def log_error(context):
    """Append the full error (with traceback) to capture_errors.log. Never raises."""
    try:
        import traceback
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {context}\n{traceback.format_exc()}")
    except Exception:
        pass


_CANON = {h.lower(): h for h in HEADERS}


def _canon(h):
    """A header cell as one of HEADERS (case/space-insensitive), else its own text, else ''."""
    s = str(h or "").strip()
    return _CANON.get(" ".join(s.lower().split()), s)


def _cell_str(v):
    return "" if v is None else str(v).strip()


def _link_target(h, v):
    """Address a cell should link to: websites/LinkedIn open in the browser, emails open mail."""
    v = _cell_str(v)
    if not v:
        return ""
    if h == "Email ID":
        return f"mailto:{v}" if EMAIL_RE.fullmatch(v) else ""
    if h not in LINK_COLUMNS:
        return ""
    if v.lower().startswith(("http://", "https://")):
        return v
    return "https://" + v if DOMAIN_RE.match(v) else ""


# ---------- the sheet file on disk (openpyxl; used while Excel does not have it open) ----------
def _is_locked(path):
    """True if another program (normally Excel) has the file open, so it cannot be written."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


def _leads_ws(wb):
    """The 'Leads' sheet, else the first sheet. Never 'whichever tab happened to be active'."""
    return wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.worksheets[0]


def _header_map(ws):
    """{header: column number} from row 1. Columns may be in any order; headers of ours that
    are missing are added at the end, so data never lands under the wrong heading."""
    col = {}
    for c in ws[1]:
        h = _canon(c.value)
        if h:
            col.setdefault(h, c.column)
    nxt = max(col.values(), default=0) + 1
    for h in HEADERS:
        if h not in col:
            ws.cell(1, nxt).value = h
            col[h] = nxt
            nxt += 1
    return col


def _last_data_row(ws, ncols):
    """Last row with any value (formatting-only rows at the bottom do not count)."""
    r = ws.max_row
    while r > 1 and all(ws.cell(r, c).value in (None, "") for c in range(1, ncols + 1)):
        r -= 1
    return r


def _set_value(cell, h, v):
    """Write one value the way the whole sheet looks: Arial, text format for text (so '+971...'
    and '=...' stay text), blue clickable link for websites / LinkedIn / email."""
    from openpyxl.styles import Font
    if isinstance(v, str):
        v = v.strip()
    cell.value = v if v not in ("", None) else None
    if isinstance(cell.value, str):
        cell.data_type = "s"        # '=x' must never become a formula
        cell.number_format = "@"
    target = _link_target(h, v)
    cell.hyperlink = target or None
    cell.font = Font(name=FONT_NAME, color="0563C1", underline="single") if target else Font(name=FONT_NAME)


def _fill_row(ws, r, col, missing):
    from openpyxl.styles import PatternFill
    fill = PatternFill("solid", fgColor=MISSING_FILL) if missing else PatternFill(fill_type=None)
    for c in col.values():
        ws.cell(r, c).fill = fill


def _put_row(ws, r, row, col):
    for h in HEADERS:
        _set_value(ws.cell(r, col[h]), h, row.get(h, ""))
    _fill_row(ws, r, col, bool(_cell_str(row.get("Missing Fields"))))


def _finish_sheet(ws, col, last):
    """Header style, frozen header row, filter buttons, dropdowns. Safe to run on every save."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    for h, c in col.items():
        cell = ws.cell(1, c)
        cell.font = Font(name=FONT_NAME, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = Alignment(vertical="center")
        dim = ws.column_dimensions[get_column_letter(c)]
        if h in COLUMN_WIDTHS and not dim.customWidth:   # never undo a width you set yourself
            dim.width = COLUMN_WIDTHS[h]
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "B2"          # header row and company name stay visible while scrolling
    ws.auto_filter.ref = f"A1:{get_column_letter(max(col.values()))}{max(last, 2)}"
    have = {dv.promptTitle for dv in ws.data_validations.dataValidation}
    for h, choices in (("Call Status", CALL_STATUS_CHOICES), ("Verified", VERIFIED_CHOICES)):
        if h in col and h not in have:
            letter = get_column_letter(col[h])
            dv = DataValidation(type="list", formula1='"' + ",".join(choices) + '"', allow_blank=True,
                                showErrorMessage=False, promptTitle=h)   # dropdown, but any text allowed
            dv.add(f"{letter}2:{letter}{VALIDATION_ROWS}")
            ws.add_data_validation(dv)


def _save_atomic(wb, path):
    """Save to a temp file, then swap it in, so a crash mid-save can never leave a broken sheet."""
    tmp = os.path.join(os.path.dirname(path), "~saving_" + os.path.basename(path))
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _latest_good_backup():
    from openpyxl import load_workbook
    try:
        names = sorted(n for n in os.listdir(BACKUP_DIR) if n.lower().endswith(".xlsx"))
    except OSError:
        return ""
    for n in reversed(names):
        p = os.path.join(BACKUP_DIR, n)
        try:
            load_workbook(p, read_only=True).close()
            return p
        except Exception:
            continue
    return ""


def _set_aside_damaged(path):
    """The sheet cannot be opened (crash / power cut while saving). Keep it as
    leads_master.DAMAGED_<time>.xlsx and put the newest good backup in its place."""
    import shutil
    base, ext = os.path.splitext(path)
    aside = f"{base}.DAMAGED_{dt.datetime.now():%Y%m%d_%H%M%S}{ext}"
    os.replace(path, aside)
    good = _latest_good_backup()
    if good:
        shutil.copy2(good, path)
    print(f"  [!] {os.path.basename(path)} was damaged. Kept it as {os.path.basename(aside)}; "
          f"{'restored backup ' + os.path.basename(good) if good else 'no backup found, starting a new sheet'}. "
          f"Every capture in {os.path.basename(JOURNAL_FILE)} is added back.")


def _open_book(path):
    """(workbook, recovered) for editing. Creates a styled sheet if there is none yet.
    recovered=True means the file was damaged and replaced by a backup (see _set_aside_damaged)."""
    from openpyxl import Workbook, load_workbook
    recovered = False
    if os.path.exists(path):
        try:
            return load_workbook(path), False
        except PermissionError:
            raise
        except Exception:
            log_error(f"cannot open {path}; recovering from backup")
            _set_aside_damaged(path)
            recovered = True
            if os.path.exists(path):
                return load_workbook(path), True
    wb = Workbook()
    wb.active.title = SHEET_NAME
    return wb, recovered


def _read_rows_from_file(path):
    """Every data row of the sheet on disk as {header: value}. [] if missing/unreadable."""
    if not os.path.exists(path):
        return []
    from openpyxl import load_workbook
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        log_error(f"cannot read {path}")
        return []
    try:
        it = _leads_ws(wb).iter_rows(values_only=True)
        header = [_canon(h) for h in (next(it, None) or [])]
        return [{h: v for h, v in zip(header, r) if h} for r in it
                if r and any(v not in (None, "") for v in r)]
    finally:
        wb.close()   # an open handle would stop Excel from saving the file


def _journal_rows():
    if not os.path.exists(JOURNAL_FILE):
        return []
    with open(JOURNAL_FILE, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _pending_rows():
    if not os.path.exists(PENDING_FILE):
        return []
    with open(PENDING_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def existing_keys(path, include_pending=True):
    """(set of (poc, company), set of emails) already in the sheet (and pending_rows.csv)."""
    keys, emails = set(), set()
    rows = _read_rows_from_file(path) + (_pending_rows() if include_pending else [])
    for r in rows:
        keys.add(_dedupe_key(r))
        mail = _cell_str(r.get("Email ID")).lower()
        if mail:
            emails.add(mail)
    return keys, emails


def journal_rows(rows):
    """Append every captured row to captured_log.csv BEFORE touching Excel. This file is never
    deleted or used to block a capture, so a crash/restart/unsaved Excel can never lose a row."""
    new = not os.path.exists(JOURNAL_FILE) or os.path.getsize(JOURNAL_FILE) == 0
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        if new:
            w.writeheader()
        for row in rows:
            w.writerow({h: _cell_str(row.get(h, "")) for h in HEADERS})
        f.flush()
        os.fsync(f.fileno())


def backup_workbook(path=OUTPUT_FILE):
    """Keep timestamped copies of the sheet (at most one per BACKUP_EVERY_MIN minutes, newest
    BACKUPS_KEEP). Captures in between are in captured_log.csv. Never raises."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return
        import shutil
        os.makedirs(BACKUP_DIR, exist_ok=True)
        old = sorted(n for n in os.listdir(BACKUP_DIR) if n.lower().endswith(".xlsx"))
        if old and time.time() - os.path.getmtime(os.path.join(BACKUP_DIR, old[-1])) < BACKUP_EVERY_MIN * 60:
            return
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, os.path.join(BACKUP_DIR, f"leads_master_{stamp}.xlsx"))
        os.utime(os.path.join(BACKUP_DIR, f"leads_master_{stamp}.xlsx"))   # age = when backed up
        old = sorted(n for n in os.listdir(BACKUP_DIR) if n.lower().endswith(".xlsx"))
        for name in old[:-BACKUPS_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, name))
    except Exception:
        log_error("backup failed")


def write_rows(rows, path=OUTPUT_FILE):
    """Append rows to the sheet file (Excel closed). Raises PermissionError if it is open."""
    if _is_locked(path):
        raise PermissionError(f"{os.path.basename(path)} is open in another program")
    wb, recovered = _open_book(path)
    ws = _leads_ws(wb)
    col = _header_map(ws)
    last = _last_data_row(ws, max(col.values()))
    have = {_dedupe_key({h: ws.cell(r, c).value for h, c in col.items()}) for r in range(2, last + 1)}
    if recovered:   # put back every capture the backup does not have
        rows = [r for r in _unique_last(_journal_rows() + list(rows)) if _dedupe_key(r) not in have]
    for row in rows:
        last += 1
        _put_row(ws, last, row, col)
    _finish_sheet(ws, col, last)
    backup_workbook(path)
    _save_atomic(wb, path)


def tidy_workbook(path=OUTPUT_FILE):
    """Give the whole sheet the clean layout and refresh Missing Fields + yellow on every row,
    also for values you typed in by hand. Returns a one-line report."""
    if not os.path.exists(path):
        return "no sheet yet - it is created on the first capture"
    if _is_locked(path):
        raise PermissionError(f"{os.path.basename(path)} is open - close it to tidy")
    wb, recovered = _open_book(path)
    ws = _leads_ws(wb)
    col = _header_map(ws)
    last = _last_data_row(ws, max(col.values()))
    seen, dups, missing, total = set(), [], 0, 0
    for r in range(2, last + 1):
        row = {h: ws.cell(r, c).value for h, c in col.items()}
        if all(v in (None, "") for v in row.values()):
            continue
        total += 1
        row["Missing Fields"] = _missing_of(row)
        missing += bool(row["Missing Fields"])
        for h in HEADERS:
            _set_value(ws.cell(r, col[h]), h, row.get(h))
        _fill_row(ws, r, col, bool(row["Missing Fields"]))
        k = _dedupe_key(row)
        if k[0] and k in seen:
            dups.append(f"row {r}: {row.get('POC')} | {row.get('Company Name')}")
        seen.add(k)
    _finish_sheet(ws, col, last)
    backup_workbook(path)
    _save_atomic(wb, path)
    if recovered:
        restore_from_journal(path)
    report = f"{total} leads, {missing} with missing fields, {len(dups)} duplicate(s)"
    if dups:
        report += " -> " + "; ".join(dups[:5]) + (" ..." if len(dups) > 5 else "")
    return report


# ---------- the sheet while it is OPEN in desktop Excel (COM) ----------
_COM_BUSY = {-2147418111, -2147417846, -2146777998}   # call rejected / retry later / Excel in edit mode


def _com_retry(fn, tries=10, delay=0.3):
    """Excel says 'busy' while it recalculates, autosaves or shows a tooltip. Wait a moment and
    try again instead of failing the capture."""
    import pywintypes
    for i in range(tries):
        try:
            return fn()
        except pywintypes.com_error as e:
            if e.hresult not in _COM_BUSY or i == tries - 1:
                raise
            time.sleep(delay)


def _excel_headers(ws):
    """{header: column} of row 1 of the open sheet; our missing headers are added at the end."""
    lastcol = _com_retry(lambda: ws.Cells(1, ws.Columns.Count).End(-4159).Column)   # xlToLeft
    vals = _com_retry(lambda: ws.Range(ws.Cells(1, 1), ws.Cells(1, lastcol)).Value)
    vals = vals[0] if isinstance(vals, tuple) else (vals,)
    col = {}
    for i, v in enumerate(vals, 1):
        h = _canon(v)
        if h:
            col.setdefault(h, i)
    nxt = max(col.values(), default=0) + 1
    for h in HEADERS:
        if h not in col:
            _com_retry(lambda c=nxt, h=h: setattr(ws.Cells(1, c), "Value", h))
            col[h] = nxt
            nxt += 1
    return col


def _excel_last_row(ws):
    """Last row with a value, hidden/filtered rows included."""
    found = _com_retry(lambda: ws.Cells.Find("*", ws.Cells(1, 1), -4123, 2, 1, 2))  # formulas, part, rows, previous
    return found.Row if found is not None else 1


def _excel_link(ws, cell, h, v):
    target = _link_target(h, v)
    if target:
        _com_retry(lambda: ws.Hyperlinks.Add(cell, target, "", "", _cell_str(v)))
        cell.Font.Name = FONT_NAME


def _excel_fill(rng, missing):
    if missing:
        rng.Interior.Color = MISSING_BGR
    else:
        rng.Interior.ColorIndex = -4142   # xlNone


def write_rows_live(rows, path=OUTPUT_FILE):
    """Append rows into the copy of `path` that is OPEN in desktop Excel, then save it.
    All rows go in with ONE write, so it is all-or-nothing. Returns True if saved to disk,
    False if the rows are in the Excel window but saving failed (user must press Ctrl+S).
    Raises RuntimeError (nothing written) if it is not open in Excel, read-only, or busy."""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        wb = _find_open_workbook(path)   # finds it in whichever Excel window has it open
        if wb is None:
            raise RuntimeError("the file is locked by a program other than desktop Excel")
        if wb.ReadOnly:
            raise RuntimeError("the workbook is open READ-ONLY in Excel")
        try:
            ws = _sheet_of(wb)
            col = _excel_headers(ws)
            ncols = max(col.values())
            first = _excel_last_row(ws) + 1
            data = []
            for row in rows:
                line = [""] * ncols
                for h in HEADERS:
                    line[col[h] - 1] = _cell_str(row.get(h, ""))
                data.append(tuple(line))
            rng = ws.Range(ws.Cells(first, 1), ws.Cells(first + len(data) - 1, ncols))
            _com_retry(lambda: setattr(rng, "NumberFormat", "@"))   # '+971..' / '=x' stay text
            _com_retry(lambda: setattr(rng, "Value", tuple(data)))
        except Exception as e:
            log_error("writing into open Excel failed")
            raise RuntimeError("Excel is busy (a cell is being edited or a dialog is open)"
                               if getattr(e, "hresult", None) in _COM_BUSY
                               else f"Excel refused the write ({type(e).__name__})") from None
        try:   # looks only: a failure here never loses the row
            rng.Font.Name = FONT_NAME
            for k, row in enumerate(rows):
                r = first + k
                _excel_fill(ws.Range(ws.Cells(r, 1), ws.Cells(r, ncols)), bool(_cell_str(row.get("Missing Fields"))))
                for h in LINK_COLUMNS | {"Email ID"}:
                    _excel_link(ws, ws.Cells(r, col[h]), h, row.get(h))
        except Exception:
            log_error("styling rows in open Excel failed")
        backup_workbook(path)   # copy of the last saved state, taken before we overwrite it
        try:
            _com_retry(wb.Save)   # rows only in an Excel window are lost on a restart
            return True
        except Exception:
            log_error("saving open Excel failed")
            return False
    finally:
        pythoncom.CoUninitialize()


def write_rows_any(rows, path=OUTPUT_FILE):
    """Write to the file; if Excel has it open, write into the open Excel window.
    Returns 'file', 'excel' or 'excel-unsaved'. Raises PermissionError only if both fail."""
    try:
        write_rows(rows, path)
        return "file"
    except PermissionError:
        pass
    try:
        return "excel" if write_rows_live(rows, path) else "excel-unsaved"
    except RuntimeError as e:
        raise PermissionError(str(e))


def _missing_of(row):
    return ", ".join(f for f in IMPORTANT_FIELDS if not str(row.get(f) or "").strip())


# Words that say little about WHICH company/person it is; ignored when comparing names.
_GENERIC_WORDS = {"hospital", "clinic", "group", "company", "co", "ltd", "llc", "inc", "limited",
                  "the", "of", "and", "for", "health", "healthcare", "medical", "center", "centre",
                  "dr", "mr", "mrs", "ms"}


def _name_words(s):
    words = []
    for t in _fold(s):
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]                      # hospitals -> hospital
        words.append(t)
    core = [w for w in words if w not in _GENERIC_WORDS]
    return set(core or words)


def name_score(a, b):
    """0..1: how much of the shorter name is contained in the other (word based)."""
    A, B = _name_words(a), _name_words(b)
    return len(A & B) / min(len(A), len(B)) if A and B else 0.0


def pick_by_key(key):
    """Row chooser: the last row whose (POC, Company Name) equals `key`."""
    want = tuple(" ".join(_fold(k)) for k in key)

    def pick(rows):
        for i in range(len(rows) - 1, -1, -1):
            have = (" ".join(_fold(str(rows[i].get("POC") or ""))),
                    " ".join(_fold(str(rows[i].get("Company Name") or ""))))
            if have == want:
                return i
        return None
    return pick


def pick_by_name(column, name, threshold=0.6):
    """Row chooser: the row whose `column` ('POC' or 'Company Name') best matches `name`;
    a tie goes to the later row."""
    def pick(rows):
        best, best_s = None, 0.0
        for i, r in enumerate(rows):
            s = name_score(name, str(r.get(column) or ""))
            if s >= threshold and s >= best_s:
                best, best_s = i, s
        return best
    return pick


def pick_all_by_name(column, name, threshold=0.8):
    """Row chooser: EVERY row whose `column` matches `name` (all the people of one company)."""
    def pick(rows):
        return [i for i, r in enumerate(rows) if name_score(name, str(r.get(column) or "")) >= threshold]
    return pick


def linkedin_title_name(title):
    """'(3) Sahar Saadi | LinkedIn - Google Chrome' -> 'Sahar Saadi';
    'Sheikh Khalifa General Hospital: Overview | LinkedIn' -> 'Sheikh Khalifa General Hospital'."""
    t = re.sub(r"^\(\d+\+?\)\s*", "", title or "").strip()
    t = re.split(r"\s*\|\s*LinkedIn", t, flags=re.I)[0]
    t = re.split(r"\s+-\s+", t)[0]
    return t.split(":")[0].strip()


class _NotInExcel(Exception):
    pass


def _apply_row_update(get_row, set_cell, set_missing, set_fill, updates):
    """Shared by the file and Excel editors: write `updates`, then recompute
    Missing Fields and the yellow highlight. Returns the updated row dict."""
    row = get_row()
    for h, v in updates.items():
        if h in row:
            set_cell(h, v)
            row[h] = v
    missing = _missing_of(row)
    set_missing(missing)
    set_fill(bool(missing))
    return row


def _indices(picked):
    """A chooser may return one index, a list of indices, or None."""
    if picked is None:
        return []
    return [picked] if isinstance(picked, int) else list(picked)


def _same_file(a, b):
    if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)):
        return True
    try:   # same file reached by another path (mapped drive, short name, ...)
        return os.path.basename(a).lower() == os.path.basename(b).lower() and os.path.samefile(a, b)
    except Exception:
        return False


def _find_open_workbook(path):
    """The workbook `path` as open in a running desktop Excel, or None. Looks in Windows' list of
    running objects, so it NEVER starts Excel or opens the file (GetObject(path) would, and
    that could lock the file)."""
    import pythoncom
    import win32com.client
    try:
        rot = pythoncom.GetRunningObjectTable()
        ctx = pythoncom.CreateBindCtx(0)
        for moniker in rot.EnumRunning():
            try:
                name = moniker.GetDisplayName(ctx, None)
            except Exception:
                continue
            if name.lower().endswith(os.path.basename(path).lower()) and _same_file(name, path):
                disp = rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
                wb = win32com.client.Dispatch(disp)
                if hasattr(wb, "Worksheets"):
                    return wb
    except Exception:
        log_error("looking for the open workbook failed")
    return None


def _sheet_of(wb):
    try:
        return wb.Worksheets(SHEET_NAME)
    except Exception:
        return wb.Worksheets(1)


def _edit_in_file(pick, updates, path):
    """Edit the closed file. LookupError if no row; PermissionError if it is locked.
    Returns the list of updated rows."""
    if not os.path.exists(path):
        raise LookupError("no sheet yet")
    if _is_locked(path):
        raise PermissionError(f"{os.path.basename(path)} is open in another program")
    wb, _ = _open_book(path)
    ws = _leads_ws(wb)
    col = _header_map(ws)
    last = _last_data_row(ws, max(col.values()))
    rows = [{h: ws.cell(r, c).value for h, c in col.items()} for r in range(2, last + 1)]
    picked = _indices(pick(rows))
    if not picked:
        raise LookupError("no matching row in the sheet")
    out = []
    for i in picked:
        r = i + 2

        def set_cell(h, v, r=r):
            _set_value(ws.cell(r, col[h]), h, v)

        def set_missing(m, r=r):
            _set_value(ws.cell(r, col["Missing Fields"]), "Missing Fields", m)

        def set_fill(on, r=r):
            _fill_row(ws, r, col, on)

        out.append(_apply_row_update(lambda i=i: dict(rows[i]), set_cell, set_missing, set_fill, updates))
    _finish_sheet(ws, col, last)
    backup_workbook(path)
    _save_atomic(wb, path)
    return out


def _edit_in_open_excel(pick, updates, path):
    """Edit the copy of the file that is open in desktop Excel. That window is the truth:
    rows added there may not be saved to disk yet. Raises _NotInExcel if it is not open there.
    Returns the list of updated rows."""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        wb = _find_open_workbook(path)
        if wb is None:
            raise _NotInExcel()
        if wb.ReadOnly:
            raise RuntimeError("the workbook is open READ-ONLY in Excel")
        ws = _sheet_of(wb)
        col = _excel_headers(ws)
        ncols = max(col.values())
        last = _excel_last_row(ws)
        if last < 2:
            raise LookupError("no rows in the sheet")
        data = _com_retry(lambda: ws.Range(ws.Cells(2, 1), ws.Cells(last, ncols)).Value)
        if not isinstance(data[0], tuple):   # a single row comes back flat
            data = (data,)
        rows = [{h: data[k][c - 1] for h, c in col.items()} for k in range(len(data))]
        picked = _indices(pick(rows))
        if not picked:
            raise LookupError("no matching row in the sheet")
        out = []
        for i in picked:
            r = 2 + i

            def set_cell(h, v, r=r):
                cell = ws.Cells(r, col[h])
                _com_retry(lambda: setattr(cell, "NumberFormat", "@"))
                _com_retry(lambda: setattr(cell, "Value", _cell_str(v)))
                try:
                    _excel_link(ws, cell, h, v)
                except Exception:
                    log_error("adding hyperlink failed")

            def set_missing(m, r=r):
                cell = ws.Cells(r, col["Missing Fields"])
                _com_retry(lambda: setattr(cell, "Value", m))

            def set_fill(on, r=r):
                try:
                    _excel_fill(ws.Range(ws.Cells(r, 1), ws.Cells(r, ncols)), on)
                except Exception:
                    log_error("row colour failed")

            out.append(_apply_row_update(lambda i=i: dict(rows[i]), set_cell, set_missing, set_fill, updates))
        backup_workbook(path)
        try:
            _com_retry(wb.Save)
        except Exception:
            log_error("saving open Excel after edit failed")
        return out
    except (_NotInExcel, LookupError, RuntimeError):
        raise
    except Exception as e:
        log_error("editing open Excel failed")
        raise RuntimeError("Excel is busy (a cell is being edited or a dialog is open)"
                           if getattr(e, "hresult", None) in _COM_BUSY
                           else f"Excel refused the change ({type(e).__name__})") from None
    finally:
        pythoncom.CoUninitialize()


def edit_rows(pick, updates, path=OUTPUT_FILE):
    """Change fields of the row(s) chosen by `pick(rows) -> index | [indexes] | None` and refresh
    Missing Fields and the yellow highlight. Order: pending_rows.csv, then the copy open in Excel,
    then the file. Returns (where, [updated rows]). Updated rows are also logged to
    captured_log.csv, so --restore brings back the latest version (with its LinkedIn URLs).
    Raises LookupError (no such row) or PermissionError (Excel busy)."""
    where, out = _edit_rows(pick, updates, path)
    try:
        journal_rows(out)
    except Exception:
        log_error("journal after edit failed")
    return where, out


def _edit_rows(pick, updates, path):
    rows = _pending_rows()
    picked = _indices(pick(rows)) if rows else []
    if picked:
        for i in picked:
            rows[i].update(updates)
            rows[i]["Missing Fields"] = _missing_of(rows[i])
        with open(PENDING_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADERS)
            w.writeheader()
            for r in rows:
                w.writerow({h: r.get(h, "") for h in HEADERS})
        return "pending", [rows[i] for i in picked]
    try:
        return "excel", _edit_in_open_excel(pick, updates, path)
    except _NotInExcel:
        pass
    except RuntimeError as e:
        raise PermissionError(str(e))
    return "file", _edit_in_file(pick, updates, path)


def read_sheet_rows(path=OUTPUT_FILE):
    """Every row of the sheet as {header: value}: from the copy open in Excel if there is one
    (it can be ahead of the file), else from the file. [] if neither can be read."""
    try:
        import pythoncom
        pythoncom.CoInitialize()
        try:
            wb = _find_open_workbook(path)
            if wb is not None:
                data = _com_retry(lambda: _sheet_of(wb).UsedRange.Value)
                if data and len(data) > 1 and isinstance(data[0], tuple):
                    col = {_canon(h): i for i, h in enumerate(data[0]) if _canon(h)}
                    return [{h: data[k][c] for h, c in col.items()} for k in range(1, len(data))]
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        log_error("reading open Excel failed")
    return _read_rows_from_file(path)


def known_company_linkedin(company, rows):
    """The Company Linkedin Url already in the sheet for this company (latest row wins), or ''."""
    for r in reversed(rows):
        url = str(r.get("Company Linkedin Url") or "").strip()
        if url and name_score(company, str(r.get("Company Name") or "")) >= 0.8:
            return url
    return ""


def update_row_fields(key, updates, path=OUTPUT_FILE):
    """Same as edit_rows for the row with (POC, Company) `key`; returns only 'pending'|'file'|'excel'."""
    return edit_rows(pick_by_key(key), updates, path)[0]


def save_pending(row):
    new = not os.path.exists(PENDING_FILE)
    with open(PENDING_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        if new:
            w.writeheader()
        w.writerow({h: row.get(h, "") for h in HEADERS})


def flush_pending():
    """Move rows saved while Excel was busy into the sheet (skipping any already there)."""
    rows = _pending_rows()
    todo = []
    if rows:
        have, _ = existing_keys(OUTPUT_FILE, include_pending=False)
        todo = [r for r in _unique_last(rows) if _dedupe_key(r) not in have]
        if todo and write_rows_any(todo) == "excel-unsaved":
            SESSION_KEYS.update(_dedupe_key(r) for r in todo)
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)
    return len(todo)


def save_debug(text, html):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(DEBUG_DIR, f"{stamp}.txt"), "w", encoding="utf-8") as f:
        f.write(text or "")
    if html:
        with open(os.path.join(DEBUG_DIR, f"{stamp}.html"), "w", encoding="utf-8") as f:
            f.write(html)


# ----------------------------------------------------------------------
# WINDOWS CLIPBOARD + FEEDBACK
# ----------------------------------------------------------------------
def read_clipboard():
    import win32clipboard
    html, text = "", ""
    fmt_html = win32clipboard.RegisterClipboardFormat("HTML Format")
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(fmt_html):
                    raw = win32clipboard.GetClipboardData(fmt_html)
                    html = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            break
        except Exception:
            time.sleep(0.1)
    return html, text


def beep(ok=True):
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
    except Exception:
        pass


def clear_clipboard():
    """Empty the clipboard so a failed copy can never be mistaken for the last one."""
    import win32clipboard
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False


_VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "esc": 0x1B}
_MOD_FLAG = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}


def _vk(name):
    name = name.strip().lower()
    if name in _VK:
        return _VK[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise ValueError(f"unsupported key '{name}' (use letters, digits, ctrl, alt, shift, win)")


def parse_hotkey(hotkey):
    """'ctrl+alt+z' -> (modifier flags, virtual-key code) for RegisterHotKey."""
    parts = [p for p in hotkey.lower().replace(" ", "").split("+") if p]
    mods = 0
    for p in parts[:-1]:
        if p not in _MOD_FLAG:
            raise ValueError(f"'{p}' is not a modifier in hotkey '{hotkey}'")
        mods |= _MOD_FLAG[p]
    if not parts or mods == 0:
        raise ValueError(f"hotkey '{hotkey}' needs at least one modifier (ctrl/alt/shift/win)")
    return mods, _vk(parts[-1])


def send_keys(combo):
    """Press a combo such as 'ctrl+a' in the active window (Windows, no extra package)."""
    import ctypes
    keys = [_vk(p) for p in combo.split("+")]
    keybd = ctypes.windll.user32.keybd_event
    for k in keys:
        keybd(k, 0, 0, 0)
    for k in reversed(keys):
        keybd(k, 0, 2, 0)   # 2 = key up


def wait_keys_released(timeout=2.0):
    """Wait until Ctrl/Alt/Shift/Win are physically up, so our own Ctrl+A / Ctrl+C are clean."""
    import ctypes
    state = ctypes.windll.user32.GetAsyncKeyState
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(state(vk) & 0x8000 for vk in (0x11, 0x12, 0x10, 0x5B, 0x5C)):
            return True
        time.sleep(0.03)
    return False


def auto_copy(send=None, read=None, clear=None, timeout=None):
    """Press Ctrl+A, Ctrl+C in the active window and wait for the text to arrive.
    Returns (html, text); text is empty if nothing was copied in time.
    The functions are parameters only so this can be tested without a keyboard."""
    send = send or send_keys
    read, clear = read or read_clipboard, clear or clear_clipboard
    timeout = COPY_TIMEOUT if timeout is None else timeout
    clear()
    send("ctrl+a")
    time.sleep(0.15)
    send("ctrl+c")
    deadline = time.monotonic() + timeout
    while True:
        html, text = read()
        if text.strip() or time.monotonic() >= deadline:
            return html, text
        time.sleep(0.1)


def copy_address_bar(send=None, read=None, clear=None, timeout=None):
    """Copy the browser's address bar (Ctrl+L, Ctrl+C), then Esc to give the page its focus back.
    Reads only the URL you are looking at; nothing is fetched. Returns the copied text."""
    send = send or send_keys
    read, clear = read or read_clipboard, clear or clear_clipboard
    timeout = COPY_TIMEOUT if timeout is None else timeout
    clear()
    send("ctrl+l")
    time.sleep(0.2)
    send("ctrl+c")
    deadline = time.monotonic() + timeout
    text = ""
    while True:
        _, text = read()
        if text.strip() or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    send("esc")
    return text.strip()


def foreground_title():
    """Title of the window in front (the browser tab title, e.g. 'Jane Doe | LinkedIn')."""
    import ctypes
    u = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(u.GetForegroundWindow(), buf, 512)
    return buf.value


# ----------------------------------------------------------------------
# POPUP (small, bottom-right, never steals focus, clears itself)
# ----------------------------------------------------------------------
class Toast:
    """One tiny always-on-top window. Tk lives on its own thread; other threads
    only put messages on a queue. If Tk is missing, it silently does nothing."""
    COLORS = {"ok": "#1E7B34", "warn": "#B26A00", "dup": "#555555", "error": "#B3261E",
              "info": "#1565C0"}
    SECONDS = {"ok": 2.5, "warn": 6.0, "dup": 4.0, "error": 6.0, "info": 3.0}

    def __init__(self):
        import queue
        import threading
        self.q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def show(self, kind, title, detail=""):
        self.q.put((kind, title, detail))

    def _run(self):
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
        except Exception as e:
            print(f"  [!] Popups are unavailable ({type(e).__name__}: {e}). "
                  "Watch this console and listen for the beeps instead.")
            return
        state = {"win": None}

        def no_focus(win):
            try:   # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: no focus steal, no taskbar button
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
                style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
                ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
            except Exception:
                pass

        def close():
            if state["win"] is not None:
                try:
                    state["win"].destroy()
                except Exception:
                    pass
                state["win"] = None

        def display(kind, title, detail):
            close()
            win = tk.Toplevel(root)
            state["win"] = win
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-alpha", 0.96)
            except Exception:
                pass
            bg = self.COLORS.get(kind, "#333333")
            frame = tk.Frame(win, bg=bg, padx=14, pady=10)
            frame.pack()
            tk.Label(frame, text=title, bg=bg, fg="white", anchor="w", justify="left",
                     font=("Segoe UI", 11, "bold"), wraplength=380).pack(anchor="w")
            if detail:
                tk.Label(frame, text=detail, bg=bg, fg="white", anchor="w", justify="left",
                         font=("Segoe UI", 9), wraplength=380).pack(anchor="w", pady=(3, 0))
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            x = win.winfo_screenwidth() - w - 20
            y = win.winfo_screenheight() - h - 70
            win.geometry(f"+{x}+{y}")
            no_focus(win)
            win.bind("<Button-1>", lambda e: close())
            frame.bind("<Button-1>", lambda e: close())
            win.after(int(self.SECONDS.get(kind, 4) * 1000), lambda w=win: close() if state["win"] is w else None)

        def poll():
            try:
                while True:
                    display(*self.q.get_nowait())
            except Exception:
                pass
            root.after(100, poll)

        root.after(100, poll)
        root.mainloop()


_TOAST = None


def notify(kind, title, detail="", sound=None):
    """Console line + popup + beep. kind: ok | warn (saved, fields missing) | dup | error."""
    global _TOAST
    print(f"  [{ {'ok': 'OK', 'warn': 'OK', 'dup': '=', 'error': '!', 'info': '..'}.get(kind, '-') }] {title}"
          + (f" - {' | '.join(detail.splitlines())}" if detail else ""))
    if POPUP:
        try:
            if _TOAST is None:
                _TOAST = Toast()
            _TOAST.show(kind, title, detail)
        except Exception:
            pass
    if kind == "info" and sound is None:
        try:   # short tick: "I heard the hotkey"
            import winsound
            winsound.Beep(1200, 60)
        except Exception:
            pass
        return
    beep(kind in ("ok", "warn") if sound is None else sound)


def get_copy():
    """Text of the page: copied for you (AUTO_COPY) or read from what you copied."""
    return auto_copy() if AUTO_COPY else read_clipboard()


# ----------------------------------------------------------------------
# PRE-CHECK: which people on a copied search-results page are already done?
# ----------------------------------------------------------------------
def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def find_already_done(clip_text, path=OUTPUT_FILE):
    """Return [(poc, company)] for every person already in the master sheet
    (or pending file) whose name appears in the copied text.
    Works on any page text, so it does not depend on the results-table layout."""
    haystack = " " + _norm(clip_text) + " "
    done = []
    people = []
    for r in read_sheet_rows(path) + _pending_rows():
        people.append((_cell_str(r.get("POC")), _cell_str(r.get("Company Name"))))
    for poc, company in people:
        n = _norm(poc)
        if len(n.split()) >= 2 and f" {n} " in haystack:
            done.append((poc, company))
    return done


def precheck():
    _, text = get_copy()
    if not text.strip():
        notify("error", "Nothing copied",
               "Click an empty area of the results page, then press the hotkey again.")
        return
    done = find_already_done(text)
    if not done:
        notify("ok", "None of these are in your sheet", "Safe to open any of them.")
        return
    lines = [f"{poc} ({company})" if company else poc for poc, company in done[:8]]
    if len(done) > 8:
        lines.append(f"... and {len(done) - 8} more")
    for ln in lines:
        print(f"      - {ln}")
    notify("dup", f"{len(done)} already in your sheet - do NOT open",
           "\n".join(lines) + "\n(Same name can be a different person: check the company.)")


# ----------------------------------------------------------------------
# MAIN CAPTURE
# ----------------------------------------------------------------------
_BUSY = None


def _run_once(fn):
    """Ignore a second hotkey press while the first one is still working."""
    global _BUSY
    import threading
    if _BUSY is None:
        _BUSY = threading.Lock()
    if not _BUSY.acquire(blocking=False):
        return
    try:
        fn()
    except Exception as e:   # never let one bad capture kill the hotkey listener
        log_error("hotkey action failed")
        notify("error", "Something went wrong - nothing lost",
               f"{type(e).__name__}: {e}\nDetails: capture_errors.log. Press the hotkey again.")
    finally:
        _BUSY.release()


def capture():
    html, text = get_copy()
    if not text.strip():
        notify("error", "Nothing was copied",
               "Click an empty area of the profile page (not a search box), then press the hotkey again.")
        return

    row = parse_profile(text, html)
    if not row["POC"] and not row["Company Name"]:
        if SAVE_DEBUG_ON_MISSING:
            save_debug(text, html)
        notify("error", "This is not a ZoomInfo profile - nothing saved",
               "Open a contact profile, click an empty area of the page, then press the hotkey.")
        return

    LAST_CAPTURE.clear()   # the LinkedIn hotkey attaches its URL to this person
    LAST_CAPTURE.update(key=_dedupe_key(row), poc=row["POC"], company=row["Company Name"])

    keys, emails = existing_keys(OUTPUT_FILE)
    keys |= SESSION_KEYS   # only rows that could not be saved to disk yet (see below)
    by_name = _dedupe_key(row) in keys
    if by_name or (row["Email ID"] and row["Email ID"].lower() in emails):
        how = "same name + company" if by_name else f"same email {row['Email ID']}"
        notify("dup", "Already in your sheet - skipped", f"{row['POC']} | {row['Company Name']}\n({how})")
        return

    # Same company already in your sheet with its LinkedIn page? Reuse it: no need to look it up again.
    company_note = ""
    if not row["Company Linkedin Url"] and row["Company Name"]:
        try:
            known = known_company_linkedin(row["Company Name"], read_sheet_rows())
        except Exception:
            known = ""
        if known:
            row["Company Linkedin Url"] = known
            row["Missing Fields"] = _missing_of(row)
            company_note = "Company LinkedIn reused from your sheet"

    if row["Missing Fields"] and SAVE_DEBUG_ON_MISSING:
        save_debug(text, html)

    journal_rows([row])   # safety copy first: survives Excel crashes, restarts, unsaved files
    pending = False
    try:
        moved = flush_pending()
        where = write_rows_any([row])
        if moved:
            print(f"  [+] Also added {moved} pending row(s).")
        if where == "excel-unsaved":
            SESSION_KEYS.add(_dedupe_key(row))
            pending = True
        status = {"file": "Saved", "excel": "Saved in open Excel",
                  "excel-unsaved": "Added to Excel - press Ctrl+S there"}[where]
    except PermissionError as e:
        save_pending(row)
        SESSION_KEYS.add(_dedupe_key(row))
        pending = True
        status = f"Kept for later (Excel busy: {e})"

    if row["Missing Fields"]:
        hint = ""
        if "Linkedin Url" in row["Missing Fields"]:
            hint = f"\nLinkedIn missing: click its icon, then press {HOTKEY.upper()} on that LinkedIn tab"
        extra = f"\n{company_note}" if company_note else ""
        notify("warn", f"{status}: {row['POC'] or 'unknown'}",
               f"{row['Company Name']}\nMISSING: {row['Missing Fields']}{extra}{hint}")
    else:
        extra = f"\n{company_note}" if company_note else ""
        notify("warn" if pending else "ok", f"{status}: {row['POC']}",
               f"{row['Company Name']} | {row['Email ID'] or '-'}{extra}")


def linkedin_capture():
    """On a LinkedIn tab: put the tab's URL into the right row of the sheet. A person's page goes
    to 'POC Linkedin Url' of the row whose POC matches the tab's name; a company's page goes to
    'Company Linkedin Url' of the row whose Company Name matches. The row captured last is only a
    fallback, and only when it matches the tab too, so a URL can never land on the wrong person."""
    raw = copy_address_bar()
    company_url = normalize_linkedin_company_url(raw)
    person_url = "" if company_url else normalize_linkedin_url(raw)
    if company_url:
        field, url, column, label = "Company Linkedin Url", company_url, "Company Name", "Company LinkedIn"
    elif person_url:
        field, url, column, label = "POC Linkedin Url", person_url, "POC", "LinkedIn"
    else:
        notify("error", "This tab is not a LinkedIn profile or company page",
               "Open the person's (linkedin.com/in/...) or company's (linkedin.com/company/...) page, "
               "then press the hotkey again.")
        return
    title = foreground_title()
    shown = linkedin_title_name(title)
    picks = []
    if shown:   # a company's page fills EVERY person of that company; a person's page fills that person
        picks.append(pick_all_by_name(column, shown) if column == "Company Name"
                     else pick_by_name(column, shown))
    if LAST_CAPTURE:
        last_name = LAST_CAPTURE.get("company" if column == "Company Name" else "poc") or ""
        if last_name and linkedin_matches_name(last_name, title + " " + url):
            picks.append(pick_by_key(LAST_CAPTURE["key"]))
    for pick in picks:
        try:
            _, rows = edit_rows(pick, {field: url})
        except LookupError:
            continue
        except PermissionError as e:
            notify("error", "Excel is busy - LinkedIn not saved",
                   f"{e}. Close any open cell or dialog and press the hotkey again.")
            return
        if column == "Company Name":
            people = ", ".join(str(r.get("POC") or "?") for r in rows[:4]) + (" ..." if len(rows) > 4 else "")
            notify("ok", f"{label} saved for {len(rows)} row(s)",
                   f"{rows[0].get('Company Name') or '?'}: {people}" + chr(10) + url)
        else:
            who = f"{rows[0].get('POC') or '?'} | {rows[0].get('Company Name') or '?'}"
            notify("ok", f"{label} saved", who + chr(10) + url)
        return
    notify("error", f"No row for '{shown or 'this page'}' in your sheet - not saved",
           f"Capture their ZoomInfo profile first ({HOTKEY.upper()} on ZoomInfo), "
           f"then press the hotkey on this LinkedIn tab again.")


def _on_linkedin_tab():
    """True when the browser tab in front is a LinkedIn page ('Jane Doe | LinkedIn')."""
    try:
        return "linkedin" in foreground_title().lower()
    except Exception:
        return False


def smart_capture():
    """ONE hotkey for both jobs: on a LinkedIn tab it saves that tab's URL into the
    last captured row; anywhere else it captures the ZoomInfo profile."""
    if _on_linkedin_tab():
        linkedin_capture()
    else:
        capture()


def _on_hotkey(name, fn):
    if name == "capture" and _on_linkedin_tab():
        name = "linkedin"
    print(f"  [hotkey] {name}")
    notify("info", {"capture": "Capturing profile...", "linkedin": "Reading LinkedIn address..."}.get(
        name, "Checking for duplicates..."), "Hotkey received. Keep this page in front.")
    wait_keys_released()
    _run_once(fn)


def hotkey_loop(bindings, stop=None):
    """Register global hotkeys with Windows itself (RegisterHotKey) and dispatch them.
    bindings: {'ctrl+alt+z': (name, function)}. Runs until Ctrl+C (or stop.is_set())."""
    import ctypes
    import ctypes.wintypes as wt
    import threading
    user32 = ctypes.windll.user32
    ids = {}
    try:
        for i, (hk, (name, fn)) in enumerate(bindings.items(), start=1):
            mods, vk = parse_hotkey(hk)
            if not user32.RegisterHotKey(None, i, mods | 0x4000, vk):   # 0x4000 = no auto-repeat
                raise OSError(f"Could not register {hk.upper()}: another program already uses it. "
                              "Change HOTKEY / PRECHECK_HOTKEY at the top of this script.")
            ids[i] = (name, fn)
        msg = wt.MSG()
        while not (stop and stop.is_set()):
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):   # 1 = remove
                if msg.message == 0x0312 and msg.wParam in ids:         # WM_HOTKEY
                    name, fn = ids[msg.wParam]
                    threading.Thread(target=_on_hotkey, args=(name, fn), daemon=True).start()
            else:
                time.sleep(0.03)   # keeps Ctrl+C working
    finally:
        for i in ids:
            user32.UnregisterHotKey(None, i)


def restore_from_journal(path=OUTPUT_FILE):
    """python zoominfo_capture.py --restore : add every row of captured_log.csv that is
    missing from the sheet (matched by POC + Company). Returns the number of rows added."""
    if not os.path.exists(JOURNAL_FILE):
        print("No captured_log.csv found - nothing to restore.")
        return 0
    logged = _journal_rows()
    have, _ = existing_keys(path)
    todo = [r for r in _unique_last(logged) if _dedupe_key(r) not in have]
    if todo:
        write_rows_any(todo, path)
    print(f"Restored {len(todo)} row(s) from captured_log.csv ({len(logged)} logged, rest already in sheet).")
    return len(todo)


def self_check():
    """Problems that would make every capture fail, in plain words. [] = all good."""
    problems = []
    for mod, pkg in (("openpyxl", "openpyxl"), ("win32com.client", "pywin32")):
        try:
            __import__(mod)
        except ImportError:
            problems.append(f"Package '{pkg}' is not installed: run  pip install -r requirements.txt")
    try:
        probe = os.path.join(BASE_DIR, "~write_test.tmp")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError:
        problems.append(f"Cannot write in {BASE_DIR}. Move the tool to a normal folder, e.g. D:\\lead_tool")
    return problems


def main():
    if "--restore" in sys.argv:
        restore_from_journal()
        return
    if "--tidy" in sys.argv:
        try:
            print("Sheet tidied:", tidy_workbook())
        except PermissionError as e:
            print(f"Close Excel first ({e}).")
        return
    problems = self_check()
    if problems:
        print("\n".join("  [X] " + p for p in problems))
        return
    if sys.platform != "win32":
        print("This tool runs on Windows. (Parser can be tested anywhere via test_parser.py)")
        return
    print("=" * 64)
    print(" ZoomInfo Capture Helper is running")
    print(f" Output file : {os.path.abspath(OUTPUT_FILE)}")
    print(f" Safety copy : {JOURNAL_FILE}")
    if AUTO_COPY:
        print(f" Capture     : {HOTKEY.upper()}   (open profile -> click an empty area of the page -> hotkey)")
        print(f" Pre-check   : {PRECHECK_HOTKEY.upper()}   (open results page -> click empty area -> hotkey, BEFORE opening profiles)")
    else:
        print(f" Capture     : {HOTKEY.upper()}   (open profile -> Ctrl+A -> Ctrl+C -> hotkey)")
        print(f" Pre-check   : {PRECHECK_HOTKEY.upper()}   (copy results page -> hotkey, BEFORE opening profiles)")
    print(f" LinkedIn    : click the LinkedIn icon, then press {HOTKEY.upper()} again on that LinkedIn tab "
          f"(or {LINKEDIN_HOTKEY.upper()})")
    print(" Stop        : press Ctrl+C in this window")
    print("=" * 64)
    try:
        n = flush_pending()
        if n:
            print(f"  [+] Added {n} pending row(s) from last session.")
    except PermissionError:
        print("  [!] Excel is busy. Pending rows will be added on the next capture.")
    try:
        if not _is_locked(OUTPUT_FILE):
            print("  [i] Sheet check:", tidy_workbook())
    except Exception as e:
        log_error("startup tidy failed")
        print(f"  [!] Sheet check skipped ({type(e).__name__}); captures still work.")
    if POPUP:
        notify("ok", "Capture helper is running",
               f"{HOTKEY.upper()} = capture | {PRECHECK_HOTKEY.upper()} = duplicate pre-check", sound=True)
    try:
        hotkey_loop({HOTKEY: ("capture", smart_capture), PRECHECK_HOTKEY: ("pre-check", precheck),
                     LINKEDIN_HOTKEY: ("linkedin", linkedin_capture)})
    except OSError as e:
        notify("error", "Hotkey not available", str(e))
        time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
