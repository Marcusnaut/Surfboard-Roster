#!/usr/bin/env python3
"""Fetch the latest NDBC wave-buoy observations for the US West and East coasts and write buoys.json.

Run by .github/workflows/buoys.yml every 30 minutes. The app reads the result from the
`data` branch, because NDBC's own files can't be read directly by a web page.

Which buoys: every station in NDBC's latest observations that reports wave height inside the
coast boxes below, named from NDBC's active station list. For each, the .spec file adds the
primary swell / wind swell split and the last 24 hours of heights, and the newest spectrum
(.data_spec energy plus .swdir direction per frequency) is split into up to three swells.

Usage: python3 tools/fetch_buoys.py OUTPUT_PATH
Standard library only.
"""
import json
import math
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


def head(url, limit=24000):
    """First part of a file only: NDBC realtime2 files hold 45 days, newest first, and only the top row is needed."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": f"bytes=0-{limit - 1}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(limit).decode("utf-8", "replace")


def spectral_row(text):
    """Newest row of a .data_spec / .swdir file: (time, [(freq, value), ...])."""
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.replace("(", " ").replace(")", " ").split()
        try:
            t = datetime(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), tzinfo=timezone.utc)
        except (ValueError, IndexError):
            return None, []
        rest = parts[5:]
        if len(rest) % 2:  # .data_spec has a separation frequency first
            rest = rest[1:]
        pairs = []
        for i in range(0, len(rest) - 1, 2):
            try:
                pairs.append((float(rest[i + 1]), float(rest[i])))
            except ValueError:
                pass
        return t, pairs
    return None, []


def ang_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def circ_mean(pairs):
    x = sum(w * math.cos(math.radians(d)) for d, w in pairs)
    y = sum(w * math.sin(math.radians(d)) for d, w in pairs)
    return None if not x and not y else (math.degrees(math.atan2(y, x)) + 360) % 360


def partitions(energy, dirs, keep=3, min_h=0.1):
    """Split one wave spectrum into separate swells.

    energy: [(freq Hz, m^2/Hz)], dirs: {freq: mean direction (from, degrees)}. Each frequency bin climbs
    to its nearest spectral peak (a watershed on the lightly smoothed spectrum). Bins don't climb across a sharp
    change in direction. Neighbouring peaks are merged when the dip between them is shallow and they come
    from about the same direction (within 40 degrees). For each
    swell: height = 4 * sqrt(energy), period = 1 / peak frequency, direction = energy-weighted mean
    direction around the peak. Returns up to `keep`, biggest first."""
    bins = sorted((f, e) for f, e in energy if 0.03 <= f <= 0.5 and e >= 0)
    n = len(bins)
    if n < 5:
        return []
    fr = [f for f, _ in bins]
    en = [e for _, e in bins]
    df = [((fr[min(i + 1, n - 1)] - fr[max(i - 1, 0)]) / (2 if 0 < i < n - 1 else 1)) for i in range(n)]
    sm = [(en[max(i - 1, 0)] + 2 * en[i] + en[min(i + 1, n - 1)]) / 4 for i in range(n)]
    def turns(a, b):
        da, db = dirs.get(fr[a]), dirs.get(fr[b])
        return da is not None and db is not None and ang_diff(da, db) >= 40

    owner = []
    for i in range(n):
        j = i
        while True:
            # Climb only toward bins from about the same direction, so a swell from another direction that
            # sits on the shoulder of a bigger one keeps its own peak.
            k = max((x for x in (j - 1, j, j + 1) if 0 <= x < n and (x == j or not turns(j, x))), key=lambda x: sm[x])
            if k == j:
                break
            j = k
        owner.append(j)
    peaks = sorted(set(owner))

    def pdir(p, members):
        near = [(dirs.get(fr[i]), en[i] * df[i]) for i in members if abs(i - p) <= 2 and dirs.get(fr[i]) is not None]
        return circ_mean(near)

    groups = [[i for i in range(n) if owner[i] == p] for p in peaks]
    merged = True
    while merged and len(groups) > 1:
        merged = False
        for g in range(len(groups) - 1):
            a, b = groups[g], groups[g + 1]
            pa, pb = max(a, key=lambda i: sm[i]), max(b, key=lambda i: sm[i])
            trough = min(sm[i] for i in range(min(pa, pb), max(pa, pb) + 1))
            low = min(sm[pa], sm[pb])
            da, db = pdir(pa, a), pdir(pb, b)
            other_way = da is not None and db is not None and ang_diff(da, db) >= 40
            if low <= 0 or (not other_way and trough >= 0.5 * low):
                groups[g:g + 2] = [sorted(a + b)]
                merged = True
                break
    out = []
    for g in groups:
        m0 = sum(en[i] * df[i] for i in g)
        p = max(g, key=lambda i: en[i])
        if m0 <= 0:
            continue
        h = 4 * math.sqrt(m0)
        d = pdir(p, g)
        if h >= min_h:
            out.append({"h": round(h, 2), "p": round(1 / fr[p], 1), "d": None if d is None else round(d)})
    out.sort(key=lambda s: -s["h"])
    return out[:keep]


def swells(sid):
    try:
        t1, en = spectral_row(head(f"{BASE}/data/realtime2/{sid}.data_spec"))
        t2, dr = spectral_row(head(f"{BASE}/data/realtime2/{sid}.swdir"))
    except Exception:  # noqa: BLE001  most stations have no spectra
        return None, []
    if not en or not t1 or not t2 or t1 != t2:
        return None, []
    dirs = {f: d for f, d in dr if 0 <= d <= 360}
    return {"t": iso(t1), "dirs": dirs}, partitions(en, dirs)


def dir_at(spec, period):
    """Mean direction (degrees) in the frequency bin closest to a period, from the newest .swdir row."""
    if not spec or not period or not spec["dirs"]:
        return None
    f = min(spec["dirs"], key=lambda x: abs(x - 1 / period))
    return round(spec["dirs"][f])


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
    return {s.get("id", "").upper(): ((s.get("name") or "").strip(), s.get("type") or "") for s in root.iter("station")}


def candidates(now, names):
    """Stations inside the coast boxes that report waves or are moored buoys, from latest_obs.txt.

    latest_obs only holds each station's newest row, which is often a weather-only report, so a
    buoy can show no wave height there; moored buoys are kept and checked against their .spec file.
    Great Lakes stations (ids starting 45) are skipped."""
    rows = parse_table(get(f"{BASE}/data/latest_obs/latest_obs.txt"))
    out = []
    for r in rows:
        sid = r.get("STN", "").upper()
        lat, lon = num(r.get("LAT")), num(r.get("LON"))
        if lat is None or lon is None or sid.startswith("45"):
            continue
        if num(r.get("WVHT")) is None and names.get(sid, ("", ""))[1] != "buoy":
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
    name = raw.split(" - ")[0].split("  ")[0].strip()
    if name.isupper():
        name = name.title()
    return name


def buoy(row, region, names):
    sid = row["STN"].upper()
    b = {
        "id": sid, "name": tidy_name(names.get(sid, ("", ""))[0], sid), "region": region,
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
        spec_dirs, parts = swells(sid)
        b["swDeg"] = dir_at(spec_dirs, b.get("swP"))
        b["wwDeg"] = dir_at(spec_dirs, b.get("wwP"))
        if parts:
            b["swells"] = parts
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
    if b.get("wvht") is None and b.get("swH") is None:
        return None  # not a wave buoy, or no recent waves
    if s and stamp(s) and (datetime.now(timezone.utc) - stamp(s)).total_seconds() > MAX_AGE_HOURS * 3600 and num(row.get("WVHT")) is None:
        return None
    return {k: v for k, v in b.items() if v is not None}


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "buoys.json"
    now = datetime.now(timezone.utc)
    names = station_names()
    cands = candidates(now, names)
    with ThreadPoolExecutor(max_workers=8) as pool:
        buoys = [b for b in pool.map(lambda c: buoy(c[0], c[1], names), cands) if b]
    buoys.sort(key=lambda b: (b["region"], -b["lat"]))
    data = {"updated": iso(now), "source": "NOAA National Data Buoy Center", "regions": [r[0] for r in REGIONS], "buoys": buoys}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    with_swell = sum(1 for b in buoys if "swH" in b)
    with_parts = sum(1 for b in buoys if len(b.get("swells", [])) > 1)
    print(f"Wrote {out_path}: {len(buoys)} buoys ({with_swell} with swell split, {with_parts} with 2+ swells from spectra)")
    if not buoys:
        sys.exit("No buoy returned data; keeping the previous file.")


if __name__ == "__main__":
    main()
