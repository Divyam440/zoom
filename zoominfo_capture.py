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
OUTPUT_FILE = "leads_master.xlsx"
PENDING_FILE = "pending_rows.csv"      # used when the Excel file is open/locked
DEBUG_DIR = "debug_captures"           # raw copies saved when fields are missing
SAVE_DEBUG_ON_MISSING = True

# Your 11 headers first (exact order), then helpful extras
HEADERS = [
    "Company Name", "Employees", "Industry", "Website", "Company Linkedin Url",
    "POC", "Job title", "Email ID", "Company Country", "Company Phone", "Date",
    "Mobile Phone", "POC Linkedin Url", "Missing Fields",
    "Call Status", "Verified", "Notes",
]
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
_LI_REFUSED = {}       # last LinkedIn URL that did not look like LAST_CAPTURE's person

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
    """Returns (company_phone, mobile_phone)."""
    sec = _section(lines, "Phone numbers", ["Location", "Tags", "About", "Similar Contacts"])
    hq, mobile, other = "", "", ""
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
        elif not other:
            other = phone
    return hq or other, mobile


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
    hq_phone, mobile = parse_phones(lines)
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
        "Notes": "",
    }
    missing = [f for f in IMPORTANT_FIELDS if not row.get(f)]
    row["Missing Fields"] = ", ".join(missing)
    return row


# ----------------------------------------------------------------------
# EXCEL OUTPUT
# ----------------------------------------------------------------------
def _dedupe_key(row):
    return (row.get("POC", "").strip().lower(), row.get("Company Name", "").strip().lower())


def _open_or_create_workbook(path):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill

    if os.path.exists(path):
        return load_workbook(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    ws.freeze_panes = "A2"
    for col, width in zip("ABCDEFGHIJKLMNOPQ",
                          [28, 12, 18, 26, 34, 22, 26, 30, 16, 18, 12, 18, 34, 30, 14, 10, 30]):
        ws.column_dimensions[col].width = width
    return wb


def existing_keys(path):
    keys, emails = set(), set()
    if os.path.exists(path):
        from openpyxl import load_workbook
        ws = load_workbook(path, read_only=True).active
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None) or []
        idx = {h: i for i, h in enumerate(header)}
        for r in rows:
            poc = (r[idx["POC"]] or "") if "POC" in idx else ""
            comp = (r[idx["Company Name"]] or "") if "Company Name" in idx else ""
            mail = (r[idx["Email ID"]] or "") if "Email ID" in idx else ""
            keys.add((str(poc).strip().lower(), str(comp).strip().lower()))
            if mail:
                emails.add(str(mail).strip().lower())
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                keys.add(_dedupe_key(r))
                if r.get("Email ID"):
                    emails.add(r["Email ID"].strip().lower())
    return keys, emails


def write_rows(rows, path=OUTPUT_FILE):
    """Append rows to Excel. Raises PermissionError if the file is open."""
    from openpyxl.styles import Font, PatternFill
    wb = _open_or_create_workbook(path)
    ws = wb.active
    yellow = PatternFill("solid", fgColor="FFF2CC")
    for row in rows:
        ws.append([row.get(h, "") for h in HEADERS])
        r = ws.max_row
        for cell in ws[r]:
            cell.font = Font(name="Arial")
            if row.get("Missing Fields"):
                cell.fill = yellow
    wb.save(path)


def _excel_safe(v):
    return "" if v is None else str(v)


def write_rows_live(rows, path=OUTPUT_FILE):
    """Append rows into the copy of `path` that is OPEN in desktop Excel.
    Raises RuntimeError if Excel/the workbook is not open or is busy (e.g. a
    cell is being edited); the caller then falls back to pending_rows.csv."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        try:
            # Finds the workbook by file path in whichever Excel window has it open
            wb = win32com.client.GetObject(os.path.abspath(path))
        except Exception:
            raise RuntimeError("workbook is locked but not open in desktop Excel")
        if not hasattr(wb, "Worksheets"):
            raise RuntimeError("workbook is locked but not open in desktop Excel")
        if wb.ReadOnly:
            raise RuntimeError("workbook is open read-only")
        try:
            ws = wb.Worksheets("Leads")
        except Exception:
            ws = wb.Worksheets(1)
        was_saved = bool(wb.Saved)   # False = you have unsaved edits; we won't save those for you
        used = ws.UsedRange
        last = used.Row + used.Rows.Count - 1
        yellow = 255 + 242 * 256 + 204 * 65536   # FFF2CC as an Excel BGR colour
        for row in rows:
            last += 1
            rng = ws.Range(ws.Cells(last, 1), ws.Cells(last, len(HEADERS)))
            rng.NumberFormat = "@"   # text: "+1 555..." or "=x" must not become a formula
            rng.Value = ([_excel_safe(row.get(h, "")) for h in HEADERS],)
            rng.Font.Name = "Arial"
            if row.get("Missing Fields"):
                rng.Interior.Color = yellow
        if was_saved:
            wb.Save()   # keep the file on disk in step, so duplicates are still caught
    except RuntimeError:
        raise
    except Exception as e:   # COM errors: cell in edit mode, dialog open, protected sheet...
        raise RuntimeError(f"Excel is busy ({type(e).__name__}); close any open cell/dialog") from None
    finally:
        pythoncom.CoUninitialize()


def write_rows_any(rows, path=OUTPUT_FILE):
    """Write to the file; if Excel has it locked, write into the open Excel window.
    Returns 'file' or 'excel'. Raises PermissionError only if both fail."""
    try:
        write_rows(rows, path)
        return "file"
    except PermissionError:
        pass
    try:
        write_rows_live(rows, path)
        return "excel"
    except RuntimeError as e:
        raise PermissionError(str(e))


def _missing_of(row):
    return ", ".join(f for f in IMPORTANT_FIELDS if not str(row.get(f) or "").strip())


def _update_in_file(key, updates, path):
    """Change fields of the row `key` in the closed file. LookupError if not found;
    PermissionError if Excel has the file open."""
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill
    wb = load_workbook(path)
    ws = wb.active
    col = {str(c.value): c.column for c in ws[1] if c.value}
    if "POC" not in col or "Company Name" not in col:
        raise LookupError("sheet has no POC / Company Name columns")
    for r in range(ws.max_row, 1, -1):
        cell = lambda h: ws.cell(r, col[h]).value if h in col else None
        if (str(cell("POC") or "").strip().lower(),
                str(cell("Company Name") or "").strip().lower()) == key:
            for h, v in updates.items():
                if h in col:
                    ws.cell(r, col[h]).value = v
            row = {h: ws.cell(r, c).value for h, c in col.items()}
            missing = _missing_of(row)
            if "Missing Fields" in col:
                ws.cell(r, col["Missing Fields"]).value = missing
            fill = PatternFill("solid", fgColor="FFF2CC") if missing else PatternFill(fill_type=None)
            for c in range(1, len(HEADERS) + 1):
                ws.cell(r, c).fill = fill
            wb.save(path)
            return
    raise LookupError("row not found in the sheet")


def _update_in_open_excel(key, updates, path):
    """Same, but inside the copy of the file that is open in desktop Excel."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        try:
            wb = win32com.client.GetObject(os.path.abspath(path))
            ws = wb.Worksheets("Leads")
        except Exception:
            try:
                ws = wb.Worksheets(1)
            except Exception:
                raise RuntimeError("workbook is locked but not open in desktop Excel")
        if wb.ReadOnly:
            raise RuntimeError("workbook is open read-only")
        was_saved = bool(wb.Saved)
        used = ws.UsedRange
        top, left = used.Row, used.Column
        ncols, nrows = used.Columns.Count, used.Rows.Count
        data = ws.Range(ws.Cells(top, left), ws.Cells(top + nrows - 1, left + ncols - 1)).Value
        header = [str(h) if h else "" for h in data[0]]
        col = {h: i for i, h in enumerate(header) if h}      # 0-based within the used range
        if "POC" not in col or "Company Name" not in col:
            raise LookupError("sheet has no POC / Company Name columns")
        for i in range(nrows - 1, 0, -1):
            vals = data[i]
            if (str(vals[col["POC"]] or "").strip().lower(),
                    str(vals[col["Company Name"]] or "").strip().lower()) != key:
                continue
            r = top + i
            row = {h: vals[c] for h, c in col.items()}
            for h, v in updates.items():
                if h in col:
                    cell = ws.Cells(r, left + col[h])
                    cell.NumberFormat = "@"
                    cell.Value = v
                    row[h] = v
            missing = _missing_of(row)
            if "Missing Fields" in col:
                ws.Cells(r, left + col["Missing Fields"]).Value = missing
            rng = ws.Range(ws.Cells(r, left), ws.Cells(r, left + min(ncols, len(HEADERS)) - 1))
            if missing:
                rng.Interior.Color = 255 + 242 * 256 + 204 * 65536
            else:
                rng.Interior.ColorIndex = -4142   # xlNone
            if was_saved:
                wb.Save()
            return
        raise LookupError("row not found in the sheet")
    except (RuntimeError, LookupError):
        raise
    except Exception as e:
        raise RuntimeError(f"Excel is busy ({type(e).__name__}); close any open cell/dialog") from None
    finally:
        pythoncom.CoUninitialize()


def update_row_fields(key, updates, path=OUTPUT_FILE):
    """Set `updates` ({header: value}) on the row whose (POC, Company) lower-case key is
    `key`, and recompute Missing Fields + the yellow highlight. Looks in pending_rows.csv,
    then the file, then the copy open in Excel. Returns 'pending' | 'file' | 'excel'.
    Raises LookupError (row not found) or PermissionError (Excel busy)."""
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        hits = [r for r in rows if _dedupe_key(r) == key]
        if hits:
            hits[-1].update(updates)
            hits[-1]["Missing Fields"] = _missing_of(hits[-1])
            with open(PENDING_FILE, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=HEADERS)
                w.writeheader()
                for r in rows:
                    w.writerow({h: r.get(h, "") for h in HEADERS})
            return "pending"
    try:
        _update_in_file(key, updates, path)
        return "file"
    except PermissionError:
        pass
    try:
        _update_in_open_excel(key, updates, path)
        return "excel"
    except RuntimeError as e:
        raise PermissionError(str(e))


def save_pending(row):
    new = not os.path.exists(PENDING_FILE)
    with open(PENDING_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        if new:
            w.writeheader()
        w.writerow({h: row.get(h, "") for h in HEADERS})


def flush_pending():
    """Move rows saved while Excel was open into the Excel file."""
    if not os.path.exists(PENDING_FILE):
        return 0
    with open(PENDING_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        os.remove(PENDING_FILE)
        return 0
    write_rows_any(rows)
    os.remove(PENDING_FILE)
    return len(rows)


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
    if os.path.exists(path):
        from openpyxl import load_workbook
        ws = load_workbook(path, read_only=True).active
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None) or []
        idx = {h: i for i, h in enumerate(header)}
        if "POC" in idx:
            for r in rows:
                people.append((str(r[idx["POC"]] or ""),
                               str(r[idx["Company Name"]] or "") if "Company Name" in idx else ""))
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                people.append((r.get("POC", ""), r.get("Company Name", "")))
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
        notify("error", "Capture failed", f"{type(e).__name__}: {e}")
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
    keys |= SESSION_KEYS   # rows written live into an unsaved Excel are not on disk yet
    if _dedupe_key(row) in keys or (row["Email ID"] and row["Email ID"].lower() in emails):
        notify("dup", "Already in your sheet - skipped", f"{row['POC']} | {row['Company Name']}")
        return

    if row["Missing Fields"] and SAVE_DEBUG_ON_MISSING:
        save_debug(text, html)

    pending = False
    try:
        moved = flush_pending()
        where = write_rows_any([row])
        SESSION_KEYS.add(_dedupe_key(row))
        if moved:
            print(f"  [+] Also added {moved} pending row(s).")
        status = "Saved" if where == "file" else "Saved in open Excel"
    except PermissionError as e:
        save_pending(row)
        pending = True
        status = f"Kept for later (Excel busy: {e})"

    if row["Missing Fields"]:
        hint = ""
        if "Linkedin Url" in row["Missing Fields"]:
            hint = f"\nLinkedIn missing: click its icon, then press {HOTKEY.upper()} on that LinkedIn tab"
        notify("warn", f"{status}: {row['POC'] or 'unknown'}",
               f"{row['Company Name']}\nMISSING: {row['Missing Fields']}{hint}")
    else:
        notify("warn" if pending else "ok", f"{status}: {row['POC']}",
               f"{row['Company Name']} | {row['Email ID'] or '-'}")


def linkedin_capture():
    """On the person's LinkedIn tab: put the tab's URL into the row captured last."""
    if not LAST_CAPTURE:
        notify("error", "Capture the ZoomInfo profile first",
               f"Press {HOTKEY.upper()} on the ZoomInfo profile, then open their LinkedIn and press "
               f"{LINKEDIN_HOTKEY.upper()}.")
        return
    poc = LAST_CAPTURE["poc"]
    raw = copy_address_bar()
    company_url = normalize_linkedin_company_url(raw)
    person_url = "" if company_url else normalize_linkedin_url(raw)
    if company_url:   # the company's page -> "Company Linkedin Url"
        field, url, who, label = "Company Linkedin Url", company_url, LAST_CAPTURE.get("company") or poc, "Company LinkedIn"
    elif person_url:  # the person's page -> "POC Linkedin Url"
        field, url, who, label = "POC Linkedin Url", person_url, poc, "LinkedIn"
    else:
        notify("error", "This tab is not a LinkedIn page",
               f"Open {poc}'s (or their company's) LinkedIn page, then press the hotkey again.")
        return
    if not linkedin_matches_name(who, foreground_title() + " " + url):
        again = _LI_REFUSED.get("url") == url and time.time() - _LI_REFUSED.get("t", 0) < 30
        if not again:
            _LI_REFUSED.update(url=url, t=time.time())
            notify("error", f"Does this match {who}? Not saved", f"{url}\nThis tab does not look like "
                   f"{who}. Press the hotkey again within 30 seconds to save it anyway.")
            return
    try:
        update_row_fields(LAST_CAPTURE["key"], {field: url})
    except LookupError as e:
        notify("error", "Could not find the row", f"{poc}: {e}")
        return
    except PermissionError as e:
        notify("error", "Excel is busy - LinkedIn not saved",
               f"{e}. Close any open cell or dialog and press the hotkey again.")
        return
    _LI_REFUSED.clear()
    notify("ok", f"{label} saved: {who}", url)


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


def main():
    if sys.platform != "win32":
        print("This tool runs on Windows. (Parser can be tested anywhere via test_parser.py)")
        return
    print("=" * 64)
    print(" ZoomInfo Capture Helper is running")
    print(f" Output file : {os.path.abspath(OUTPUT_FILE)}")
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
        print("  [!] Excel file is open. Pending rows will be added later.")
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
