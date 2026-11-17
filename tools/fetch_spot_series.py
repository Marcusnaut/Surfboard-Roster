#!/usr/bin/env python3
"""Model swell, wind and tide for every built-in spot, in one file the map can load at once.

The map's surf rating, swell pulse and shore hazard need swell, wind and tide for many spots. Asking
Open-Meteo from the phone for each view was slow (and can hit its per-address rate limit), so this job
asks once for all spots, in large combined requests, and writes spotseries.json on the data branch.

Values every STEP hours for DAYS days, packed as integers to keep the file small:
  sh  swell height, dm        sp  swell period, s       sd  swell direction, deg (from)
  wh  total wave height, dm   th  tide (sea level), cm  ws  wind speed, dm/s     wd  wind direction, deg
Missing values are null. Times: t0 (UTC) + i * step hours.

Refreshes when the previous file is older than REFRESH_HOURS; otherwise keeps it.
Data: Open-Meteo.com (CC BY 4.0), non-commercial use.
Usage: python3 tools/fetch_spot_series.py SPOTS_JSON OUTPUT_PATH [PREV_PATH]
Standard library only.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
MARINE = "https://marine-api.open-meteo.com/v1/marine"
WEATHER = "https://api.open-meteo.com/v1/forecast"
DAYS = 6
STEP = 2
CHUNK = 100
REFRESH_HOURS = 5.5


def get(url, params):
    q = urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                j = json.load(r)
                return j if isinstance(j, list) else [j]
        except Exception as e:  # noqa: BLE001
            print(f"{url.split('/')[2]} attempt {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return None


def pack(vals, scale):
    return [None if v is None else round(v * scale) for v in vals]


def main():
    spots_path, out_path = sys.argv[1], sys.argv[2]
    prev_path = sys.argv[3] if len(sys.argv) > 3 else ""
    now = datetime.now(timezone.utc)
    if prev_path and os.path.exists(prev_path):
        try:
            prev = json.load(open(prev_path))
            age = now - datetime.strptime(prev["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if age < timedelta(hours=REFRESH_HOURS) and prev.get("spots"):
                json.dump(prev, open(out_path, "w"), separators=(",", ":"))
                print(f"Kept spot series from {prev['updated']}")
                return
        except Exception:  # noqa: BLE001
            pass
    data = json.load(open(spots_path))
    spots = [s for s in (data["spots"] if isinstance(data, dict) else data) if s.get("lat") is not None]
    out, t0 = {}, None
    for i in range(0, len(spots), CHUNK):
        chunk = spots[i:i + CHUNK]
        base = {"latitude": ",".join(f"{s['lat']:.3f}" for s in chunk), "longitude": ",".join(f"{s['lon']:.3f}" for s in chunk),
                "forecast_days": DAYS, "timezone": "GMT"}
        m = get(MARINE, {**base, "cell_selection": "sea",
                         "hourly": "swell_wave_height,swell_wave_period,swell_wave_direction,wave_height,wave_period,wave_direction,sea_level_height_msl"})
        w = get(WEATHER, {**base, "hourly": "wind_speed_10m,wind_direction_10m", "wind_speed_unit": "ms"})
        for k, s in enumerate(chunk):
            mh = m[k]["hourly"] if m and k < len(m) and "hourly" in m[k] else None
            wh = w[k]["hourly"] if w and k < len(w) and "hourly" in w[k] else None
            h = mh or wh
            if not h:
                continue
            if t0 is None:
                t0 = h["time"][0]
            pick = lambda src, key: (src or {}).get(key, [None] * len(h["time"]))[::STEP]  # noqa: E731
            either = lambda a, b: [x if x is not None else y for x, y in zip(pick(mh, a), pick(mh, b))]  # noqa: E731
            out[s["id"]] = {
                "sh": pack(either("swell_wave_height", "wave_height"), 10),
                "sp": pack(either("swell_wave_period", "wave_period"), 1),
                "sd": pack(either("swell_wave_direction", "wave_direction"), 1),
                "wh": pack(pick(mh, "wave_height"), 10),
                "th": pack(pick(mh, "sea_level_height_msl"), 100),
                "ws": pack(pick(wh, "wind_speed_10m"), 10),
                "wd": pack(pick(wh, "wind_direction_10m"), 1),
            }
        time.sleep(1)
    if not out:
        sys.exit("No spot series fetched; keeping the previous file.")
    result = {"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "t0": t0 + "Z", "step": STEP,
              "source": "Open-Meteo.com (CC BY 4.0)", "spots": out}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"))
    print(f"Wrote {out_path}: {len(out)} of {len(spots)} spots, {os.path.getsize(out_path) // 1024} KB")


if __name__ == "__main__":
    main()
