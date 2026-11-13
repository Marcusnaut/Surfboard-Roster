#!/usr/bin/env python3
"""Fetch the latest NDBC observations for buoys near Ventura and write buoys.json.

Run by .github/workflows/buoys.yml every 30 minutes. The app reads the result from the
`data` branch, because NDBC's own files can't be read directly by a web page.

Usage: python3 tools/fetch_buoys.py OUTPUT_PATH
Standard library only.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone

BUOYS = [
    # NDBC id, display name, fallback lat, lon (replaced by NDBC's own position when available)
    ("46053", "East Santa Barbara", 34.241, -119.839),
    ("46217", "Anacapa Passage", 34.167, -119.435),
    ("46216", "Goleta Point", 34.334, -119.803),
    ("46054", "West Santa Barbara", 34.265, -120.477),
    ("46218", "Harvest", 34.454, -120.782),
    ("46025", "Santa Monica Basin", 33.749, -119.053),
    ("46221", "Santa Monica Bay", 33.860, -118.641),
    ("46219", "San Nicolas Island", 33.221, -119.882),
]
BASE = "https://www.ndbc.noaa.gov/data"
UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
HISTORY_HOURS = 24


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
    if v is None or v in ("MM", "N/A", "-99", "99.0", "999", "9999.0"):
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


def positions():
    try:
        rows = parse_table(get(f"{BASE}/latest_obs/latest_obs.txt"))
    except Exception as e:  # noqa: BLE001
        print(f"latest_obs: {e}", file=sys.stderr)
        return {}
    out = {}
    for r in rows:
        lat, lon = num(r.get("LAT")), num(r.get("LON"))
        if r.get("STN") and lat is not None and lon is not None:
            out[r["STN"]] = (lat, lon)
    return out


def buoy(bid, name, lat, lon, pos):
    b = {"id": bid, "name": name, "lat": lat, "lon": lon}
    if bid in pos:
        b["lat"], b["lon"] = pos[bid]
    errors = []
    try:
        spec = parse_table(get(f"{BASE}/realtime2/{bid}.spec"))
    except Exception as e:  # noqa: BLE001
        spec, errors = [], errors + [f"spec: {e}"]
    try:
        met = parse_table(get(f"{BASE}/realtime2/{bid}.txt"))
    except Exception as e:  # noqa: BLE001
        met, errors = [], errors + [f"txt: {e}"]

    # Newest row with a reading (rows are newest first).
    s = next((r for r in spec if num(r.get("WVHT")) is not None or num(r.get("SwH")) is not None), None)
    m = next((r for r in met if any(num(r.get(k)) is not None for k in ("WVHT", "WSPD", "WTMP"))), None)
    if s:
        b.update({
            "time": iso(stamp(s)),
            "wvht": num(s.get("WVHT")),
            "swH": num(s.get("SwH")), "swP": num(s.get("SwP")), "swD": text(s.get("SwD")),
            "wwH": num(s.get("WWH")), "wwP": num(s.get("WWP")), "wwD": text(s.get("WWD")),
            "steep": text(s.get("STEEPNESS")), "apd": num(s.get("APD")), "mwd": num(s.get("MWD")),
        })
    if m:
        b.update({
            "metTime": iso(stamp(m)),
            "dpd": num(m.get("DPD")),
            "wdir": num(m.get("WDIR")), "wspd": num(m.get("WSPD")), "gst": num(m.get("GST")),
            "wtmp": num(m.get("WTMP")), "atmp": num(m.get("ATMP")),
        })
        if b.get("wvht") is None:
            b["wvht"] = num(m.get("WVHT"))
        if b.get("mwd") is None:
            b["mwd"] = num(m.get("MWD"))
        if b.get("apd") is None:
            b["apd"] = num(m.get("APD"))
        if not b.get("time"):
            b["time"] = b["metTime"]
    # Last 24 hours of heights for the popup chart, oldest first.
    newest = stamp(spec[0]) if spec else None
    hist = []
    for r in spec:
        t = stamp(r)
        if not t or not newest or (newest - t).total_seconds() > HISTORY_HOURS * 3600:
            continue
        hist.append({"t": iso(t), "wvht": num(r.get("WVHT")), "swH": num(r.get("SwH"))})
    b["history"] = list(reversed(hist))
    if errors:
        b["errors"] = errors
    return b


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "buoys.json"
    pos = positions()
    buoys = [buoy(*row, pos) for row in BUOYS]
    ok = sum(1 for b in buoys if b.get("time"))
    data = {"updated": iso(datetime.now(timezone.utc)), "source": "NOAA National Data Buoy Center", "buoys": buoys}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"Wrote {out_path}: {ok}/{len(buoys)} buoys with data")
    if ok == 0:
        sys.exit("No buoy returned data; keeping the previous file.")


if __name__ == "__main__":
    main()
