#!/usr/bin/env python3
"""
Build World Cup goal-scorer birthplace dataset.

Run once:  python build_data.py
Then open: index.html

Strategy (v2 — bulk SPARQL):
  1. Load goals (men's 1930-2022, women's 1991-2019).
  2. Bulk-fetch ALL Wikidata association footballers who have a birthplace
     with coordinates, in paginated SPARQL queries (~100K rows total).
     Cache result in footballers_cache.json so reruns are instant.
  3. Match our 1768 scorers against the bulk dataset by normalised name.
  4. Write scorers.js (loaded by index.html directly via <script src>).

Flags:
  --fresh   Ignore cache, re-fetch from Wikidata.
"""

import sys
import json
import re
import time
import unicodedata
import argparse
from pathlib import Path

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")

HERE      = Path(__file__).parent
GOALS_CSV = HERE.parent / "worldcup-map" / "goals.csv"
OUTPUT_JS = HERE / "scorers.js"
CACHE_FILE     = HERE / "footballers_cache.json"
OVERRIDES_FILE = HERE / "overrides.json"

SPARQL_URL = "https://query.wikidata.org/sparql"
HEADERS    = {"User-Agent": "WCBirthplaceMap/1.0 (personal-noncommercial)"}

PAGE_SIZE  = 50_000   # rows per SPARQL page
MAX_PAGES  = 8        # safety cap: 8 × 50K = 400K rows


# ─── Helpers ──────────────────────────────────────────────────────────────────
def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name).strip())
    return re.sub(r"[^a-z0-9]", "", s.encode("ascii", "ignore").decode().lower())


def build_name(row) -> str:
    given  = str(row.get("given_name",  "")).strip()
    family = str(row.get("family_name", "")).strip()
    if not given or given.lower() in ("not applicable", "nan"):
        return family
    return f"{given} {family}"


def parse_point(raw: str) -> tuple[float, float]:
    """'Point(lon lat)' -> (lat, lon)"""
    parts = raw.replace("Point(", "").replace(")", "").split()
    return float(parts[1]), float(parts[0])


def is_qid(s: str) -> bool:
    return bool(re.match(r"^Q\d+$", s))


# ─── Bulk SPARQL fetch ─────────────────────────────────────────────────────────
def fetch_footballer_page(offset: int) -> list[dict]:
    """One page of association footballers with birthplace coords."""
    query = f"""
SELECT ?player ?playerLabel ?birthPlaceLabel ?coords WHERE {{
  ?player wdt:P106 wd:Q937857 ;
          wdt:P19  ?birthPlace .
  ?birthPlace wdt:P625 ?coords .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {PAGE_SIZE}
OFFSET {offset}
"""
    for attempt in range(4):
        try:
            resp = requests.get(
                SPARQL_URL,
                params={"query": query, "format": "json"},
                headers=HEADERS,
                timeout=120,
            )
            resp.raise_for_status()
            bindings = resp.json()["results"]["bindings"]
            results = []
            for b in bindings:
                label = b.get("playerLabel", {}).get("value", "")
                city  = b.get("birthPlaceLabel", {}).get("value", "")
                raw   = b.get("coords", {}).get("value", "")
                if not label or not raw or is_qid(city):
                    continue
                try:
                    lat, lon = parse_point(raw)
                except Exception:
                    continue
                results.append({"label": label, "city": city, "lat": lat, "lon": lon})
            return results
        except Exception as exc:
            wait = 2 ** attempt
            print(f"    [!] Attempt {attempt+1} failed: {exc}  (retry in {wait}s)")
            time.sleep(wait)
    return []


def fetch_all_footballers() -> list[dict]:
    """Paginate through Wikidata to get all footballers with birthplace coords."""
    all_rows: list[dict] = []
    for page in range(MAX_PAGES):
        offset = page * PAGE_SIZE
        print(f"  Fetching page {page+1}  (offset={offset})...", end=" ", flush=True)
        rows = fetch_footballer_page(offset)
        print(f"{len(rows)} rows")
        all_rows.extend(rows)
        if len(rows) < PAGE_SIZE:
            break  # last page
        time.sleep(3)
    return all_rows


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="Re-fetch Wikidata")
    args = parser.parse_args()

    # 1. Load goals
    print("Loading goals.csv...")
    df = pd.read_csv(GOALS_CSV)
    df = df[df["own_goal"] == 0].copy()
    df["player_name"] = df.apply(build_name, axis=1)
    df["gender"] = df["tournament_name"].apply(
        lambda t: "women" if "Women" in str(t) else "men"
    )
    df["tournament_year"] = df["tournament_name"].str.extract(r"(\d{4})").astype(int)
    scorers = (
        df.groupby(["player_name", "team_name", "gender", "tournament_year"])
        .size()
        .reset_index(name="goals")
    )
    scorers["nkey"] = scorers["player_name"].apply(norm)
    total = len(scorers)
    unique_players = scorers[["player_name", "team_name", "gender"]].drop_duplicates().shape[0]
    print(f"  {total} player-tournament entries ({unique_players} unique players, own goals excluded)")

    # 2. Load or fetch bulk footballer data
    if not args.fresh and CACHE_FILE.exists():
        print(f"\nStep 1/3 — Loading footballer cache ({CACHE_FILE.name})...")
        footballers: list[dict] = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        print(f"  {len(footballers):,} entries loaded")
    else:
        print("\nStep 1/3 — Fetching all Wikidata footballers with birthplace...")
        footballers = fetch_all_footballers()
        print(f"  Total: {len(footballers):,} footballer-birthplace entries")
        CACHE_FILE.write_text(json.dumps(footballers, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        print(f"  Cache saved: {CACHE_FILE.name}")

    # 3. Build nkey -> best birthplace index
    #    Multiple Wikidata entries can share the same nkey (e.g. many "John Smith").
    #    We keep the first one encountered (Wikidata ordering is quasi-stable).
    #    For the most famous players this is usually the top result.
    print("\nStep 2/3 — Building name -> birthplace index...")
    nkey_index: dict[str, dict] = {}
    for entry in footballers:
        nk = norm(entry["label"])
        if nk and nk not in nkey_index:
            nkey_index[nk] = {"city": entry["city"], "lat": entry["lat"], "lon": entry["lon"]}

    print(f"  {len(nkey_index):,} unique normalised names in index")

    # Apply overrides — these win over the bulk index for corrections and additions
    if OVERRIDES_FILE.exists():
        overrides: dict[str, dict] = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        for nk, entry in overrides.items():
            nkey_index[nk] = {"city": entry["city"], "lat": entry["lat"], "lon": entry["lon"]}
        print(f"  {len(overrides)} overrides applied")
    else:
        print("  No overrides.json found — run fetch_overrides.py to generate one")

    # 4. Match scorers against index
    scorers["city"] = None
    scorers["lat"]  = None
    scorers["lon"]  = None
    for idx, row in scorers.iterrows():
        hit = nkey_index.get(row["nkey"])
        if hit:
            scorers.at[idx, "city"] = hit["city"]
            scorers.at[idx, "lat"]  = hit["lat"]
            scorers.at[idx, "lon"]  = hit["lon"]

    matched = scorers["lat"].notna().sum()
    print(f"  {matched}/{total} scorers matched ({100*matched/total:.1f}%)")

    # 4.5 Reverse-geocode each unique birthplace coord → ISO alpha-2 country code.
    #     Results cached in birth_country_cache.json so reruns are instant.
    print("\nStep 2.5/3 — Tagging birth countries (cached Nominatim reverse geocode)...")
    CC_CACHE_FILE = HERE / "birth_country_cache.json"
    cc_cache: dict[str, str] = (
        json.loads(CC_CACHE_FILE.read_text(encoding="utf-8"))
        if CC_CACHE_FILE.exists() else {}
    )

    NOM_URL = "https://nominatim.openstreetmap.org/reverse"

    UK_STATES = {"England": "GB-ENG", "Scotland": "GB-SCO",
                 "Wales": "GB-WAL", "Northern Ireland": "GB-NIR"}

    # Nominatim's ISO3166-2-lvl4 codes for UK nations (differ from FIFA codes for SCO/WAL)
    NOM_UK_ISO4 = {"GB-ENG": "GB-ENG", "GB-SCT": "GB-SCO",
                   "GB-WLS": "GB-WAL", "GB-NIR": "GB-NIR"}

    def _reverse_cc(lat: float, lon: float) -> str:
        key = f"{lat:.3f},{lon:.3f}"
        # Re-fetch if previously cached as bare "GB" (subdivision not yet resolved)
        if key in cc_cache and cc_cache[key] != "GB":
            return cc_cache[key]
        try:
            r = requests.get(
                NOM_URL,
                params={"lat": lat, "lon": lon, "format": "json", "zoom": 5},
                headers=HEADERS,
                timeout=12,
            )
            r.raise_for_status()
            data = r.json().get("address", {})
            cc = data.get("country_code", "").upper()
            if cc == "GB":
                # state field works for England, Scotland, Wales but NOT Northern Ireland
                state = data.get("state", data.get("region", ""))
                cc = UK_STATES.get(state, "GB")
                if cc == "GB":
                    # ISO3166-2-lvl4 is the reliable fallback — covers Northern Ireland
                    iso4 = data.get("ISO3166-2-lvl4", "")
                    cc = NOM_UK_ISO4.get(iso4, "GB")
        except Exception:
            cc = ""
        cc_cache[key] = cc
        return cc

    unique_coords = (
        scorers[["lat", "lon"]].dropna()
        .drop_duplicates()
        .itertuples(index=False)
    )
    needs_lookup = [(r.lat, r.lon) for r in unique_coords
                    if cc_cache.get(f"{r.lat:.3f},{r.lon:.3f}", "") in ("", "GB")]

    if needs_lookup:
        print(f"  {len(needs_lookup)} coordinates to look up or re-resolve (≈{len(needs_lookup)}s)…")
        for i, (lat, lon) in enumerate(needs_lookup):
            _reverse_cc(lat, lon)
            time.sleep(1.1)
            if (i + 1) % 50 == 0:
                CC_CACHE_FILE.write_text(json.dumps(cc_cache, ensure_ascii=False), encoding="utf-8")
                print(f"    …{i+1}/{len(needs_lookup)}")
        CC_CACHE_FILE.write_text(json.dumps(cc_cache, ensure_ascii=False), encoding="utf-8")
        print(f"  Saved {len(cc_cache)} entries to {CC_CACHE_FILE.name}")
    else:
        print(f"  All {len(cc_cache)} coords already cached.")

    scorers["cc"] = scorers.apply(
        lambda r: cc_cache.get(f"{r['lat']:.3f},{r['lon']:.3f}", "")
        if pd.notna(r["lat"]) else "",
        axis=1,
    )

    # 5. Write scorers.js
    print("\nStep 3/3 — Writing scorers.js...")
    records = []
    for _, row in scorers.sort_values("goals", ascending=False).iterrows():
        if pd.isna(row["lat"]) or float(row["lat"]) == 0.0:
            continue
        records.append({
            "player":     row["player_name"],
            "team":       row["team_name"],
            "goals":      int(row["goals"]),
            "gender":     row["gender"],
            "tournament": int(row["tournament_year"]),
            "city":       str(row["city"]),
            "lat":        round(float(row["lat"]), 5),
            "lon":        round(float(row["lon"]), 5),
            "cc":         str(row["cc"]),
        })

    skipped = total - len(records)
    OUTPUT_JS.write_text(
        f"// World Cup Goal Scorers Birthplace Map\n"
        f"// {len(records)}/{total} plotted | {skipped} without Wikidata birthplace\n"
        f"// Men's 1930-2026 | Women's 1991-2019\n"
        f"const SCORERS = {json.dumps(records, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )

    print(f"\nDone! {len(records)}/{total} plotted ({100 * len(records) / total:.1f}%)")
    print(f"Output -> {OUTPUT_JS}")
    print("Open index.html in your browser.")


if __name__ == "__main__":
    main()
