"""
Does ZoomInfo's copied page contain the LinkedIn links at all?

Run after a capture that showed "MISSING ... Linkedin Url":
    python check_links.py

It reads the newest file in debug_captures/ (saved automatically when a field is
missing) and prints what links the copy contained. Nothing is sent anywhere.
"""
import glob
import os
import re
import sys
from urllib.parse import unquote

folder = sys.argv[1] if len(sys.argv) > 1 else "debug_captures"
htmls = sorted(glob.glob(os.path.join(folder, "*.html")), key=os.path.getmtime)
txts = sorted(glob.glob(os.path.join(folder, "*.txt")), key=os.path.getmtime)

if not txts and not htmls:
    print(f"No captures in '{folder}'. Capture a profile that has a missing field first.")
    sys.exit(0)

if not htmls:
    print("The newest capture has TEXT only, no HTML. Windows did not give the tool the page's")
    print("links, so LinkedIn links cannot be read from the copy. Use the LinkedIn hotkey instead.")
    sys.exit(0)

path = htmls[-1]
html = unquote(open(path, encoding="utf-8", errors="ignore").read())
hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", html, flags=re.I)
social = [h for h in hrefs if re.search(r"linkedin|facebook|twitter|x\.com", h, re.I)]
print(f"File: {path}")
print(f"Links (<a href>) in the copy: {len(hrefs)}")
print(f"Word 'linkedin' appears {len(re.findall('linkedin', html, flags=re.I))} times")
if social:
    print("Social links found:")
    for h in dict.fromkeys(social):
        print("   ", h[:110])
    print("-> The links ARE in the copy, so the tool can read them. Send this output to Claude if the")
    print("   sheet still shows them missing.")
else:
    print("No LinkedIn/Facebook/X links in the copy.")
    print("-> ZoomInfo opens those icons with a script, not a normal link, so the copy cannot carry them.")
    print("   Use the LinkedIn step: click the icon, then press the hotkey on the LinkedIn tab.")
