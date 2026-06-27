#!/usr/bin/env python3
"""
build_squads.py — Build Men's World Cup full squad birthplace dataset.

Fetches Wikipedia squad pages, resolves each player's Wikipedia article
to a Wikidata birthplace, geocodes birth coords → ISO country code.

Deps:  pip install requests beautifulsoup4
Run:   python build_squads.py

Expected runtime (first run):
  Steps 1–3 (Wikipedia + Wikidata, batched): ~10–15 min
  Step 4 (Nominatim, new coords only):       ~20–40 min
  Total: 30–55 min
  Reruns: < 1 min (all cached)
"""

import sys, json, re, time
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

HERE     = Path(__file__).parent
OUT_JS   = HERE / "squads.js"

# Cache files (safe to delete to force a re-fetch)
PARSED_F = HERE / "squads_parsed.json"    # raw parsed rows from Wikipedia pages
QID_F    = HERE / "squads_qid_cache.json" # Wikipedia article title → Wikidata QID
BP_F     = HERE / "squads_bp_cache.json"  # Wikidata QID → {city, lat, lon} or null
CC_F     = HERE / "birth_country_cache.json"  # shared with build_data.py

HEADERS   = {"User-Agent": "WCBirthplaceMap/1.0 (personal-noncommercial)"}
WP_API    = "https://en.wikipedia.org/w/api.php"
WD_SPARQL = "https://query.wikidata.org/sparql"
NOM_URL   = "https://nominatim.openstreetmap.org/reverse"

MEN_YEARS = [
    1930, 1934, 1938, 1950, 1954, 1958, 1962, 1966, 1970,
    1974, 1978, 1982, 1986, 1990, 1994, 1998, 2002, 2006,
    2010, 2014, 2018, 2022, 2026,
]

# ── Utilities ──────────────────────────────────────────────────────────────

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def eta_str(done, total, elapsed_s):
    if done == 0 or elapsed_s < 0.01:
        return "?"
    remaining = (total - done) / (done / elapsed_s)
    if remaining > 90:
        return f"{remaining / 60:.1f} min"
    return f"{remaining:.0f}s"


# ── Step 1: Parse Wikipedia squad pages ───────────────────────────────────

SKIP_HEADINGS = {
    "notes", "references", "see also", "external links",
    "sources", "key", "squads", "legend",
}

# Headings that are group labels (e.g. "Group A"), not team names
GROUP_RE = re.compile(r"^group\s+[a-z]$", re.I)

# Headings for stats tables embedded on squad pages — not actual squads
STATS_HEADING = re.compile(
    r"representation\s+by|average\s+age|coaches\s+by|managers?\s+by|kit\s+",
    re.I,
)

# Article URL patterns that identify clubs, leagues, and football concepts (not people)
_NON_PLAYER_ART = re.compile(
    r"(_F\.?C\.?_|_F\.?C\.?$|^F\.?C\.?_|"         # F.C. / FC in club names
    r"_A\.?F\.?C\.?$|_A\.?F\.?C\.?_|^A\.?F\.?C_|"  # AFC
    r"_S\.?[KC]\.?_|_S\.?[KC]\.?$|^S\.?[KC]\.?_|"  # SK / SC clubs
    r"_R\.?F\.?C\.?$|_R\.?F\.?C\.?_|"              # RFC
    r"^FK_|_FK_|^BK_|_BK_|^IFK_|"                  # FK/BK/IFK (Scandinavian/Slavic clubs)
    r"_CF$|_CF_|^CF_|"                              # CF (Valencia CF, Girona CF…)
    r"^Club_|"                                      # Club América, Club Brugge…
    r"_United$|_City$|_Town$|_Athletic$|_Villa$|"
    r"_Wanderers$|_Rovers$|_Rangers$|_County$|"
    r"_Club$|_Club_|_Hotspur$|_Albion$|_Orient$|"
    r"Football_in_|_football_league|_league_system|"
    r"_national_football_team|_national_team|_football_team$|"
    r"association_football|_football_association|"
    r"^Goalkeeper|^Midfielder|^Defender|^Forward|^Winger|^Sweeper|"
    r"^Association_football|^Libero|^Striker)",
    re.I,
)

# Display text patterns that are position abbreviations
_POS_ABBREV = re.compile(r"^(GK|DF|MF|FW|GR|PO|AR|ME|DE|AT|CB|RB|LB|CM|RM|LM|CAM|CDM|CF|SS)$", re.I)


def _is_player_link(article, display):
    """True if this link is likely a Wikipedia article for a person."""
    if _NON_PLAYER_ART.search(article):
        return False
    if _POS_ABBREV.match(display.strip()):
        return False
    return True


def fetch_squad_page_html(year):
    page = f"{year}_FIFA_World_Cup_squads"
    for attempt in range(6):
        try:
            r = requests.get(WP_API, params={
                "action": "parse",
                "page":   page,
                "prop":   "text",
                "format": "json",
                "disableeditsection": "1",
            }, headers=HEADERS, timeout=40)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"rate-limited, waiting {wait}s…", end=" ", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()["parse"]["text"]["*"]
        except requests.exceptions.HTTPError as e:
            raise
    raise RuntimeError(f"Max retries exceeded fetching {year}")


def parse_squad_html(html, year):
    """
    Return list of {name, article, team, year} dicts.
    'article' is the Wikipedia article title (underscored, URL-decoded).
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    current_team = None

    for el in soup.find_all(["h2", "h3", "h4", "table"]):
        if el.name in ("h2", "h3", "h4"):
            raw  = el.get_text(" ", strip=True)
            text = re.sub(r"\[.*?\]", "", raw).strip()
            low  = text.lower()
            if low in SKIP_HEADINGS or GROUP_RE.match(low) or STATS_HEADING.search(low):
                current_team = None   # stop processing until next valid team heading
            elif low:
                current_team = text
        elif el.name == "table" and current_team:
            classes = el.get("class") or []
            if "wikitable" not in classes:
                continue
            for tr in el.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                # Skip pure header rows
                if not cells or all(c.name == "th" for c in cells):
                    continue
                # Skip coaching staff rows
                row_text = tr.get_text(" ").lower()
                if any(w in row_text for w in (
                    "manager", "head coach", "assistant coach", "coaching staff",
                    "goalkeeping coach", "technical director", "fitness coach",
                    "coach", "trainer", "fitness",
                )):
                    continue
                hit = _player_link(cells)
                if hit:
                    rows.append({**hit, "team": current_team, "year": year})
    return rows


def _player_link(cells):
    """
    Find the player's Wikipedia link among the cells of a squad table row.
    Returns {name, article} or None.

    Filters out club articles, position articles, and league articles using
    _is_player_link(). For rows where the player has no Wikipedia article,
    all cells will have either 0 qualifying links or only non-player links,
    so we correctly return None.
    """
    for cell in cells:
        links = []
        for a in cell.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("/wiki/"):
                continue
            raw_article = unquote(href[6:]).split("#")[0]
            if ":" in raw_article:          # skip File:, Category:, etc.
                continue
            if a.find("img"):               # skip flag/icon links
                continue
            display = a.get_text(strip=True)
            if not display:
                continue
            art = raw_article.replace(" ", "_")
            if not _is_player_link(art, display):
                continue
            links.append((art, display))

        if len(links) == 1:
            return {"article": links[0][0], "name": links[0][1]}

        if len(links) > 1:
            # Multiple qualifying links in one cell — take the first
            return {"article": links[0][0], "name": links[0][1]}
    return None


# ── Step 2: Batch-fetch Wikidata QIDs via Wikipedia pageprops API ──────────

def fill_qid_cache(articles, qid_cache):
    needed = [a for a in articles if a not in qid_cache]
    if not needed:
        print(f"  All {len(articles)} QIDs already cached.")
        return

    total = len(needed)
    done  = 0
    start = time.time()
    print(f"  {total} new articles to resolve…")

    for i in range(0, total, 50):
        batch  = needed[i : i + 50]
        titles = [a.replace("_", " ") for a in batch]

        for attempt in range(6):
            try:
                r = requests.get(WP_API, params={
                    "action":    "query",
                    "titles":    "|".join(titles),
                    "prop":      "pageprops",
                    "ppprop":    "wikibase_item",
                    "format":    "json",
                    "redirects": "1",
                }, headers=HEADERS, timeout=30)

                if r.status_code == 429:
                    wait = 20 * (attempt + 1)
                    print(f"\n    rate-limited (QID batch {i//50+1}), waiting {wait}s…", end=" ", flush=True)
                    time.sleep(wait)
                    continue

                r.raise_for_status()
                data = r.json().get("query", {})

                # Build redirect map: "from title" → "canonical title"
                redirect_map = {}
                for redir in (data.get("redirects") or []):
                    redirect_map[redir["from"]] = redir["to"]

                # Build canonical title → QID
                canon_to_qid = {}
                for page in data.get("pages", {}).values():
                    canon = page.get("title", "")
                    qid   = page.get("pageprops", {}).get("wikibase_item", "")
                    if canon:
                        canon_to_qid[canon] = qid

                # Store result for each original article
                for orig, title in zip(batch, titles):
                    canonical       = redirect_map.get(title, title)
                    qid_cache[orig] = canon_to_qid.get(canonical, "")

                break  # success — exit retry loop

            except requests.exceptions.HTTPError as e:
                print(f"\n    [!] HTTP error QID batch {i//50+1}: {e}")
                break
            except Exception as e:
                wait = 5 * (2 ** attempt)
                print(f"\n    [!] Error QID batch {i//50+1} (retry {attempt+1} in {wait}s): {e}")
                time.sleep(wait)

        done += len(batch)
        elapsed = time.time() - start
        print(f"    {done}/{total}  ETA {eta_str(done, total, elapsed)}", end="\r")
        time.sleep(1.2)

    print()


# ── Step 3: Batch-fetch birthplaces from Wikidata (SPARQL) ────────────────

def fill_bp_cache(qids, bp_cache):
    needed = [q for q in qids if q and q not in bp_cache]
    if not needed:
        print(f"  All birthplaces already cached.")
        return

    total = len(needed)
    done  = 0
    start = time.time()
    print(f"  {total} players to look up…")

    for i in range(0, total, 50):
        batch  = needed[i : i + 50]
        values = " ".join(f"wd:{q}" for q in batch)
        # Also fetch the birth city's country ISO alpha-2 code (wdt:P297).
        # This lets Step 4 skip Nominatim for all non-GB coordinates.
        query  = f"""
SELECT ?player ?cityLabel ?coords ?countryIso WHERE {{
  VALUES ?player {{ {values} }}
  OPTIONAL {{
    ?player wdt:P19 ?city .
    ?city   wdt:P625 ?coords .
    OPTIONAL {{
      ?city wdt:P17 ?country .
      ?country wdt:P297 ?countryIso .
    }}
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}"""
        for attempt in range(4):
            try:
                r = requests.get(WD_SPARQL,
                    params={"query": query, "format": "json"},
                    headers=HEADERS, timeout=90)
                r.raise_for_status()
                for b in r.json()["results"]["bindings"]:
                    q = b["player"]["value"].rsplit("/", 1)[-1]
                    if "coords" in b and "cityLabel" in b:
                        raw = b["coords"]["value"]   # "Point(lon lat)"
                        p   = raw.replace("Point(", "").replace(")", "").split()
                        cc_raw = b.get("countryIso", {}).get("value", "")
                        bp_cache[q] = {
                            "city": b["cityLabel"]["value"],
                            "lat":  round(float(p[1]), 5),
                            "lon":  round(float(p[0]), 5),
                            "cc":   cc_raw.upper() if cc_raw else "",
                        }
                    elif q not in bp_cache:
                        bp_cache[q] = None   # confirmed: no birthplace in Wikidata
                break
            except Exception as e:
                if attempt < 3:
                    wait = 10 * (attempt + 1)
                    print(f"\n    [!] SPARQL batch {i//50+1} error (retry in {wait}s): {e}")
                    time.sleep(wait)
                else:
                    print(f"\n    [!] SPARQL batch {i//50+1} failed: {e}")

        done += len(batch)
        elapsed = time.time() - start
        print(f"    {done}/{total}  ETA {eta_str(done, total, elapsed)}", end="\r")
        time.sleep(1.5)

    print()


# ── Step 4: Nominatim reverse-geocode → ISO birth country ─────────────────

UK_STATES   = {"England": "GB-ENG", "Scotland": "GB-SCO",
               "Wales": "GB-WAL", "Northern Ireland": "GB-NIR"}
NOM_UK_ISO4 = {"GB-ENG": "GB-ENG", "GB-SCT": "GB-SCO",
               "GB-WLS": "GB-WAL", "GB-NIR": "GB-NIR"}


def reverse_cc(lat, lon, cc_cache):
    key    = f"{lat:.3f},{lon:.3f}"
    cached = cc_cache.get(key, "")
    if cached and cached != "GB":
        return cached
    try:
        r = requests.get(NOM_URL,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 5},
            headers=HEADERS, timeout=12)
        r.raise_for_status()
        addr = r.json().get("address", {})
        cc   = addr.get("country_code", "").upper()
        if cc == "GB":
            state = addr.get("state", addr.get("region", ""))
            cc    = UK_STATES.get(state, "GB")
            if cc == "GB":
                iso4 = addr.get("ISO3166-2-lvl4", "")
                cc   = NOM_UK_ISO4.get(iso4, "GB")
    except Exception:
        cc = ""
    cc_cache[key] = cc
    return cc


def fill_cc_cache(records, cc_cache):
    # Only geocode with Nominatim where Wikidata gave "GB" or nothing.
    # All other countries are already known from the SPARQL query in Step 3.
    unique = list({
        (r["lat"], r["lon"])
        for r in records
        if r.get("lat")
        and r.get("cc", "") in ("", "GB")   # Wikidata didn't resolve this one definitively
        and cc_cache.get(f"{r['lat']:.3f},{r['lon']:.3f}", "") in ("", "GB")
    })
    if not unique:
        print(f"  All coords resolved (Wikidata) or cached (Nominatim).")
        return

    total = len(unique)
    print(f"  {total} GB/unknown coordinates to Nominatim-geocode (~{total}s at 1/sec)…")
    start = time.time()
    for i, (la, lo) in enumerate(unique):
        reverse_cc(la, lo, cc_cache)
        elapsed = time.time() - start
        print(f"    {i+1}/{total}  ETA {eta_str(i+1, total, elapsed)}", end="\r")
        if (i + 1) % 100 == 0:
            save_json(CC_F, cc_cache)
        time.sleep(1.1)
    print()
    save_json(CC_F, cc_cache)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parsed_cache = load_json(PARSED_F, {})
    qid_cache    = load_json(QID_F,    {})
    bp_cache     = load_json(BP_F,     {})
    cc_cache     = load_json(CC_F,     {})

    # ── 1. Fetch squad pages ───────────────────────────────────────────────
    print("=" * 60)
    print(f"Step 1/4 — Fetching {len(MEN_YEARS)} Wikipedia squad pages")
    print("=" * 60)
    all_rows = []
    for year in MEN_YEARS:
        key = str(year)
        if key in parsed_cache:
            rows = parsed_cache[key]
            print(f"  {year}  (cached)  {len(rows)} entries")
        else:
            print(f"  {year}  fetching…", end=" ", flush=True)
            try:
                html = fetch_squad_page_html(year)
                rows = parse_squad_html(html, year)
                parsed_cache[key] = rows
                save_json(PARSED_F, parsed_cache)
                print(f"{len(rows)} entries")
            except Exception as e:
                print(f"ERROR — {e}")
                rows = []
            time.sleep(2.5)
        all_rows.extend(rows)

    unique_articles = sorted({r["article"] for r in all_rows})
    print(f"\n  {len(all_rows)} player-tournament entries")
    print(f"  {len(unique_articles)} unique player articles\n")

    # ── 2. Resolve Wikipedia articles → Wikidata QIDs ─────────────────────
    print("=" * 60)
    print("Step 2/4 — Resolving Wikipedia articles → Wikidata QIDs")
    print("=" * 60)
    fill_qid_cache(unique_articles, qid_cache)
    save_json(QID_F, qid_cache)

    with_qid = [a for a in unique_articles if qid_cache.get(a)]
    print(f"  {len(with_qid)}/{len(unique_articles)} articles resolved to QIDs\n")

    # ── 3. Fetch birthplaces from Wikidata ────────────────────────────────
    print("=" * 60)
    print("Step 3/4 — Fetching birthplaces from Wikidata (SPARQL)")
    print("=" * 60)
    all_qids = [qid_cache.get(a) for a in unique_articles if qid_cache.get(a)]
    fill_bp_cache(all_qids, bp_cache)
    save_json(BP_F, bp_cache)

    with_bp = sum(1 for q in all_qids if bp_cache.get(q))
    print(f"  {with_bp}/{len(all_qids)} players have Wikidata birthplace\n")

    # ── 4. Reverse-geocode birth coords → ISO country code ────────────────
    print("=" * 60)
    print("Step 4/4 — Tagging birth countries (Nominatim)")
    print("=" * 60)

    # Build flat records first so we can pass coords to Nominatim
    records = []
    for row in all_rows:
        art = row["article"]
        qid = qid_cache.get(art)
        bp  = bp_cache.get(qid) if qid else None
        records.append({
            "player":     row["name"],
            "team":       row["team"],
            "tournament": row["year"],
            "gender":     "men",
            "city":       bp["city"] if bp else None,
            "lat":        bp["lat"]  if bp else None,
            "lon":        bp["lon"]  if bp else None,
            "wiki":       art,
            "cc":         (bp.get("cc", "") if bp else ""),   # from Wikidata SPARQL
        })

    records_with_coords = [r for r in records if r.get("lat")]
    fill_cc_cache(records_with_coords, cc_cache)

    # Stamp final cc: Wikidata for non-GB, Nominatim for GB/unknown (handles subdivisions)
    for r in records_with_coords:
        wd_cc  = r.get("cc", "")
        nom_cc = cc_cache.get(f"{r['lat']:.3f},{r['lon']:.3f}", "")
        if wd_cc and wd_cc != "GB":
            r["cc"] = wd_cc    # Wikidata gave a definitive non-UK country
        elif nom_cc:
            r["cc"] = nom_cc   # Nominatim (handles GB-ENG / GB-SCO / GB-WAL / GB-NIR)
        else:
            r["cc"] = wd_cc    # last resort (might be "GB" or "")

    # ── Write squads.js ────────────────────────────────────────────────────
    out   = [r for r in records if r.get("lat")]
    total = len(records)
    print(f"\nWriting squads.js…  ({len(out)}/{total} plotted, "
          f"{total - len(out)} without birthplace)")

    OUT_JS.write_text(
        f"// World Cup Full Squads Birthplace Map\n"
        f"// {len(out)}/{total} plotted | {total - len(out)} without birthplace\n"
        f"// Men's 1930–2026\n"
        f"const SQUADS = {json.dumps(out, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )

    print(f"Done!  →  {OUT_JS}")
    print(f"\nMatch rate: {100 * len(out) / total:.1f}%")


if __name__ == "__main__":
    main()
