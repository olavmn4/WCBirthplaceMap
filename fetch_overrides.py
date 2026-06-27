#!/usr/bin/env python3
"""
Fetch correct birthplace data for missing / wrong players in a specific tournament.

Run:  python fetch_overrides.py --tournament 2022
      python fetch_overrides.py --tournament "Women" --year 2019
      python fetch_overrides.py --all     (all missing, newest first)

Writes / updates overrides.json. Then run:  python build_data.py
"""
import sys, json, re, time, unicodedata, argparse
from pathlib import Path
from collections import defaultdict
import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")

HERE       = Path(__file__).parent
GOALS_CSV  = HERE.parent / "worldcup-map" / "goals.csv"
CACHE_FILE = HERE / "footballers_cache.json"
SCORERS    = HERE / "scorers.js"
OUT_FILE   = HERE / "overrides.json"

WDAPI  = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
HEADS  = {"User-Agent": "WCBirthplaceMap/1.0 (personal-noncommercial)"}

FOOTBALL_KEYWORDS = (
    "football","soccer","footballer","futbol","association football",
    "forward","striker","midfielder","defender","goalkeeper","winger",
    "football player","soccer player",
)
NOT_PERSON = (
    "football club","f.c.","fc ","soccer club"," club",
    "association","federation","tournament","competition",
    "municipality","commune","arrondissement",
    "city","town","village","district","province",
    "country","nation","region","department",
    "company","organization","organisation",
    "television","newspaper","magazine","album","film","song",
    "given name","family name","surname","forename",
    "species","taxon","chemical","protein","person identified",
)

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

WPAPI = "https://en.wikipedia.org/w/api.php"

def _qid_from_wikipedia(name):
    """Search Wikipedia for name, return Wikidata QID from the top article."""
    try:
        # Step 1: opensearch to get article title
        r = requests.get(WPAPI, params={
            "action":"opensearch","search":name,"namespace":0,"limit":3,"format":"json"
        }, headers=HEADS, timeout=10)
        r.raise_for_status()
        titles = r.json()[1]
        if not titles:
            return None
        # Step 2: pageprops for top result → wikibase_item = QID
        r2 = requests.get(WPAPI, params={
            "action":"query","titles":titles[0],"prop":"pageprops","format":"json"
        }, headers=HEADS, timeout=10)
        r2.raise_for_status()
        pages = r2.json().get("query",{}).get("pages",{})
        for page in pages.values():
            qid = page.get("pageprops",{}).get("wikibase_item")
            if qid:
                return qid
    except Exception:
        pass
    return None

def search_qid(name):
    """Search wbsearchentities for name, with Wikipedia fallback."""
    # Pass 1: Wikidata entity search
    for query in [name, f"{name} footballer"]:
        for attempt in range(3):
            try:
                resp = requests.get(WDAPI, params={
                    "action":"wbsearchentities","search":query,
                    "language":"en","type":"item","limit":10,"format":"json"
                }, headers=HEADS, timeout=15)
                resp.raise_for_status()
                hits = resp.json().get("search",[])
                for hit in hits:
                    if any(kw in hit.get("description","").lower() for kw in FOOTBALL_KEYWORDS):
                        return hit["id"]
                for hit in hits:
                    if not any(kw in hit.get("description","").lower() for kw in NOT_PERSON):
                        return hit["id"]
                break  # API succeeded but no useful result — try next query variant
            except Exception:
                if attempt < 2: time.sleep(2 ** attempt)
    # Pass 2: Wikipedia article → Wikidata QID
    return _qid_from_wikipedia(name)

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
            resp = requests.get(SPARQL, params={"query":query,"format":"json"},
                                headers=HEADS, timeout=60)
            resp.raise_for_status()
            result = {}
            for b in resp.json()["results"]["bindings"]:
                qid  = b["player"]["value"].rsplit("/",1)[-1]
                city = b["birthPlaceLabel"]["value"]
                if is_qid(city): continue
                lat, lon = parse_point(b["coords"]["value"])
                if qid not in result:
                    result[qid] = {"city":city,"lat":lat,"lon":lon}
            return result
        except Exception as e:
            if attempt < 2: time.sleep(3)
    return {}

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--tournament", default=None, help="Year or keyword, e.g. 2022 or Women")
parser.add_argument("--all", action="store_true", help="Process all missing players")
args = parser.parse_args()

# ── Load scorers ──────────────────────────────────────────────────────────────
df = pd.read_csv(GOALS_CSV)
df = df[df["own_goal"]==0].copy()
df["player_name"] = df.apply(build_name, axis=1)
df["gender"]      = df["tournament_name"].apply(lambda t: "women" if "Women" in str(t) else "men")
df["year"]        = df["match_date"].str[:4].astype(int)
df["nkey"]        = df["player_name"].apply(norm)

scorers_all = (df.groupby(["tournament_name","year","gender","player_name","team_name","nkey"])
                 .size().reset_index(name="goals"))

# Filter to tournament if specified
if args.tournament and not args.all:
    mask = (scorers_all["tournament_name"].str.contains(args.tournament, case=False) |
            scorers_all["year"].astype(str).str.contains(args.tournament))
    scorers_filtered = scorers_all[mask]
    if scorers_filtered.empty:
        print(f"No tournament matching '{args.tournament}'. Available:")
        for t in scorers_all["tournament_name"].unique():
            print(f"  {t}")
        sys.exit(1)
    print(f"Tournament filter: '{args.tournament}' → {len(scorers_filtered)} scorer rows")
else:
    scorers_filtered = scorers_all
    print(f"Processing all {len(scorers_filtered)} scorer rows")

# Already-plotted nkeys
content = SCORERS.read_text(encoding="utf-8")
plotted = json.loads(content[content.index("["):content.rindex("]")+1])
plotted_nkeys = {norm(p["player"]) for p in plotted}

# Missing players from the filtered set
missing = scorers_filtered[~scorers_filtered["nkey"].isin(plotted_nkeys)]
missing = missing.sort_values(["year","goals"], ascending=[False,False])
print(f"Missing players: {len(missing)}")

# ── Existing overrides ────────────────────────────────────────────────────────
existing = {}
if OUT_FILE.exists():
    existing = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(existing)} existing overrides\n")

# ── Search for each missing player ───────────────────────────────────────────
targets = []
seen = set()
for _, row in missing.iterrows():
    nk = row["nkey"]
    if nk in seen or nk in existing:
        continue
    seen.add(nk)
    targets.append(row)

print(f"{len(targets)} new players to look up (skipping {len(missing)-len(targets)} already overridden)\n")

nkey_to_qid = {}
for i, row in enumerate(targets):
    qid = search_qid(row["player_name"])
    status = f"QID={qid}" if qid else "no match"
    tournament_short = row["tournament_name"].replace("FIFA ","").replace("World Cup","WC")
    print(f"  [{i+1:>3}/{len(targets)}] {row['player_name']:<28} ({row['team_name']}, {row['year']}, {row['goals']}g)  → {status}")
    if qid:
        nkey_to_qid[row["nkey"]] = (row["player_name"], row["team_name"], int(row["goals"]), qid)
    time.sleep(0.3)

# ── Batch SPARQL ──────────────────────────────────────────────────────────────
print(f"\nFetching birthplaces for {len(nkey_to_qid)} QIDs...")
qids = [v[3] for v in nkey_to_qid.values()]
birthplaces = batch_birthplaces(qids)
print(f"Got {len(birthplaces)}/{len(qids)} birthplaces\n")

# ── Write overrides ───────────────────────────────────────────────────────────
new_overrides = dict(existing)
added = 0
no_bp = []

for nk, (player, team, goals, qid) in nkey_to_qid.items():
    bp = birthplaces.get(qid)
    if bp:
        new_overrides[nk] = {
            "player": player, "team": team, "goals": goals, "qid": qid,
            "city": bp["city"], "lat": round(bp["lat"],5), "lon": round(bp["lon"],5),
        }
        print(f"  ✓ {player:<30} {bp['city']} ({bp['lat']:.3f}, {bp['lon']:.3f})")
        added += 1
    else:
        no_bp.append(f"{player} ({team}) QID={qid}")

OUT_FILE.write_text(json.dumps(new_overrides, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\n{added} new overrides added. Total: {len(new_overrides)}")
if no_bp:
    print(f"\nQID found but no birthplace in Wikidata ({len(no_bp)}):")
    for s in no_bp:
        print(f"  {s}")
print("\nRun:  python build_data.py  to rebuild with overrides.")
