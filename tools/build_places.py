#!/usr/bin/env python3
"""Build places.json: coastal city and town names for the map's own place labels.

Source: USGS The National Map, National Boundaries Dataset, "Incorporated Place" (govunits layer 24),
which carries the Census place name and population. Only places within NEAR_KM of a surf spot are kept.
A name with a common name in brackets, like "San Buenaventura (Ventura)", uses the bracketed one.

Run by hand when needed (places change rarely); the result is committed next to spots.json.
Usage: python3 tools/build_places.py SPOTS_JSON OUTPUT_PATH
Standard library only.
"""
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
QUERY = "https://carto.nationalmap.gov/arcgis/rest/services/govunits/MapServer/24/query"
NEAR_KM = 30
TILE = 2.0


def get(params):
    url = QUERY + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            print(f"attempt {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return {}


def label_point(rings):
    """Area-weighted centroid of the largest ring (good enough for a label)."""
    best, best_a = None, 0
    for ring in rings:
        a = cx = cy = 0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            f = x1 * y2 - x2 * y1
            a += f; cx += (x1 + x2) * f; cy += (y1 + y2) * f
        if a and abs(a) > best_a:
            best_a, best = abs(a), (cy / (3 * a), cx / (3 * a))
    return best


def km(a, b, c, d):
    r = math.radians
    x = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def common_name(name):
    m = re.search(r"\(([^)]+)\)\s*$", name)
    return m.group(1).strip() if m else name.strip()


def main():
    data = json.load(open(sys.argv[1]))
    spots = [(s["lat"], s["lon"]) for s in (data["spots"] if isinstance(data, dict) else data)]
    cells = {(math.floor(la / TILE), math.floor(lo / TILE)) for la, lo in spots}
    cells |= {(a + da, b + db) for a, b in cells for da in (-1, 0, 1) for db in (-1, 0, 1)}
    found = {}
    for a, b in sorted(cells):
        s, w = a * TILE, b * TILE
        offset = 0
        while True:
            d = get({"where": "1=1", "geometry": f"{w},{s},{w + TILE},{s + TILE}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                     "spatialRel": "esriSpatialRelIntersects", "outFields": "place_name,population,state_name", "returnGeometry": "true",
                     "outSR": 4326, "maxAllowableOffset": 0.005, "geometryPrecision": 4, "resultOffset": offset, "resultRecordCount": 1000, "f": "json"})
            feats = d.get("features", [])
            for f in feats:
                at, g = f["attributes"], f.get("geometry") or {}
                pt = label_point(g.get("rings") or [])
                if not pt or not at.get("place_name"):
                    continue
                near = min(km(pt[0], pt[1], la, lo) for la, lo in spots)
                if near > NEAR_KM:
                    continue
                key = (at["place_name"], at.get("state_name"))
                if key not in found:
                    found[key] = [common_name(at["place_name"]), round(pt[0], 4), round(pt[1], 4), at.get("population") or 0]
            if not d.get("exceededTransferLimit") or not feats:
                break
            offset += len(feats)
        time.sleep(0.5)
    places = sorted(found.values(), key=lambda p: -p[3])
    json.dump({"source": "USGS The National Map, National Boundaries Dataset (incorporated places)", "fields": ["name", "lat", "lon", "population"],
               "places": places}, open(sys.argv[2], "w"), separators=(",", ":"))
    print(f"Wrote {sys.argv[2]}: {len(places)} places")


if __name__ == "__main__":
    main()
