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
- Safety: if the tab does not look like that person (name not in the tab title or URL),
  nothing is saved; press the hotkey again within 30 seconds to save it anyway.
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
