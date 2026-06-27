#!/usr/bin/env python3
"""
Fix POTENTIAL ISSUES (GEO mismatches + collisions) by looking up each flagged
player on English Wikipedia, getting their Wikidata QID from the linked article,
then fetching the correct birthplace from Wikidata.

Run:  python fix_issues.py           (all tournaments)
      python fix_issues.py 2022      (single tournament)
      python fix_issues.py men       (all men's)
"""
import sys, json, re, time, unicodedata, argparse
from pathlib import Path
from collections import defaultdict
import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")

HERE      = Path(__file__).parent
GOALS_CSV = HERE.parent / "worldcup-map" / "goals.csv"
CACHE_FILE= HERE / "footballers_cache.json"
SCORERS   = HERE / "scorers.js"
OUT_FILE  = HERE / "overrides.json"

WPAPI  = "https://en.wikipedia.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
HEADS  = {"User-Agent": "WCBirthplaceMap/1.0 (personal-noncommercial)"}

# Teams where foreign birthplaces are expected (skip GEO check for these)
DIASPORA_TEAMS = {
    "France","Netherlands","Germany","Belgium","Portugal","Switzerland",
    "Sweden","England","Australia","United States","Canada",
    "Ivory Coast","Senegal","Nigeria","Cameroon",
}

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

def norm(name):
    s = unicodedata.normalize("NFKD", str(name).strip())
    return re.sub(r"[^a-z0-9]", "", s.encode("ascii", "ignore").decode().lower())

def build_name(row):
    given  = str(row.get("given_name","")).strip()
    family = str(row.get("family_name","")).strip()
    if not given or given.lower() in ("not applicable","nan"):
        return family
    return f"{given} {family}"

def parse_point(raw):
    parts = raw.replace("Point(","").replace(")","").split()
    return float(parts[1]), float(parts[0])

def is_qid(s):
    return bool(re.match(r"^Q\d+$", s))

def geo_mismatch(team, lat, lon):
    if team in DIASPORA_TEAMS:
        return False
    box = TEAM_BOXES.get(team)
    if not box:
        return False
    la0,la1,lo0,lo1 = box
    buf = 15
    return not (la0-buf <= lat <= la1+buf and lo0-buf <= lon <= lo1+buf)


def qid_from_wikipedia(name, team=None):
    """Search English Wikipedia for player name; return Wikidata QID from the article."""
    queries = [name]
    if team:
        queries.append(f"{name} footballer")
        queries.append(f"{name} {team}")

    for q in queries:
        try:
            r = requests.get(WPAPI, params={
                "action": "opensearch", "search": q,
                "namespace": 0, "limit": 5, "format": "json"
            }, headers=HEADS, timeout=10)
            r.raise_for_status()
            titles = r.json()[1]
            if not titles:
                continue

            # Try each title; prefer ones that look like people (not clubs/tournaments)
            skip_words = ("F.C.","FC ","Club","Tournament","Championship","Stadium",
                          "Association","Federation","Cup","League")
            person_titles = [t for t in titles if not any(w in t for w in skip_words)]
            candidates = person_titles if person_titles else titles

            for title in candidates[:3]:
                r2 = requests.get(WPAPI, params={
                    "action": "query", "titles": title,
                    "prop": "pageprops", "format": "json"
                }, headers=HEADS, timeout=10)
                r2.raise_for_status()
                pages = r2.json().get("query", {}).get("pages", {})
                for page in pages.values():
                    qid = page.get("pageprops", {}).get("wikibase_item")
                    if qid:
                        return qid, title
        except Exception:
            time.sleep(1)
    return None, None


def batch_birthplaces(qids):
    if not qids:
        return {}
    values = " ".join(f"wd:{q}" for q in qids)
    query = (
        "SELECT ?player ?birthPlaceLabel ?coords WHERE {"
        f"  VALUES ?player {{ {values} }}"
        "  ?player wdt:P19 ?birthPlace ."
        "  ?birthPlace wdt:P625 ?coords ."
        '  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }'
        "}"
    )
    for attempt in range(3):
        try:
            resp = requests.get(SPARQL, params={"query": query, "format": "json"},
                                headers=HEADS, timeout=60)
            resp.raise_for_status()
            result = {}
            for b in resp.json()["results"]["bindings"]:
                qid  = b["player"]["value"].rsplit("/", 1)[-1]
                city = b["birthPlaceLabel"]["value"]
                if is_qid(city):
                    continue
                lat, lon = parse_point(b["coords"]["value"])
                if qid not in result:
                    result[qid] = {"city": city, "lat": lat, "lon": lon}
            return result
        except Exception:
            if attempt < 2:
                time.sleep(3)
    return {}


# ── Load base data ─────────────────────────────────────────────────────────────
df = pd.read_csv(GOALS_CSV)
df = df[df["own_goal"] == 0].copy()
df["player_name"] = df.apply(build_name, axis=1)
df["gender"]      = df["tournament_name"].apply(lambda t: "women" if "Women" in str(t) else "men")
df["year"]        = df["match_date"].str[:4].astype(int)
df["nkey"]        = df["player_name"].apply(norm)

scorers_all = (df.groupby(["tournament_name","year","gender","player_name","team_name","nkey"])
                 .size().reset_index(name="goals"))

content      = SCORERS.read_text(encoding="utf-8")
plotted      = json.loads(content[content.index("["):content.rindex("]")+1])
plotted_map  = {norm(p["player"]): p for p in plotted}
plotted_nkeys= set(plotted_map.keys())

cache      = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
nkey_cands = defaultdict(list)
for e in cache:
    nk = norm(e["label"])
    if nk:
        nkey_cands[nk].append(e)

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("filter", nargs="?", default=None,
                    help="Year or keyword filter (e.g. 2022, men, women)")
args = parser.parse_args()

tournaments = (scorers_all.groupby(["tournament_name","year","gender"])
               .size().reset_index()
               .sort_values("year", ascending=False))

if args.filter:
    mask = (tournaments["tournament_name"].str.contains(args.filter, case=False) |
            tournaments["year"].astype(str).str.contains(args.filter) |
            tournaments["gender"].str.contains(args.filter, case=False))
    tournaments = tournaments[mask]

# ── Collect all POTENTIAL ISSUES ──────────────────────────────────────────────
issues = []   # (player_name, team, goals, nkey, tournament_name, year, flags)
seen_nkeys = set()

for _, t_row in tournaments.iterrows():
    tname = t_row["tournament_name"]
    scorers = scorers_all[scorers_all["tournament_name"] == tname].copy()
    plotted_t = scorers[scorers["nkey"].isin(plotted_nkeys)]

    for _, r in plotted_t.iterrows():
        nk = r["nkey"]
        p  = plotted_map.get(nk)
        if not p or nk in seen_nkeys:
            continue

        flags = []
        if geo_mismatch(r["team_name"], p["lat"], p["lon"]):
            flags.append(f"GEO born {p['city']} ({p['lat']:.1f},{p['lon']:.1f})")
        cands = nkey_cands.get(nk, [])
        cities = list({c["city"] for c in cands})
        if len(cands) > 1 and len(cities) > 1:
            flags.append(f"COLLISION: {', '.join(cities[:3])}")

        if flags:
            seen_nkeys.add(nk)
            issues.append({
                "player": r["player_name"],
                "team":   r["team_name"],
                "goals":  int(r["goals"]),
                "nkey":   nk,
                "tournament": tname,
                "year":   int(t_row["year"]),
                "flags":  flags,
            })

print(f"Found {len(issues)} unique players with potential issues.\n")

# ── Load existing overrides ────────────────────────────────────────────────────
existing = {}
if OUT_FILE.exists():
    existing = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(existing)} existing overrides.\n")

# ── Wikipedia lookup for each ─────────────────────────────────────────────────
nkey_to_qid = {}   # nkey → (player, team, goals, qid, wiki_title)

for i, iss in enumerate(issues):
    name  = iss["player"]
    team  = iss["team"]
    nk    = iss["nkey"]
    flags = ", ".join(iss["flags"])

    qid, wiki_title = qid_from_wikipedia(name, team)
    status = f"QID={qid} (via '{wiki_title}')" if qid else "no Wikipedia match"
    print(f"  [{i+1:>3}/{len(issues)}] {name:<30} ({team})  {flags}")
    print(f"           → {status}")

    if qid:
        nkey_to_qid[nk] = (iss["player"], iss["team"], iss["goals"], qid)
    time.sleep(0.4)

# ── Batch birthplace lookup ───────────────────────────────────────────────────
print(f"\nFetching birthplaces for {len(nkey_to_qid)} QIDs from Wikidata...")
qids = [v[3] for v in nkey_to_qid.values()]
birthplaces = batch_birthplaces(qids)
print(f"Got {len(birthplaces)}/{len(qids)} birthplaces.\n")

# ── Write overrides ───────────────────────────────────────────────────────────
new_overrides = dict(existing)
added = updated = 0
no_bp = []

for nk, (player, team, goals, qid) in nkey_to_qid.items():
    bp = birthplaces.get(qid)
    if not bp:
        no_bp.append(f"{player} ({team}) QID={qid}")
        continue

    old = existing.get(nk)
    entry = {
        "player": player, "team": team, "goals": goals, "qid": qid,
        "city": bp["city"], "lat": round(bp["lat"], 5), "lon": round(bp["lon"], 5),
    }
    if old:
        if old.get("city") != bp["city"] or old.get("qid") != qid:
            print(f"  UPDATED  {player:<30} {old.get('city','?')} → {bp['city']} ({bp['lat']:.3f},{bp['lon']:.3f})")
            updated += 1
        else:
            print(f"  same     {player:<30} {bp['city']} (no change)")
    else:
        print(f"  NEW      {player:<30} {bp['city']} ({bp['lat']:.3f},{bp['lon']:.3f})")
        added += 1

    new_overrides[nk] = entry

OUT_FILE.write_text(json.dumps(new_overrides, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\n{added} new + {updated} updated overrides. Total: {len(new_overrides)}")
if no_bp:
    print(f"\nWikipedia QID found but no Wikidata birthplace ({len(no_bp)}):")
    for s in no_bp:
        print(f"  {s}")
print("\nRun:  python build_data.py  to rebuild scorers.js")
