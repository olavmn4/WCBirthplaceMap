#!/usr/bin/env python3
"""Audit squads data: list missing birthplaces and flag suspicious entries."""

import sys, json, re
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

parsed  = json.loads((HERE / "squads_parsed.json").read_text(encoding="utf-8"))
qids    = json.loads((HERE / "squads_qid_cache.json").read_text(encoding="utf-8"))
bp      = json.loads((HERE / "squads_bp_cache.json").read_text(encoding="utf-8"))

all_rows = []
for year_rows in parsed.values():
    all_rows.extend(year_rows)

# ── 1. Missing birthplaces ──────────────────────────────────────────────────
no_qid    = []   # article not resolved to a QID
no_bp     = []   # QID found but no birthplace in Wikidata

for row in all_rows:
    art = row["article"]
    qid = qids.get(art, "")
    if not qid:
        no_qid.append(row)
    else:
        bp_entry = bp.get(qid)
        if bp_entry is None:  # explicitly None = confirmed no birthplace
            no_bp.append(row)

missing = no_qid + no_bp
missing.sort(key=lambda r: (r["team"], r["year"], r["name"]))

print(f"Total rows: {len(all_rows)}")
print(f"No QID:     {len(no_qid)}")
print(f"No BP:      {len(no_bp)}")
print(f"Missing:    {len(missing)}\n")

print("=" * 70)
print("MISSING BIRTHPLACES")
print("=" * 70)
for r in missing:
    reason = "no QID" if not qids.get(r["article"], "") else "no birthplace in Wikidata"
    print(f"  {r['year']}  {r['team']:<30}  {r['name']:<35}  [{reason}]")
    if reason == "no QID":
        print(f"           article: {r['article']}")

# ── 2. Suspicious entries ────────────────────────────────────────────────────
# Club/org article patterns that slipped through the filter
CLUB_PAT = re.compile(
    r"(_F\.?C\.?|_AFC$|_RFC$|_SC$|_SK$|_Club$|_United$|_City$|"
    r"_Athletic$|_Sporting$|_Real_|Football_in_|_league|association_football)",
    re.I
)
POS_PAT  = re.compile(r"^(Goalkeeper|Midfielder|Defender|Forward|Winger|Sweeper)", re.I)

# Entries where the article clearly isn't a person
suspicious_art = [r for r in all_rows
                  if CLUB_PAT.search(r["article"]) or POS_PAT.match(r["article"])]

# Players appearing with implausibly many tournaments (>5 would be odd)
player_years = defaultdict(list)
for r in all_rows:
    player_years[(r["name"], r["team"])].append(r["year"])
multi = [(k, v) for k, v in player_years.items() if len(v) > 5]

# Names that look like clubs (contain FC, SC, etc. in the display name)
NAME_CLUB = re.compile(r"\b(F\.C\.|FC|S\.C\.|SC|SK|AC |RC |Real |Olimp|Dynamo|Sporting)\b")
suspicious_name = [r for r in all_rows if NAME_CLUB.search(r["name"])]

print("\n" + "=" * 70)
print("SUSPICIOUS: ARTICLE LOOKS LIKE A CLUB/CONCEPT")
print("=" * 70)
if suspicious_art:
    for r in sorted(suspicious_art, key=lambda r: r["year"]):
        print(f"  {r['year']}  {r['team']:<30}  {r['name']:<35}  {r['article']}")
else:
    print("  (none found — parser filter working correctly)")

print("\n" + "=" * 70)
print("SUSPICIOUS: DISPLAY NAME LOOKS LIKE A CLUB")
print("=" * 70)
if suspicious_name:
    for r in sorted(suspicious_name, key=lambda r: r["year"]):
        print(f"  {r['year']}  {r['team']:<30}  {r['name']:<35}  {r['article']}")
else:
    print("  (none found)")

print("\n" + "=" * 70)
print("SUSPICIOUS: SAME PLAYER+TEAM IN >5 TOURNAMENTS")
print("=" * 70)
if multi:
    for (name, team), years in sorted(multi):
        print(f"  {name} / {team}: {sorted(years)}")
else:
    print("  (none found)")
