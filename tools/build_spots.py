#!/usr/bin/env python3
"""One-off helper: pull surf spots tagged sport=surfing from OpenStreetMap (Overpass API)
for the US West and East coasts and write them to a JSON file for review.

Run by .github/workflows/build-spots.yml on demand. Output feeds the curated spots.json.
Spot data (c) OpenStreetMap contributors, ODbL.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
BOXES = {
    "West Coast": (32.0, -125.5, 49.5, -116.8),
    "East Coast": (24.3, -82.5, 45.5, -65.0),
}


def query(box):
    s, w, n, e = box
    q = f"""[out:json][timeout:120];
(node["sport"="surfing"]({s},{w},{n},{e});
 way["sport"="surfing"]({s},{w},{n},{e});
 relation["sport"="surfing"]({s},{w},{n},{e}););
out center tags;"""
    body = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for url in ENDPOINTS:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as ex:  # noqa: BLE001
                last = ex
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Overpass failed: {last}")


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "spots_osm.json"
    spots = []
    for region, box in BOXES.items():
        data = query(box)
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            lat = el.get("lat", (el.get("center") or {}).get("lat"))
            lon = el.get("lon", (el.get("center") or {}).get("lon"))
            if lat is None or lon is None:
                continue
            spots.append({
                "osm": f"{el['type']}/{el['id']}", "name": tags.get("name") or tags.get("name:en") or "",
                "lat": round(lat, 5), "lon": round(lon, 5), "region": region,
                "tags": {k: v for k, v in tags.items() if k in ("surfing", "surfing:wave_type", "surfing:break", "natural", "description", "addr:state", "is_in:state")},
            })
        time.sleep(5)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"source": "OpenStreetMap contributors (ODbL)", "count": len(spots), "spots": spots}, f, indent=0)
    named = sum(1 for s in spots if s["name"])
    print(f"Wrote {out_path}: {len(spots)} spots, {named} named")


if __name__ == "__main__":
    main()
