#!/usr/bin/env python3
"""Storm tracking grids for the map's Storms layer: NOAA GFS sea-level pressure and 10 m wind over the Pacific.

The app draws isobars, marks highs and lows, and picks out gale-force wind (34 kt and up) over the ocean,
especially wind blowing toward the home area, which is what builds swell headed there.

Grid: 1 degree, 65 N to 65 S, 110 E to 60 W (lon 110..300 east), rows north to south, columns west to east.
Every 6 hours from the run time to 168 h.

Files (on the data branch):
  storms/index.json  run, source, grid, times, file names, land mask (bit-packed, base64)
  storms/fNNN.json   one forecast hour: u, v (signed bytes of 0.5 m/s), p (unsigned bytes, hPa = 925 + p / 2)

Usage: python3 tools/fetch_storms.py OUT_DIR [PREV_DIR]
Keeps the previous files when they are newer than REFRESH_HOURS or when fetching fails.
NOAA GFS data are US public domain. Needs: pygrib, numpy (pip).
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
NORTH, SOUTH, WEST, EAST = 65, -65, 110, 300
HOURS = list(range(0, 169, 6))
REFRESH_HOURS = 5.5
P0 = 925.0
FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_1p00.pl"


def http(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def url(run, f, land=False):
    ymd, hh = run.strftime("%Y%m%d"), f"{run.hour:02d}"
    q = {"file": f"gfs.t{hh}z.pgrb2.1p00.f{f:03d}", "subregion": "", "leftlon": WEST, "rightlon": EAST,
         "toplat": NORTH, "bottomlat": SOUTH, "dir": f"/gfs.{ymd}/{hh}/atmos"}
    if land:
        q.update({"var_LAND": "on", "lev_surface": "on"})
    else:
        q.update({"var_PRMSL": "on", "var_UGRD": "on", "var_VGRD": "on", "lev_10_m_above_ground": "on", "lev_mean_sea_level": "on"})
    return FILTER + "?" + urllib.parse.urlencode(q)


def fields(data):
    import pygrib
    with tempfile.NamedTemporaryFile(suffix=".grb") as fh:
        fh.write(data); fh.flush()
        out = {}
        g = pygrib.open(fh.name)
        for m in g:
            vals = np.asarray(m.values, dtype=float)
            lats, lons = m.latlons()
            if lats[0, 0] < lats[-1, 0]:  # make rows north to south
                vals, lats = vals[::-1], lats[::-1]
            out[m.shortName] = vals
        g.close()
    return out


def latest_run():
    now = datetime.now(timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0)
    for back in range(3, 30):
        run = t - timedelta(hours=back)
        if run.hour % 6:
            continue
        try:
            if len(http(url(run, HOURS[-1]), timeout=60)) > 1000:
                return run
        except Exception:
            pass
    return None


def b64(a):
    return base64.b64encode(a.tobytes()).decode()


def main():
    out = os.path.join(sys.argv[1], "storms")
    prev = os.path.join(sys.argv[2], "storms") if len(sys.argv) > 2 else None
    os.makedirs(out, exist_ok=True)
    old = None
    if prev and os.path.exists(os.path.join(prev, "index.json")):
        try:
            old = json.load(open(os.path.join(prev, "index.json")))
        except Exception:
            old = None
    if old and all(os.path.exists(os.path.join(prev, f)) for f in old["files"]):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(old["updated"].replace("Z", "+00:00"))
        if age < timedelta(hours=REFRESH_HOURS):
            print(f"storms: keeping previous ({age})")
            for f in old["files"] + ["index.json"]:
                shutil.copy(os.path.join(prev, f), os.path.join(out, f))
            return
    run = latest_run()
    if not run:
        raise RuntimeError("no complete GFS run found")
    if old and old.get("run") == run.strftime("%Y-%m-%dT%HZ") and all(os.path.exists(os.path.join(prev, f)) for f in old["files"]):
        print("storms: same run as before, keeping it")
        old["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for f in old["files"]:
            shutil.copy(os.path.join(prev, f), os.path.join(out, f))
        json.dump(old, open(os.path.join(out, "index.json"), "w"), separators=(",", ":"))
        return
    land = None
    try:
        lf = fields(http(url(run, 0, land=True)))
        land = next(iter(lf.values()))
    except Exception as e:
        print("storms: no land mask", e)
    files, times, shape = [], [], None
    for f in HOURS:
        for attempt in range(4):
            try:
                fl = fields(http(url(run, f)))
                break
            except Exception as e:
                if attempt == 3:
                    raise
                time.sleep(3 + 4 * attempt)
        u, v, p = fl["10u"], fl["10v"], fl["prmsl"] / 100.0
        shape = u.shape
        U = np.clip(np.round(u * 2), -127, 127).astype(np.int8)
        V = np.clip(np.round(v * 2), -127, 127).astype(np.int8)
        P = np.clip(np.round((p - P0) * 2), 0, 255).astype(np.uint8)
        t = run + timedelta(hours=f)
        name = f"f{f:03d}.json"
        json.dump({"t": t.strftime("%Y-%m-%dT%H:%MZ"), "u": b64(U), "v": b64(V), "p": b64(P)},
                  open(os.path.join(out, name), "w"), separators=(",", ":"))
        files.append(name); times.append(t.strftime("%Y-%m-%dT%H:%MZ"))
        time.sleep(0.3)
    ny, nx = shape
    idx = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "run": run.strftime("%Y-%m-%dT%HZ"),
           "model": "GFS", "source": "NOAA NCEP GFS via NOMADS (US public domain)",
           "north": NORTH, "west": WEST, "step": 1, "nx": nx, "ny": ny, "scale": 0.5, "p0": P0,
           "times": times, "files": files}
    if land is not None and land.shape == shape:
        idx["land"] = base64.b64encode(np.packbits((land > 0.5).astype(np.uint8).ravel()).tobytes()).decode()
    json.dump(idx, open(os.path.join(out, "index.json"), "w"), separators=(",", ":"))
    print(f"storms: {run:%Y-%m-%d %HZ}, {len(files)} steps, grid {nx}x{ny}, land {'yes' if 'land' in idx else 'no'}")


if __name__ == "__main__":
    main()
