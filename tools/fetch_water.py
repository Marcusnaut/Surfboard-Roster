#!/usr/bin/env python3
"""Fetch recent California beach bacteria results and write water.json for the map's water-quality layer.

Source: California State Water Resources Control Board, "Surface Water - Fecal Indicator Bacteria
Monitoring Results" (the data behind the Safe to Swim map), on the California Open Data Portal
(data.ca.gov). Only BeachWatch (ocean beach) results are kept. The state updates it about weekly and
results run a week or more behind sampling.

For each sampling station with a result in the last LOOKBACK_DAYS days, the newest few sample dates
are kept with one value per indicator (enterococcus, fecal coliform, total coliform, E. coli). The app
scores them against the state's single-sample limits.

Usage: python3 tools/fetch_water.py OUTPUT_PATH [PREVIOUS_PATH]
Keeps the previous file when it is less than REFRESH_HOURS old; exits with an error (writing nothing) on failure.
Standard library only.
"""
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
API = "https://data.ca.gov/api/3/action/datastore_search_sql"
FIB = "15a63495-8d9f-4a49-b43a-3092ef3106b9"
LOOKBACK_DAYS = 60
KEEP_DATES = 6
REFRESH_HOURS = 12
VERSION = 2  # bump to force a refresh when the file layout changes
PAGE = 5000
STATIONS = "98e628ff-d012-4982-ad32-b9f9ad8ab524"  # BeachWatch station list: beach names and descriptions
KEYS = {"Enterococcus": "ent", "Coliform, Fecal": "fc", "Coliform, Total": "tc", "E. coli": "ec"}


def sql(q):
    url = API + "?" + urllib.parse.urlencode({"sql": q})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)["result"]["records"]


def station_info():
    """BeachWatch station list by code: [(lat, lon, beach name, description)]. Codes repeat across counties."""
    url = "https://data.ca.gov/api/3/action/datastore_search?" + urllib.parse.urlencode({"resource_id": STATIONS, "limit": 5000})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            recs = json.load(r)["result"]["records"]
    except Exception as e:  # noqa: BLE001
        print(f"station list: {e}", file=sys.stderr)
        return {}
    info = {}
    for r in recs:
        try:
            lat, lon = float(r["Station_UpperLat"]), float(r["Station_UpperLon"])
        except (KeyError, TypeError, ValueError):
            continue
        info.setdefault(str(r.get("Station_Name")), []).append((lat, lon, (r.get("Beach_Name") or "").strip(), (r.get("Station_Description") or "").strip()))
    return info


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "water.json"
    prev_path = sys.argv[2] if len(sys.argv) > 2 else None
    now = datetime.now(timezone.utc)
    if prev_path:
        try:
            prev = json.load(open(prev_path))
            age = now - datetime.fromisoformat(prev["updated"].replace("Z", "+00:00"))
            if age < timedelta(hours=REFRESH_HOURS) and prev.get("stations") and prev.get("v") == VERSION:
                json.dump(prev, open(out_path, "w"), separators=(",", ":"))
                print(f"water.json is {age} old; kept it")
                return
        except Exception:  # noqa: BLE001
            pass
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    cols = '"StationCode","StationName","SampleDate","Analyte","Result","ResultQualCode","TargetLatitude","TargetLongitude","SampleAgency"'
    rows, offset = [], 0
    while True:
        page = sql(f'SELECT {cols} FROM "{FIB}" WHERE "SampleDate" >= \'{since}\' AND "DataSource" = \'BeachWatch\' '
                   f'ORDER BY "_id" LIMIT {PAGE} OFFSET {offset}')
        rows += page
        if len(page) < PAGE:
            break
        offset += PAGE
    stations = {}
    for r in rows:
        key = KEYS.get(r.get("Analyte"))
        lat, lon, code = r.get("TargetLatitude"), r.get("TargetLongitude"), r.get("StationCode")
        if not key or r.get("Result") is None or lat is None or lon is None or code is None:
            continue
        lat, lon = float(lat), float(lon)
        if not (32 < lat < 42.1 and -125 < lon < -117):
            continue
        st = stations.setdefault(code, {"id": str(code), "name": r.get("StationName") or str(code), "lat": round(lat, 5), "lon": round(lon, 5),
                                        "agency": r.get("SampleAgency") or "", "dates": {}})
        day = str(r["SampleDate"])[:10]
        val = float(r["Result"])
        q = (r.get("ResultQualCode") or "=").strip()
        d = st["dates"].setdefault(day, {})
        # Several results for one indicator on one day (replicates): keep the highest.
        if key not in d or val > d[key]:
            d[key] = val
            if q.startswith(("<", ">")):
                d[key + "q"] = q[0]
            else:
                d.pop(key + "q", None)
    info = station_info()
    out = []
    for st in stations.values():
        days = sorted(st.pop("dates").items(), reverse=True)[:KEEP_DATES]
        # Names look like "0-Huntington State Beach, Orange"; drop the leading station code.
        name = st["name"]
        if "-" in name and name.split("-", 1)[0].strip() == str(st["id"]):
            name = name.split("-", 1)[1].strip()
        match = min(info.get(st["id"], []), key=lambda m: (m[0] - st["lat"]) ** 2 + (m[1] - st["lon"]) ** 2, default=None)
        if match and (match[0] - st["lat"]) ** 2 + (match[1] - st["lon"]) ** 2 < 0.05 ** 2:
            beach, desc = match[2], match[3]
            if name.replace(" ", "").isdigit() or name == st["id"]:
                name = beach or desc or name
            if desc and desc.lower() not in name.lower():
                st["desc"] = desc
        st["name"] = name
        st["samples"] = [{"d": day, **vals} for day, vals in days]
        out.append(st)
    out.sort(key=lambda s: (-s["lat"], s["lon"]))
    if not out:
        sys.exit("No water-quality results; keeping the previous file.")
    newest = max(s["samples"][0]["d"] for s in out)
    data = {"v": VERSION, "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "newest": newest,
            "source": "California State Water Resources Control Board, Safe to Swim / BeachWatch (data.ca.gov)", "stations": out}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"Wrote {out_path}: {len(out)} stations from {len(rows)} results since {since}; newest sample {newest}")


if __name__ == "__main__":
    main()
