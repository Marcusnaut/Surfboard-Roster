"""Lists the time steps of CDIP's California wave-height grids (nowcast and forecast).

The app draws these grids as a map layer straight from CDIP's WMS server, but it needs
to know which hours exist. Browsers can't always read CDIP's capabilities document
(no CORS), so this script writes the list to wavegrid.json on the data branch.

Usage: python3 tools/fetch_wave_grid.py out/wavegrid.json
"""

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
WMS = "https://thredds.cdip.ucsd.edu/thredds/wms/cdip/model/MOP_grids/CA_0.01_{kind}.nc"
LAYER = "waveHs"


def parse_iso(s):
    s = s.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def parse_period(p):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", p.strip())
    if not m:
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi) or None


def expand(text):
    """A WMS time dimension is a comma list of instants and/or start/end/period ranges."""
    out = []
    for part in text.replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            a, b, p = (part.split("/") + [""])[:3]
            t, end, step = parse_iso(a), parse_iso(b), parse_period(p)
            if not step:
                out += [t, end]
                continue
            while t <= end and len(out) < 2000:
                out.append(t)
                t += step
        else:
            out.append(parse_iso(part))
    return sorted(set(out))


def times(kind):
    url = WMS.format(kind=kind) + "?service=WMS&version=1.3.0&request=GetCapabilities"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        root = ET.fromstring(r.read())
    for layer in root.iter():
        if not layer.tag.endswith("Layer"):
            continue
        name = next((c.text for c in layer if c.tag.endswith("Name")), None)
        if name != LAYER:
            continue
        for c in layer:
            if c.tag.endswith("Dimension") and c.get("name") == "time" and c.text:
                return [t.strftime("%Y-%m-%dT%H:%M:%S.000Z") for t in expand(c.text)]
    return []


def main():
    out_path = sys.argv[1]
    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "url": WMS.format(kind="{kind}"), "layer": LAYER}
    for kind in ("nowcast", "forecast"):
        try:
            out[kind] = times(kind)
        except Exception as ex:  # noqa: BLE001
            print(f"{kind}: {ex}")
            out[kind] = []
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {out_path}: {len(out['nowcast'])} nowcast and {len(out['forecast'])} forecast steps")


if __name__ == "__main__":
    main()
