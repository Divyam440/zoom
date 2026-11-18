# ZoomInfo Capture Helper
Turns a manually copied ZoomInfo profile into one clean Excel row.

## Setup (Windows, one time)
1. Install Python 3.10+ (tick "Add Python to PATH").
2. Put this folder anywhere, e.g. `D:\lead_tool`.
3. Open Command Prompt in the folder and run:
   `pip install -r requirements.txt`

## Daily use
1. Run: `python zoominfo_capture.py` (keep the window open, it can stay in the background).
2. In ZoomInfo, open a contact profile.
3. Click an **empty area of the page** (not a search box), then press **Ctrl+Alt+Z**.
4. The tool presses Ctrl+A, Ctrl+C for you, adds the row to `leads_master.xlsx`, and a
   small popup (bottom-right, never steals focus) shows the result:
   green = saved, amber = saved but fields missing (they are listed),
   grey = already in your sheet, red = nothing saved (and why).
   Click a popup to dismiss it.
Prefer to copy yourself? Set `AUTO_COPY = False` at the top of the script, then
press Ctrl+A, Ctrl+C before the hotkey. `POPUP = False` turns the popup off.

## LinkedIn URL of the person
1. Capture the ZoomInfo profile as usual (**Ctrl+Alt+Z**).
2. If "POC Linkedin Url" is listed under Missing Fields, click the LinkedIn icon on
   the ZoomInfo profile. LinkedIn opens in a new tab.
3. On that LinkedIn tab press **Ctrl+Alt+Z again** (the same hotkey: on a LinkedIn tab
   it saves the URL, anywhere else it captures the profile; Ctrl+Alt+L also works).
   The tool copies the tab's address,
   writes it into that person's row (the one captured last), and removes
   "POC Linkedin Url" from Missing Fields (the yellow goes away when nothing else is missing).
- Works with the Excel file open or closed.
- It only reads the address bar of the tab you are looking at. It never reads the LinkedIn page.
- The tool finds the right row by the NAME in the LinkedIn tab title, not by "last captured":
  a person's tab fills "POC Linkedin Url" of the row with that POC, a company's tab fills
  "Company Linkedin Url" of the row with that Company Name. So you can do the LinkedIn
  steps in any order, even later. If no row matches the tab's name, nothing is saved
  and the popup says so (capture their ZoomInfo profile first).
- A COMPANY's LinkedIn tab fills "Company Linkedin Url" for EVERY row of that company (all the
  people you captured there), so you do it once per company. When you capture another person
  of a company that already has its URL in the sheet, the URL is filled in automatically.
- Company Phone is the (HQ) number only. A person's direct (D/B/O) line is never put there;
  if the profile has no (HQ) number it is saved in Notes as "Direct phone: ..." and Company
  Phone stays empty and is flagged.
- Excel can stay open, even with unsaved changes: the tool edits the open window.
- Missing Fields and the yellow colour are refreshed by the tool. If you type a value into
  the sheet yourself, they do not update on their own.
- `python check_links.py` shows whether ZoomInfo's copy contains LinkedIn links at all.
- A link taken from the ZoomInfo copy is only accepted when it contains the person's
  name, so a "Similar Contacts" link can never land in the wrong row.

## Duplicate pre-check (saves view credits)
Do this BEFORE opening profiles, because opening a profile uses a view credit.
1. Open your saved search (results list).
2. Click an empty area, then press **Ctrl+Alt+X**.
3. The popup lists people on that page who are already in `leads_master.xlsx`.
   Skip those, open only the rest.
Same name can be a different person, so check the company shown before skipping.

## Behaviour
- Columns: your 11 headers in order, then Mobile Phone, POC Linkedin Url,
  Missing Fields, Call Status, Verified, Notes.
- Missing data: cell left empty, row highlighted yellow, listed in "Missing Fields".
- Duplicates: same POC + Company, or same email, is skipped (saves re-work).
- Excel open: you can keep `leads_master.xlsx` open in desktop Excel. The row is
  written live into the open window (yellow if fields are missing) and the file
  is saved for you. If you have unsaved edits of your own, the tool leaves saving
  to you. Do not capture while a cell is in edit mode or a dialog is open.
  If live writing is not possible (Excel busy, file read-only, or opened in a
  different app) the row goes to `pending_rows.csv` and is added automatically
  on a later capture.
- Debug: when a field is missing, the raw copy is saved in `debug_captures/`.
  These files contain contact data, so keep them private and clear them regularly.

## Testing the parser with a real copy
Paste a copied profile into `my_copy.txt` and run:
`python test_parser.py my_copy.txt`

## Rules
- The tool never touches ZoomInfo. A person opens every profile and presses the hotkey.
  The only keys it sends are Ctrl+A, Ctrl+C, and only right after you press the hotkey.
- No ZoomInfo API call and no bulk credit is used by this tool.
- Do not add auto-trigger on page load, auto-clicking or page scraping.

## The Excel sheet
- Layout is applied automatically: dark-blue header, frozen header row + Company Name column,
  filter buttons, dropdowns for **Call Status** (Not called, No answer, Call back, Interested,
  Not interested, Meeting booked, Wrong number) and **Verified** (Yes/No), clickable Website,
  LinkedIn and Email cells, yellow rows = something missing. Column widths you change are kept.
- Rows always go into the **Leads** tab, under the right heading, even if you switched tabs,
  moved/inserted columns, or left formatted blank rows at the bottom.
- When Excel is busy for a moment (autosave, recalculation) the tool waits and retries.
- `python zoominfo_capture.py --tidy` (Excel closed) re-applies the layout to the whole sheet,
  refreshes Missing Fields + yellow after you typed values by hand, and lists duplicate people.
  This also runs by itself every time the tool starts while the sheet is closed.

## Safety net (nothing gets lost)
- Every capture (and every LinkedIn URL added later) is first written to `captured_log.csv`,
  which is never deleted. All files live next to the script, whatever folder you start it from.
- `backups/` keeps up to 50 copies of `leads_master.xlsx` (at most one per 5 minutes).
- Saves are atomic: a crash or power cut while saving can't break the sheet. If the sheet is
  ever damaged anyway, the tool keeps it as `leads_master.DAMAGED_<time>.xlsx`, puts the newest
  backup back and re-adds every logged capture by itself.
- Rows missing from the sheet? Close Excel, run `python zoominfo_capture.py --restore`.
- Rows written into an open Excel window are saved right away. If Excel refuses to save, the
  popup says "press Ctrl+S there".
- Something went wrong? Full details are in `capture_errors.log`. Send that file when asking for help.
