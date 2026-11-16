#!/usr/bin/env python3
"""Probe the California beach water-quality data on data.ca.gov (State Water Board BeachWatch, ODC-BY).

Prints the fields and a few rows of the stations and results tables so the real fetcher can be written
against the actual column names. Usage: python3 tools/fetch_water.py
"""
import json
import urllib.parse
import urllib.request

UA = "SurfboardTracker/1.0 (personal surf log; github.com/Marcusnaut/Surfboard-Roster)"
API = "https://data.ca.gov/api/3/action/"
RESULTS = "7bd961cf-abe4-433b-8033-378161237ff3"
STATIONS = "98e628ff-d012-4982-ad32-b9f9ad8ab524"


def call(action, **params):
    url = API + action + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["result"]


for name, rid in (("stations", STATIONS), ("results", RESULTS)):
    try:
        r = call("datastore_search", resource_id=rid, limit=3)
        print(name, "total", r.get("total"))
        print(name, "fields", [f["id"] + ":" + f.get("type", "") for f in r["fields"]])
        for rec in r["records"]:
            print(name, "row", json.dumps(rec)[:600])
    except Exception as e:  # noqa: BLE001
        print(name, "error", e)
for q in ('SELECT MAX("SampleDate") AS m FROM "%s"' % RESULTS,):
    try:
        print("sql", q, call("datastore_search_sql", sql=q)["records"])
    except Exception as e:  # noqa: BLE001
        print("sql error", e)
try:
    r = call("package_search", q="Safe to Swim fecal indicator bacteria", rows=5)
    for p in r["results"]:
        print("pkg", p["name"], p.get("metadata_modified"), p.get("license_id"))
        for res in p.get("resources", []):
            print("   res", res["id"], res.get("name"), res.get("last_modified"), res.get("datastore_active"))
except Exception as e:  # noqa: BLE001
    print("pkg error", e)
