"""
Quick parser test. Runs on any OS:  python test_parser.py
Sample text is modelled on what a ZoomInfo profile page looks like.
To test with YOUR real copy: paste it into my_copy.txt and run
    python test_parser.py my_copy.txt
"""
import sys
from zoominfo_capture import parse_profile

SAMPLE_WITH_EMAIL = """Sales
Search for companies, contacts, industries, etc.
Advanced Search
Navigation
Home
Advanced Search
Signals
Lists and records
Automations
Alerts
Contact Search
/
Neil Robinette
Suggest Update
Tag
Track Contact
Export
Neil Robinette
Chief Executive Officer
C.F. Smith Property Group
Website: www.cfsmithpg.com
Industry
Real Estate
Employees
11 - 50
Revenue
$7 Million
Contact Profile
Overview
Org Chart
Employees
Tabs
Contact Details
Emails
nrobinette@cfsmithpg.com (B)
Phone numbers
(910) 907-2544 (HQ)
(910) 417-7550 (M)
Location
Local
1005 E Broad Ave Ste 38, Rockingham, North Carolina, 28379, United States
HQ
100 Magnolia Rd Ste 200, Pinehurst, North Carolina, 28374, United States
Current Role Start Date: January 1, 2014
Tags
Add tag
About
Neil Robinette is the Chief Executive Officer at C.F. Smith Property Group
Similar Contacts
No Contacts Found
"""
SAMPLE_HTML = """Version:0.9
<html><body>
<a href="https://www.linkedin.com/in/neil-robinette-123/">li</a>
<a href="https://www.linkedin.com/company/cf-smith-property-group/">li</a>
</body></html>"""

SAMPLE_NO_EMAIL = """Contact Search
/
Ericka S. Black
Ericka S. Black
Chief Executive Officer At Ericka S. Black, REALTOR
Coldwell Banker Realty
Website
www.coldwellbankerhomes.com
Industry
Real Estate
Employees
11 - 50
Revenue
$3.1 Million
Contact Profile
Contact Details
Phone numbers
(888) 778-6995
(HQ)
(202) 670-6677
(M)
Location
Local
350 7th St SE, Washington, D.C., District of Columbia, 20003, United States
HQ
175 Park Ave, Madison, New Jersey, 07940, United States
Similar Contacts
James Knurek
"""


def show(title, row):
    print(f"\n--- {title} ---")
    for k, v in row.items():
        print(f"{k:22}: {v}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            show(sys.argv[1], parse_profile(f.read()))
    else:
        show("Profile with email", parse_profile(SAMPLE_WITH_EMAIL, SAMPLE_HTML))
        show("Profile without email", parse_profile(SAMPLE_NO_EMAIL))
