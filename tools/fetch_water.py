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


FIB = "15a63495-8d9f-4a49-b43a-3092ef3106b9"
r = call("datastore_search", resource_id=FIB, limit=2)
print("fib total", r.get("total"))
print("fib fields", [f["id"] + ":" + f.get("type", "") for f in r["fields"]])
for rec in r["records"]:
    print("fib row", json.dumps(rec))
cols = [f["id"] for f in r["fields"]]
date = next(c for c in cols if "date" in c.lower())
print("sql", call("datastore_search_sql", sql=f'SELECT MAX("{date}") AS m, COUNT(*) AS n FROM "{FIB}" WHERE "{date}" >= \'2026-10-01\'')["records"])
rows = call("datastore_search_sql", sql=f'SELECT * FROM "{FIB}" WHERE "{date}" >= \'2026-11-01\' AND "{cols[cols.index(date)]}" IS NOT NULL LIMIT 5')["records"]
for x in rows:
    print("recent", json.dumps(x))
for c in cols:
    if any(k in c.lower() for k in ("analyte", "parameter", "unit", "county", "program", "source", "datasource")):
        print("distinct", c, call("datastore_search_sql", sql=f'SELECT "{c}", COUNT(*) n FROM "{FIB}" WHERE "{date}" >= \'2026-09-01\' GROUP BY "{c}" ORDER BY n DESC LIMIT 25')["records"])
p = call("package_show", id="surface-water-fecal-indicator-bacteria-results")
print("license", p.get("license_id"), p.get("license_title"), (p.get("notes") or "")[:1500])
