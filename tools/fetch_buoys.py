#!/usr/bin/env python3
"""Fetch the latest NDBC wave-buoy observations for the US West and East coasts and write buoys.json.

Run by .github/workflows/buoys.yml every 30 minutes. The app reads the result from the
`data` branch, because NDBC's own files can't be read directly by a web page.

Which buoys: every station in NDBC's latest observations that reports wave height inside the
coast boxes below, named from NDBC's active station list. For each, the .spec file adds the
primary swell / wind swell split and the last 24 hours of heights.

Usage: python3 tools/fetch_buoys.py OUTPUT_PATH
Standard library only.
"""
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://www.ndbc.noaa.gov"
UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
HISTORY_HOURS = 24
MAX_AGE_HOURS = 6
# (name, south, north, west, east)
REGIONS = [
    ("West Coast", 32.0, 49.5, -128.0, -116.8),
    ("East Coast", 24.3, 45.5, -82.5, -65.0),
]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_table(text):
    """Parse an NDBC whitespace table into a list of dicts keyed by the first header row."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("#"):
        return []
    header = lines[0].lstrip("#").split()
    rows = []
    for line in lines:
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < len(header):
            continue
        rows.append(dict(zip(header, parts)))
    return rows


def num(v):
    if v is None or v in ("MM", "N/A", "-99", "99.0", "99.00", "999", "999.0", "9999.0"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def text(v):
    return None if v in (None, "MM", "N/A") else v


def stamp(row):
    y = row.get("YY") or row.get("YYYY")
    try:
        return datetime(int(y), int(row["MM"]), int(row.get("DD") or row.get("day")),
                        int(row["hh"]), int(row["mm"]), tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def region_of(lat, lon):
    for name, s, n, w, e in REGIONS:
        if s <= lat <= n and w <= lon <= e:
            return name
    return None


def station_names():
    try:
        root = ET.fromstring(get(f"{BASE}/activestations.xml"))
    except Exception as e:  # noqa: BLE001
        print(f"activestations: {e}", file=sys.stderr)
        return {}
    return {s.get("id", "").upper(): (s.get("name") or "").strip() for s in root.iter("station")}


def candidates(now):
    """Wave-reporting stations inside the coast boxes, from latest_obs.txt."""
    rows = parse_table(get(f"{BASE}/data/latest_obs/latest_obs.txt"))
    out = []
    for r in rows:
        lat, lon = num(r.get("LAT")), num(r.get("LON"))
        if lat is None or lon is None or num(r.get("WVHT")) is None:
            continue
        region = region_of(lat, lon)
        t = stamp(r)
        if not region or not t or (now - t).total_seconds() > MAX_AGE_HOURS * 3600:
            continue
        out.append((r, region))
    return out


def tidy_name(raw, sid):
    if not raw:
        return f"Buoy {sid}"
    # NDBC names look like "EAST SANTA BARBARA - 12 NM Southwest of Santa Barbara, CA" or "Anacapa Passage, CA (111)".
    name = raw.split(" - ")[0].strip()
    if name.isupper():
        name = name.title()
    return name


def buoy(row, region, names):
    sid = row["STN"].upper()
    b = {
        "id": sid, "name": tidy_name(names.get(sid), sid), "region": region,
        "lat": num(row.get("LAT")), "lon": num(row.get("LON")),
        "time": iso(stamp(row)),
        "wvht": num(row.get("WVHT")), "dpd": num(row.get("DPD")), "apd": num(row.get("APD")), "mwd": num(row.get("MWD")),
        "wdir": num(row.get("WDIR")), "wspd": num(row.get("WSPD")), "gst": num(row.get("GST")),
        "wtmp": num(row.get("WTMP")), "atmp": num(row.get("ATMP")),
    }
    try:
        spec = parse_table(get(f"{BASE}/data/realtime2/{sid}.spec"))
    except Exception:  # noqa: BLE001  many stations have no spectral file
        spec = []
    s = next((r for r in spec if num(r.get("SwH")) is not None or num(r.get("WVHT")) is not None), None)
    if s:
        b.update({
            "swH": num(s.get("SwH")), "swP": num(s.get("SwP")), "swD": text(s.get("SwD")),
            "wwH": num(s.get("WWH")), "wwP": num(s.get("WWP")), "wwD": text(s.get("WWD")),
            "steep": text(s.get("STEEPNESS")),
        })
        st = stamp(s)
        if st and (not b["time"] or iso(st) > b["time"]):
            b["time"] = iso(st)
            if num(s.get("WVHT")) is not None:
                b["wvht"] = num(s.get("WVHT"))
    # Last 24 hours, one reading per hour, oldest first.
    newest = stamp(spec[0]) if spec else None
    hist, seen = [], set()
    for r in spec:
        t = stamp(r)
        if not t or not newest or (newest - t).total_seconds() > HISTORY_HOURS * 3600:
            continue
        key = t.strftime("%Y%m%d%H")
        if key in seen:
            continue
        seen.add(key)
        hist.append({"t": iso(t), "wvht": num(r.get("WVHT")), "swH": num(r.get("SwH"))})
    b["history"] = list(reversed(hist))
    return {k: v for k, v in b.items() if v is not None}


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "buoys.json"
    now = datetime.now(timezone.utc)
    names = station_names()
    cands = candidates(now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        buoys = list(pool.map(lambda c: buoy(c[0], c[1], names), cands))
    buoys.sort(key=lambda b: (b["region"], -b["lat"]))
    data = {"updated": iso(now), "source": "NOAA National Data Buoy Center", "regions": [r[0] for r in REGIONS], "buoys": buoys}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    with_swell = sum(1 for b in buoys if "swH" in b)
    print(f"Wrote {out_path}: {len(buoys)} buoys ({with_swell} with swell split)")
    if not buoys:
        sys.exit("No buoy returned data; keeping the previous file.")


if __name__ == "__main__":
    main()
