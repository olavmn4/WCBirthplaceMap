#!/usr/bin/env python3
"""
Audit scorers.js by tournament — shows coverage and issues per WC edition.
Run:  python audit.py [tournament_filter]
e.g.  python audit.py 2022    (shows only 2022 WC)
      python audit.py          (shows all, newest first)
"""
import sys, json, re, unicodedata, argparse
from pathlib import Path
from collections import defaultdict
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

HERE      = Path(__file__).parent
GOALS_CSV = HERE.parent / "worldcup-map" / "goals.csv"
CACHE     = HERE / "footballers_cache.json"
SCORERS   = HERE / "scorers.js"

def norm(name):
    s = unicodedata.normalize("NFKD", str(name).strip())
    return re.sub(r"[^a-z0-9]", "", s.encode("ascii", "ignore").decode().lower())

def build_name(row):
    given  = str(row.get("given_name","")).strip()
    family = str(row.get("family_name","")).strip()
    if not given or given.lower() in ("not applicable","nan"):
        return family
    return f"{given} {family}"

# Bounding boxes for geo-mismatch check (team → lat_min, lat_max, lon_min, lon_max)
TEAM_BOXES = {
    "Argentina":    (-57,-20,-74,-52), "Australia":    (-45,-9,112,154),
    "Belgium":      (49,52,2,7),       "Bolivia":      (-23,-9,-69,-57),
    "Brazil":       (-34,6,-74,-34),   "Bulgaria":     (41,44,22,29),
    "Cameroon":     (1,13,8,16),       "Canada":       (41,84,-141,-52),
    "Chile":        (-56,-17,-76,-66), "China":        (18,53,73,135),
    "Colombia":     (-4,13,-79,-66),   "Costa Rica":   (8,12,-86,-82),
    "Croatia":      (42,46,13,19),     "Cuba":         (19,23,-85,-74),
    "Czech Republic":(48,52,12,19),    "Czechoslovakia":(47,52,12,23),
    "Denmark":      (54,58,8,16),      "Ecuador":      (-5,2,-81,-75),
    "Egypt":        (22,32,24,37),     "England":      (50,56,-6,2),
    "France":       (41,51,-5,9),      "Germany":      (47,55,6,15),
    "Greece":       (35,42,19,27),     "Hungary":      (45,48,16,23),
    "Iran":         (25,40,44,64),     "Italy":        (36,47,6,19),
    "Ivory Coast":  (4,11,-9,9),       "Jamaica":      (17,19,-78,-76),
    "Japan":        (24,46,122,146),   "Mexico":       (14,33,-118,-86),
    "Morocco":      (27,36,-14,-1),    "Netherlands":  (50,54,3,8),
    "Nigeria":      (4,14,3,15),       "Norway":       (57,72,4,32),
    "Paraguay":     (-28,-19,-63,-54), "Peru":         (-18,-0,-82,-68),
    "Poland":       (49,55,14,24),     "Portugal":     (36,42,-10,-6),
    "Romania":      (43,48,20,30),     "Russia":       (41,82,19,180),
    "Saudi Arabia": (16,33,34,56),     "Scotland":     (54,61,-8,-0),
    "Senegal":      (12,17,-18,-11),   "Serbia":       (42,46,19,23),
    "Slovakia":     (47,50,16,23),     "South Africa": (-35,-22,16,33),
    "South Korea":  (33,39,124,130),   "Soviet Union": (35,82,19,180),
    "Spain":        (35,44,-10,4),     "Sweden":       (55,70,10,25),
    "Switzerland":  (45,48,5,11),      "Turkey":       (35,43,25,45),
    "Ukraine":      (44,53,22,40),     "United States":(24,50,-125,-66),
    "Uruguay":      (-35,-30,-59,-53), "Wales":        (51,54,-5,-2),
    "West Germany": (47,55,6,15),      "Yugoslavia":   (40,47,13,23),
    "China PR":     (18,53,73,135),    "Korea Republic":(33,39,124,130),
    "Korea DPR":    (37,43,124,130),
}
DIASPORA_TEAMS = {
    "France","Netherlands","Germany","Belgium","Portugal","Switzerland",
    "Sweden","England","Australia","United States","Canada",
    "Ivory Coast","Senegal","Nigeria","Cameroon",
}

def geo_mismatch(team, lat, lon):
    if team in DIASPORA_TEAMS:
        return False
    box = TEAM_BOXES.get(team)
    if not box:
        return False
    la0,la1,lo0,lo1 = box
    buf = 15
    return not (la0-buf <= lat <= la1+buf and lo0-buf <= lon <= lo1+buf)

# ── Load data ─────────────────────────────────────────────────────────────────
df = pd.read_csv(GOALS_CSV)
df = df[df["own_goal"]==0].copy()
df["player_name"] = df.apply(build_name, axis=1)
df["gender"]      = df["tournament_name"].apply(lambda t: "women" if "Women" in str(t) else "men")
df["year"]        = df["match_date"].str[:4].astype(int)
df["nkey"]        = df["player_name"].apply(norm)

# Unique scorers per tournament
scorers_all = (df.groupby(["tournament_name","year","gender","player_name","team_name","nkey"])
                 .size().reset_index(name="goals"))

# All plotted players
content  = SCORERS.read_text(encoding="utf-8")
plotted  = json.loads(content[content.index("["):content.rindex("]")+1])
plotted_nkeys = {norm(p["player"]) for p in plotted}
plotted_map   = {norm(p["player"]): p for p in plotted}

# Collision index
cache = json.loads(CACHE.read_text(encoding="utf-8"))
nkey_cands = defaultdict(list)
for e in cache:
    nk = norm(e["label"])
    if nk:
        nkey_cands[nk].append(e)

# Overrides index (collision resolved if override exists)
OVERRIDES_FILE = HERE / "overrides.json"
overridden_nkeys = set()
if OVERRIDES_FILE.exists():
    overridden_nkeys = set(json.loads(OVERRIDES_FILE.read_text(encoding="utf-8")).keys())

# ── Tournaments sorted newest → oldest ───────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("filter", nargs="?", default=None, help="Year or keyword filter")
args = parser.parse_args()

tournaments = (scorers_all.groupby(["tournament_name","year","gender"])
               .size().reset_index()
               .sort_values("year", ascending=False))

if args.filter:
    mask = (tournaments["tournament_name"].str.contains(args.filter, case=False) |
            tournaments["year"].astype(str).str.contains(args.filter))
    tournaments = tournaments[mask]

# ── Print report ──────────────────────────────────────────────────────────────
print("=" * 72)
print("WORLD CUP BIRTHPLACE MAP — TOURNAMENT AUDIT")
print("=" * 72)

for _, t_row in tournaments.iterrows():
    tname  = t_row["tournament_name"]
    tyear  = t_row["year"]
    tgender= t_row["gender"]

    scorers = scorers_all[scorers_all["tournament_name"]==tname].copy()
    total   = len(scorers)
    plotted_t = scorers[scorers["nkey"].isin(plotted_nkeys)]
    pct     = 100 * len(plotted_t) / total if total else 0

    print(f"\n{'─'*72}")
    print(f"  {tname}  [{tgender}]  —  {len(plotted_t)}/{total} plotted ({pct:.0f}%)")
    print(f"{'─'*72}")

    # Missing
    missing = scorers[~scorers["nkey"].isin(plotted_nkeys)].sort_values("goals", ascending=False)
    if not missing.empty:
        print(f"\n  MISSING ({len(missing)}):")
        for _, r in missing.iterrows():
            print(f"    {r['goals']:>2}g  {r['team_name']:<22}  {r['player_name']}")

    # Geo-mismatches and collisions among plotted
    issues = []
    for _, r in plotted_t.iterrows():
        p = plotted_map.get(r["nkey"])
        if not p: continue
        flags = []
        if geo_mismatch(r["team_name"], p["lat"], p["lon"]):
            flags.append(f"GEO? born {p['city']} ({p['lat']:.1f},{p['lon']:.1f})")
        cands = nkey_cands.get(r["nkey"],[])
        if len(cands) > 1 and r["nkey"] not in overridden_nkeys:
            cities = list({c["city"] for c in cands})
            if len(cities) > 1:
                flags.append(f"COLLISION? candidates: {', '.join(cities[:3])}")
        if flags:
            issues.append((r["goals"], r["player_name"], r["team_name"], flags))

    if issues:
        issues.sort(key=lambda x: -x[0])
        print(f"\n  POTENTIAL ISSUES ({len(issues)}):")
        for goals, player, team, flags in issues:
            for f in flags:
                print(f"    {goals:>2}g  {team:<22}  {player}  [{f}]")
