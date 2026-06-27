#!/usr/bin/env python3
"""Patch squads_bp_cache.json with manually found birthplaces."""

import sys, json, time, requests
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE    = Path(__file__).parent
BP_F    = HERE / "squads_bp_cache.json"
CC_F    = HERE / "birth_country_cache.json"
HEADERS = {"User-Agent": "WCBirthplaceMap/1.0 (personal-noncommercial)"}
NOM_URL = "https://nominatim.openstreetmap.org/search"

bp_cache = json.loads(BP_F.read_text(encoding="utf-8"))
cc_cache = json.loads(CC_F.read_text(encoding="utf-8")) if CC_F.exists() else {}

# (QID, display_city, geocode_query, cc)
# Kristiania = historical name for Oslo (renamed 1925), same coords
PATCHES = [
    ("Q1363125",   "Carampangue, Chile",      "Carampangue, Chile",         "CL"),
    ("Q2897340",   "Luque, Paraguay",          "Luque, Paraguay",            "PY"),
    ("Q369357",    "Asunción, Paraguay",       "Asunción, Paraguay",         "PY"),
    ("Q57239239",  "Port Said, Egypt",         "Port Said, Egypt",           "EG"),
    ("Q2635272",   "Lausanne, Switzerland",    "Lausanne, Switzerland",      "CH"),
    ("Q748321",    "Kristiania",               "Oslo, Norway",               "NO"),
    ("Q635914",    "Strømsø, Drammen",         "Strømsø, Drammen, Norway",   "NO"),
    ("Q2343205",   "Kristiania",               "Oslo, Norway",               "NO"),
    ("Q2482183",   "Cochabamba, Bolivia",      "Cochabamba, Bolivia",        "BO"),
    ("Q2244051",   "Asunción, Paraguay",       "Asunción, Paraguay",         "PY"),
    ("Q977165",    "Montevideo, Uruguay",       "Montevideo, Uruguay",         "UY"),
    ("Q938125",    "Östervåla, Sweden",        "Östervåla, Sweden",          "SE"),
    ("Q210616",    "Medellín, Colombia",       "Medellín, Colombia",         "CO"),
    ("Q266436",    "Yenagoa, Nigeria",         "Yenagoa, Nigeria",           "NG"),
    ("Q14132",     "Pyongyang, North Korea",   "Pyongyang, North Korea",     "KP"),
    ("Q5466108",   "Pyongyang, North Korea",   "Pyongyang, North Korea",     "KP"),
    ("Q16632974",  "Malmö, Sweden",            "Malmö, Sweden",              "SE"),
    ("Q117035133", "Desouk, Egypt",            "Desouk, Egypt",              "EG"),
    ("Q139986016", "Alexandria, Egypt",        "Alexandria, Egypt",          "EG"),
    ("Q106212969", "Accra, Ghana",             "Accra, Ghana",               "GH"),
    ("Q135287344", "Amman, Jordan",            "Amman, Jordan",              "JO"),
    ("Q130210573", "Doha, Qatar",              "Doha, Qatar",                "QA"),
    ("Q133505943", "Tunis, Tunisia",           "Tunis, Tunisia",             "TN"),
    ("Q137394484", "Doha, Qatar",             "Doha, Qatar",                "QA"),
    ("Q119432409", "Busan, South Korea",       "Busan, South Korea",         "KR"),
]

ok, fail = 0, []
for qid, city, query, cc in PATCHES:
    print(f"  {city:<35}", end=" ", flush=True)
    try:
        r = requests.get(NOM_URL, params={"q": query, "format": "json", "limit": 1},
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        hits = r.json()
        if hits:
            lat = round(float(hits[0]["lat"]), 5)
            lon = round(float(hits[0]["lon"]), 5)
            bp_cache[qid] = {"city": city, "lat": lat, "lon": lon, "cc": cc}
            cc_cache[f"{lat:.3f},{lon:.3f}"] = cc
            print(f"→ {lat:.4f}, {lon:.4f}  [{cc}]")
            ok += 1
        else:
            print("NOT FOUND")
            fail.append((qid, city, query))
    except Exception as e:
        print(f"ERROR: {e}")
        fail.append((qid, city, query))
    time.sleep(1.1)

BP_F.write_text(json.dumps(bp_cache, ensure_ascii=False, indent=2), encoding="utf-8")
CC_F.write_text(json.dumps(cc_cache, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\n✓ {ok}/{len(PATCHES)} patched")
if fail:
    print("Not found:")
    for qid, city, query in fail:
        print(f"  {qid}  {city}  (query: {query})")
print("\nRun: python build_squads.py")
