#!/usr/bin/env python3
"""Fetch CDIP 5-day wave forecasts for California buoys and built-in California spots.

Run by .github/workflows/buoys.yml after fetch_buoys.py. CDIP forecasts update a few times a day,
so this only refreshes when the previous forecasts.json is older than REFRESH_HOURS; otherwise it
keeps the previous file (passed in as PREV_PATH).

- Buoys: CDIP stations carry their CDIP number in the NDBC name, e.g. "Harvest, CA (071)".
  Their forecast is the MOP model at the buoy: MOP_validation/BP071_forecast.nc.
- Spots: each built-in California spot uses the nearest CDIP MOP alongshore site (about every
  100 m along the coast, at 10 m depth) from CDIP's site list, if one is within MAX_KM.

Usage: python3 tools/fetch_forecasts.py BUOYS_JSON SPOTS_JSON OUTPUT_PATH [PREV_PATH]
Standard library only.
"""
import csv
import io
import json
import math
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
NCSS = "https://thredds.cdip.ucsd.edu/thredds/ncss/point/cdip/model"
SITES_URL = "https://zenodo.org/records/6946105/files/CDIP_Transects.csv?download=1"
REFRESH_HOURS = 5.5
HOURS = 120
STEP = 3
MAX_KM = 2.0


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def km(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def mop_sites():
    rows = list(csv.DictReader(io.StringIO(get(SITES_URL, timeout=120))))
    out = []
    for r in rows:
        try:
            lat = float(r.get("Lat_Pred") or r.get("Lat_BackBeach"))
            lon = float(r.get("Lon_Pred") or r.get("Lon_BackBeach"))
        except (TypeError, ValueError):
            continue
        if r.get("ID"):
            out.append((r["ID"].strip(), lat, lon))
    return out


def series(path, start, end):
    url = (f"{NCSS}/{path}?var=waveHs,waveTp,waveDp&accept=csv"
           f"&time_start={start:%Y-%m-%dT%H:%M:%SZ}&time_end={end:%Y-%m-%dT%H:%M:%SZ}")
    text = get(url)
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return None
    col = {}
    for i, h in enumerate(header):
        for key in ("time", "waveHs", "waveTp", "waveDp"):
            if h == key or h.startswith(key + "["):
                col[key] = i
    if not all(k in col for k in ("time", "waveHs")):
        return None
    rows = []
    for r in reader:
        try:
            t = datetime.strptime(r[col["time"]], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
        if t.hour % STEP:
            continue
        def val(k, nd=2):
            try:
                v = float(r[col[k]])
                return None if math.isnan(v) or v < -900 else round(v, nd)
            except (KeyError, ValueError, IndexError):
                return None
        rows.append((t, val("waveHs"), val("waveTp", 1), val("waveDp", 0)))
    if not rows:
        return None
    return {"t0": rows[0][0].strftime("%Y-%m-%dT%H:%MZ"), "step": STEP,
            "hs": [r[1] for r in rows], "tp": [r[2] for r in rows], "dp": [r[3] for r in rows]}


def fresh(prev_path):
    try:
        prev = json.load(open(prev_path))
        age = datetime.now(timezone.utc) - datetime.strptime(prev["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return prev if age < timedelta(hours=REFRESH_HOURS) else None
    except Exception:  # noqa: BLE001
        return None


def main():
    buoys_path, spots_path, out_path = sys.argv[1:4]
    prev_path = sys.argv[4] if len(sys.argv) > 4 else ""
    prev = fresh(prev_path) if prev_path and os.path.exists(prev_path) else None
    if prev and not os.environ.get("FORCE_FORECASTS"):
        json.dump(prev, open(out_path, "w"), separators=(",", ":"))
        print(f"Kept previous forecasts from {prev['updated']}")
        return

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=now.hour % STEP)
    end = start + timedelta(hours=HOURS)

    jobs = {}  # model path -> list of (kind, key, extra)
    buoys = json.load(open(buoys_path)).get("buoys", [])
    for b in buoys:
        m = re.search(r"\((\d{3})\)", b.get("name", ""))
        if m and 32 <= b.get("lat", 0) <= 42.1 and b.get("lon", 0) < -117:
            jobs.setdefault(f"MOP_validation/BP{m.group(1)}_forecast.nc", []).append(("buoys", b["id"], {"site": f"BP{m.group(1)}", "cdip": m.group(1)}))

    spots = [s for s in json.load(open(spots_path))["spots"] if s.get("ca")]
    sites = mop_sites()
    for s in spots:
        best = min(sites, key=lambda x: (x[1] - s["lat"]) ** 2 + ((x[2] - s["lon"]) * math.cos(math.radians(s["lat"]))) ** 2)
        d = km(s["lat"], s["lon"], best[1], best[2])
        if d <= MAX_KM:
            jobs.setdefault(f"MOP_alongshore/{best[0]}_forecast.nc", []).append(("spots", s["id"], {"site": best[0], "km": round(d, 2)}))

    def run(item):
        path, targets = item
        try:
            return path, targets, series(path, start, end)
        except Exception as ex:  # noqa: BLE001
            return path, targets, str(ex)

    out = {"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "CDIP (Scripps Institution of Oceanography)", "buoys": {}, "spots": {}}
    failed = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for path, targets, res in pool.map(run, jobs.items()):
            if not isinstance(res, dict):
                failed += 1
                continue
            for kind, key, extra in targets:
                out[kind][key] = {**extra, **res}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {out_path}: {len(out['buoys'])} buoys, {len(out['spots'])} spots; {failed} of {len(jobs)} model requests failed")
    if not out["buoys"] and not out["spots"]:
        if prev_path and os.path.exists(prev_path):
            json.dump(json.load(open(prev_path)), open(out_path, "w"), separators=(",", ":"))
            print("Nothing fetched; kept the previous file.")


if __name__ == "__main__":
    main()
