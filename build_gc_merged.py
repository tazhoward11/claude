import openpyxl
from openpyxl.styles import Font

SRC = "Austin office: Construction billings 2025"
# (rank, rank2, company_label) keyed by the company key used below
META = {
    "Swinerton": (21, 22, "Swinerton"),
    "Burton": (23, 17, "Burton Construction"),
    "Adolfson": (25, 38, "Adolfson & Peterson"),
    "Wurzel": (26, "", "Wurzel Builders"),
    "Zapalac": (28, 35, "ZapalacReed Construction"),
    "Roers": (29, 26, "Roers Companies"),
    "Lott": (30, 25, "Lott Brothers "),
    "Legacy": (31, 27, "Legacy MCS"),
    "HITT": (32, 41, "HITT Contracting"),
    "Rand": (33, 30, "Rand* Construction"),
    "Weis": (34, "", "Weis Builders"),
    "JayReese": (35, "", "Jay-Reese"),
    "Gordon": (36, 34, "Gordon Highlander"),
    "Andres": (38, 36, "Andres Construction"),
    "Headwater": (41, 39, "Headwater Construction"),
    "Paradigm": (43, 44, "Paradigm Commercial"),
    "GCreek": (44, "", "G. Creek Construction"),
}

# (key, email, name, title) - exactly as Taz has them today
EXISTING = [
    ("Swinerton", "snamuth@swinerton.com", "Susan Namuth", "Senior Office Manager"),
    ("Swinerton", "gladys.juarez@swinerton.com", "Gladys Juarez", "External Communications Manager, Regional"),
    ("Burton", "tjensen@burtonconstruction.com", "Tucker Jensen", "VP"),
    ("Adolfson", "mdeleon@a-p.com", "Megan DeLeon", "marketing and business development director"),
    ("Wurzel", "Barry.wurzel@wurzelbuilders.com", "Barry Wurzel", "President"),
    ("Wurzel", "business@wurzelbuilders.com", "Media Request, ABJ", ""),
    ("Zapalac", "sz@zapalacreed.com", "Shad Zapalac", "President"),
    ("Roers", "eden.garman@roerscompanies.com", "Eden Garman", "senior communications specialist"),
    ("Roers", "heather@roerscompanies.com", "Heather Ouellette", "director of corporate marketing"),
    ("Roers", "darren.marx@roerscompanies.com", "Darren Marx", "director of construction"),
    ("Lott", "WayneL@lottbrothers.com", "Wayne Lott", "Partner"),
    ("Lott", "DavidL@LottBrothers.com", "David Lott", "Partner"),
    ("Lott", "MWhite@LottBrothers.com", "Mike White", "COO/partner"),
    ("Lott", "JustinL@LottBrothers.com", "Justin Lott", "CEO/partner"),
    ("Lott", "karahk@lottbrothers.com", "Karah Kent", ""),
    ("Lott", "sarahh@lottbrothers.com", "Sarah Hunter", ""),
    ("Legacy", "gregory@legacymcs.com", "greg Cole", "COO"),
    ("Legacy", "cassbrewer@legacymcs.com", "Cass Brewer", "president"),
    ("HITT", "szelinger@hitt-gc.com", "Stacia Zelinger", "Senior Associate, Communications"),
    ("Rand", "", "Fred Noblett", "senior vice president"),
    ("Rand", "rtorres@randcc.com", "Rose Torres", "director of marketing and strategy"),
    ("Rand", "schmidt@randcc.com", "Tim Schmidt", "EVP"),
    ("Weis", "", "Derek Armstrong", "SVP"),
    ("JayReese", "caoueille@jayreese.net", "Chandra L. Aoueille", "secretary treasurer"),
    ("JayReese", "ralbee@jayreese.net", "Ron J. Albee", "President"),
    ("JayReese", "dalbee@jayreese.net", "Darcy Albee", "HR Manager"),
    ("Gordon", "cbailey@gordonhighlander.com", "Cody Bailey", "senior vice president"),
    ("Gordon", "sodonnell@gordonhighlander.com", "Steve O'Donnell", "CSO"),
    ("Gordon", "blitten@gordonhighlander.com", "Brooke Litten", "Director of Communications"),
    ("Andres", "jonathan@andresconstruction.com", "Jonathan Haywood", "VP, relationship development"),
    ("Andres", "chris@andresconstruction.com", "Chris Pulcini", "CFO"),
    ("Andres", "josh@andresconstruction.com", "Josh Torres", "VP"),
    ("Headwater", "jarrett@hw-companies.com", "Jarrett Dooley", "chief operating officer"),
    ("Headwater", "info@HW-companies.com", "Info Request", ""),
    ("Paradigm", "sarah@teamparadigm.com", "Sarah Sparrowhawk", "Business Operations Manager"),
    ("Paradigm", "reachus@teamparadigm.com", "Media Inquiry", ""),
    ("Paradigm", "", "Heather Merz", "CFO"),
    ("Paradigm", "", "Dylan Jensen", "principal"),
    ("Paradigm", "", "Joe O'Jibway", "VP of construction"),
    ("GCreek", "john@gcreek.com", "John Haralson", "president/co-owner"),
    ("GCreek", "matt@gcreek.com", "Matt Haralson", "VP/co-owner"),
    ("GCreek", "robert@gcreek.com", "Robert Petri", ""),
    ("GCreek", "Leo@gcreek.com", "Leo Cardenas", ""),
    ("GCreek", "haydn@gcreek.com", "Haydon Bush", ""),
]

# New from Apollo. status: "" = verified, "verify" = pattern-based
NEW = [
    ("Swinerton", "asatt@swinerton.com", "Alison Satt", "Vice President, Division Manager", ""),
    ("Swinerton", "sara.ballard@swinerton.com", "Sara Ballard", "Senior Internal Communications Manager", ""),
    ("Adolfson", "aautumn@a-p.com", "Amber Autumn", "Director of Business Development", ""),
    ("Wurzel", "brenda.jones@wurzelbuilders.com", "Brenda Jones", "Senior Director Business Development", ""),
    ("Wurzel", "john.mitchell@wurzelbuilders.com", "John Mitchell", "VP Field Ops", ""),
    ("Zapalac", "bz@zapalacreed.com", "Bill Zapalac", "Senior Vice President", ""),
    ("Zapalac", "hl@zapalacreed.com", "Hector Lopez", "Vice President", ""),
    ("Roers", "ray.castillo@roerscompanies.com", "Ray Castillo", "Director of Business Development", ""),
    ("Lott", "barretts@lottbrothers.com", "Barrett Schulz", "Vice President", ""),
    ("Lott", "josephn@lottbrothers.com", "Joseph Norrell", "Chief Financial Officer", ""),
    ("Legacy", "renee@legacydcs.com", "Renee Ernst", "VP of Admin", "check domain: legacydcs.com, not legacymcs.com"),
    ("HITT", "jmacks@hitt-gc.com", "Jennifer Macks", "Vice President and Business Unit Leader", ""),
    ("Rand", "rmcgovern@randcc.com", "Ryan McGovern", "Director", ""),
    ("Rand", "clardy@randcc.com", "Zachary Clardy", "Director of Field Operations", ""),
    ("Gordon", "lsawyer@gordonhighlander.com", "Lee Sawyer", "Vice President Business Development", ""),
    ("Gordon", "bjarrett@gordonhighlander.com", "Brian Jarrett", "Vice President of Business Development", ""),
    ("Andres", "tom@andresconstruction.com", "Tom Feather", "VP", ""),
    ("Headwater", "caren@hw-companies.com", "Caren Williams-Murch", "Vice President", ""),
    ("Headwater", "jennifer@hw-companies.com", "Jennifer Feikis", "Marketing Manager", ""),
    ("Headwater", "anson@hw-companies.com", "Anson Wa***g", "Director of Construction", "pattern-based - verify before sending"),
    ("Headwater", "wade@hw-companies.com", "Wade Ba***r", "Director of Engineering", "pattern-based - verify before sending"),
]

by_company = {}
for key, email, name, title in EXISTING:
    by_company.setdefault(key, []).append((email, name, title, "", ""))
for key, email, name, title, note in NEW:
    by_company.setdefault(key, []).append((email, name, title, "new - Apollo", note))

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "General contractors"
ws.append(["Category", "Rank", "Prior rank", "Source", "Company", "Email", "Name", "Title", "Status", "Note"])
for c in ws[1]:
    c.font = Font(bold=True)

for key in META:
    rank, prior, label = META[key]
    for email, name, title, status, note in by_company.get(key, []):
        ws.append(["General contractors", rank, prior, SRC, label, email, name, title, status, note])

widths = [18, 6, 10, 34, 26, 34, 22, 44, 12, 44]
for i, w in enumerate(widths, start=1):
    ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
ws.freeze_panes = "A2"
wb.save("gc_merged_list.xlsx")
print(ws.max_row - 1, "rows")
