#!/usr/bin/env python3
"""Wind forecast grids for the map's wind layers, over the home area (Point Conception to Malibu and the islands).

Models (each written to wind/<key>.json on the data branch):
  hrrr  NOAA HRRR, 3 km, hourly to 48 h (00/06/12/18 z runs)      NOMADS grib filter
  gfs   NOAA GFS 0.25 deg, 3-hourly to 120 h                         NOMADS grib filter
  ecmwf ECMWF IFS 0.25 deg via Open-Meteo (CC BY 4.0), 3-hourly    Open-Meteo
NOAA data are US public domain. HRRR gives winds relative to their Lambert grid; they are rotated to
north/east before resampling. (NOAA retired the NAM model from NOMADS in 2026.) Every model is resampled (nearest point) to one regular latitude/longitude grid
so the app can draw them the same way.

Packing: u and v (m/s, east and north) for every time, as signed bytes in units of 0.5 m/s, base64. Index
order: time, then row (north to south), then column (west to east).

Usage: python3 tools/fetch_wind.py OUT_DIR [PREV_DIR]
Keeps a model's previous file when it is newer than REFRESH_HOURS or when fetching fails.
Needs: pygrib, numpy (pip).
"""
import base64
import json
import math
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
# Home area box and output grid (about 4.5 km).
SOUTH, NORTH, WEST, EAST = 33.55, 34.75, -120.75, -118.45
STEP = 0.04
LATS = np.round(np.arange(NORTH, SOUTH - 1e-9, -STEP), 4)
LONS = np.round(np.arange(WEST, EAST + 1e-9, STEP), 4)
REFRESH_HOURS = 2.5  # ECMWF: 6 hours (new runs every 6 hours, and it counts against Open-Meteo's daily limit)
NOMADS = "https://nomads.ncep.noaa.gov/cgi-bin/"
MODELS = {
    "hrrr": {"name": "HRRR", "filter": "filter_hrrr_2d.pl", "runs": [0, 6, 12, 18], "hours": list(range(0, 49)), "lag": 2,
             "file": "hrrr.t{hh}z.wrfsfcf{f:02d}.grib2", "dir": "/hrrr.{ymd}/conus"},
    "gfs": {"name": "GFS", "filter": "filter_gfs_0p25.pl", "runs": [0, 6, 12, 18], "hours": list(range(0, 121, 3)), "lag": 4,
            "file": "gfs.t{hh}z.pgrb2.0p25.f{f:03d}", "dir": "/gfs.{ymd}/{hh}/atmos"},
}


def http(url, binary=False, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")


def nomads_url(m, run, f):
    ymd, hh = run.strftime("%Y%m%d"), f"{run.hour:02d}"
    q = {"file": m["file"].format(hh=hh, f=f), "var_UGRD": "on", "var_VGRD": "on", "lev_10_m_above_ground": "on", "subregion": "",
         "leftlon": WEST - 0.3, "rightlon": EAST + 0.3, "toplat": NORTH + 0.3, "bottomlat": SOUTH - 0.3,
         "dir": m["dir"].format(ymd=ymd, hh=hh)}
    return NOMADS + m["filter"] + "?" + urllib.parse.urlencode(q)


def latest_run(m):
    """Newest run whose last forecast hour is already posted."""
    now = datetime.now(timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0)
    for back in range(0, 36):
        run = t - timedelta(hours=back)
        if run.hour not in m["runs"] or now - run < timedelta(hours=m["lag"]):
            continue
        try:
            data = http(nomads_url(m, run, m["hours"][-1]), binary=True, timeout=60)
            if data[:4] == b"GRIB":
                return run
        except Exception:  # noqa: BLE001
            pass
    return None


def grib_uv(data):
    """(lats, lons, u, v) earth-relative from a GRIB2 message pair."""
    import pygrib
    with tempfile.NamedTemporaryFile(suffix=".grib2") as fh:
        fh.write(data); fh.flush()
        g = pygrib.open(fh.name)
        u = v = None
        for msg in g:
            if msg.shortName in ("10u", "u", "ugrd"):
                u = msg
            elif msg.shortName in ("10v", "v", "vgrd"):
                v = msg
        if u is None or v is None:
            raise ValueError("no u/v in file")
        lats, lons = u.latlons()
        uu, vv = np.array(u.values, dtype=float), np.array(v.values, dtype=float)
        if u.gridType == "lambert" and u["uvRelativeToGrid"] == 1:
            # Rotate grid-relative winds to earth-relative (Lambert conformal, tangent cone constant).
            lov = u["LoVInDegrees"]; l1, l2 = math.radians(u["Latin1InDegrees"]), math.radians(u["Latin2InDegrees"])
            n = math.sin(l1) if abs(l1 - l2) < 1e-6 else math.log(math.cos(l1) / math.cos(l2)) / math.log(math.tan(math.pi / 4 + l2 / 2) / math.tan(math.pi / 4 + l1 / 2))
            ang = np.radians(n * (((lons - lov + 180) % 360) - 180))
            c, s = np.cos(ang), np.sin(ang)
            uu, vv = c * uu + s * vv, -s * uu + c * vv
        lons = ((lons + 180) % 360) - 180
        return lats, lons, uu, vv


def resample(lats, lons, u, v):
    """Nearest model point for every output grid cell."""
    pts_lat, pts_lon = lats.ravel(), lons.ravel()
    k = math.cos(math.radians((SOUTH + NORTH) / 2))
    gl, go = np.meshgrid(LATS, LONS, indexing="ij")
    out_u = np.empty(gl.shape); out_v = np.empty(gl.shape)
    uf, vf = u.ravel(), v.ravel()
    for i in range(gl.shape[0]):
        d = (pts_lat[None, :] - gl[i][:, None]) ** 2 + ((pts_lon[None, :] - go[i][:, None]) * k) ** 2
        j = d.argmin(axis=1)
        out_u[i], out_v[i] = uf[j], vf[j]
    return out_u, out_v


def pack(arrs):
    q = np.clip(np.round(np.stack(arrs) * 2), -127, 127).astype(np.int8)
    return base64.b64encode(q.tobytes()).decode()


def fetch_noaa(key):
    m = MODELS[key]
    run = latest_run(m)
    if not run:
        raise RuntimeError("no complete run found")
    us, vs, times = [], [], []
    for f in m["hours"]:
        data = None
        for attempt in range(3):
            try:
                data = http(nomads_url(m, run, f), binary=True, timeout=90)
                if data[:4] == b"GRIB":
                    break
            except Exception as e:  # noqa: BLE001
                print(f"{key} f{f} attempt {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(2 + 3 * attempt)
        if not data or data[:4] != b"GRIB":
            continue
        lats, lons, u, v = grib_uv(data)
        gu, gv = resample(lats, lons, u, v)
        us.append(gu); vs.append(gv); times.append(run + timedelta(hours=f))
        time.sleep(0.3)  # be polite to NOMADS
    if len(times) < 4:
        raise RuntimeError("too few hours")
    return m["name"], run, times, us, vs, "NOAA NCEP via NOMADS (US public domain)"


def fetch_ecmwf():
    gl, go = np.meshgrid(LATS[::3], LONS[::3], indexing="ij")  # 0.12 deg is enough for a 0.25 deg model
    pts = list(zip(gl.ravel(), go.ravel()))
    hourly = None
    out = []
    for i in range(0, len(pts), 100):
        chunk = pts[i:i + 100]
        q = urllib.parse.urlencode({"latitude": ",".join(f"{a:.3f}" for a, _ in chunk), "longitude": ",".join(f"{b:.3f}" for _, b in chunk),
                                    "hourly": "wind_speed_10m,wind_direction_10m", "wind_speed_unit": "ms", "models": "ecmwf_ifs025",
                                    "forecast_days": 6, "timezone": "GMT"})
        j = json.loads(http("https://api.open-meteo.com/v1/forecast?" + q, timeout=120))
        out += j if isinstance(j, list) else [j]
        time.sleep(1)
    hourly = out[0]["hourly"]["time"]
    idx = [k for k, t in enumerate(hourly) if int(t[11:13]) % 3 == 0]
    times = [datetime.strptime(hourly[k], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) for k in idx]
    us, vs, keep = [], [], []
    for n, k in enumerate(idx):
        sp = np.array([(p["hourly"]["wind_speed_10m"][k] if p["hourly"]["wind_speed_10m"][k] is not None else np.nan) for p in out]).reshape(gl.shape)
        dr = np.array([(p["hourly"]["wind_direction_10m"][k] if p["hourly"]["wind_direction_10m"][k] is not None else np.nan) for p in out]).reshape(gl.shape)
        if np.isnan(sp).all():
            continue
        u = -sp * np.sin(np.radians(dr)); v = -sp * np.cos(np.radians(dr))
        gu, gv = resample(gl, go, np.nan_to_num(u), np.nan_to_num(v))
        us.append(gu); vs.append(gv); keep.append(times[n])
    run = keep[0]
    return "ECMWF", run, keep, us, vs, "ECMWF IFS via Open-Meteo.com (CC BY 4.0)"


def main():
    out_dir = sys.argv[1]
    prev_dir = sys.argv[2] if len(sys.argv) > 2 else ""
    os.makedirs(os.path.join(out_dir, "wind"), exist_ok=True)
    now = datetime.now(timezone.utc)
    for key in ["hrrr", "gfs", "ecmwf"]:
        path = os.path.join(out_dir, "wind", f"{key}.json")
        prev = os.path.join(prev_dir, "wind", f"{key}.json") if prev_dir else ""
        if prev and os.path.exists(prev):
            try:
                p = json.load(open(prev))
                if now - datetime.strptime(p["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=6 if key == "ecmwf" else REFRESH_HOURS):
                    json.dump(p, open(path, "w"), separators=(",", ":"))
                    print(f"{key}: kept file from {p['updated']}")
                    continue
            except Exception:  # noqa: BLE001
                pass
        try:
            name, run, times, us, vs, source = fetch_ecmwf() if key == "ecmwf" else fetch_noaa(key)
        except Exception as e:  # noqa: BLE001
            print(f"{key}: {e}; keeping the previous file", file=sys.stderr)
            if prev and os.path.exists(prev):
                with open(prev) as a, open(path, "w") as b:
                    b.write(a.read())
            continue
        doc = {"model": name, "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "run": run.strftime("%Y-%m-%dT%H:%MZ"),
               "times": [t.strftime("%Y-%m-%dT%H:%MZ") for t in times], "north": float(LATS[0]), "west": float(LONS[0]), "step": STEP,
               "ny": len(LATS), "nx": len(LONS), "scale": 0.5, "u": pack(us), "v": pack(vs), "source": source}
        json.dump(doc, open(path, "w"), separators=(",", ":"))
        print(f"{key}: run {doc['run']}, {len(times)} times, {os.path.getsize(path) // 1024} KB")


if __name__ == "__main__":
    main()
