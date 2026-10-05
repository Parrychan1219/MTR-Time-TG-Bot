#!/usr/bin/env python3
"""Regenerate app/stations.json.

Inputs, in data/:
  mtr_lines_and_stations.csv       https://opendata.mtr.com.hk/data/mtr_lines_and_stations.csv
  light_rail_routes_and_stops.csv  https://opendata.mtr.com.hk/data/light_rail_routes_and_stops.csv
  osm.json                         Overpass, out center tags, HK bbox:
    node["railway"~"^(station|halt|tram_stop)$"](22.13,113.82,22.57,114.44);
    way["railway"="station"](...); node["public_transport"="station"](...);
  (Overpass returns 406 without a User-Agent; from the Mac it was also blocked
  through Shadowrocket, so fetch it from the server.)

Stations are matched by English or Chinese name; several OSM objects for one
station are averaged. MANUAL covers stations OSM did not tag as expected
(2026-10-05: Hong Kong, Diamond Hill). The script prints sanity distances --
look at them before committing a new file.
"""

import csv
import json
import math
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
LINE_ORDER = ["AEL", "TCL", "TML", "TKL", "EAL", "SIL", "TWL", "ISL", "KTL", "DRL"]
MANUAL = {"HOK": (22.284900, 114.158300), "DIH": (22.340100, 114.201600)}


def ll(e):
    return (e.get("lat") or e["center"]["lat"], e.get("lon") or e["center"]["lon"])


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("station", "").replace("stop", ""))


def tags(e):
    return e.get("tags", {})


def is_lr(t):
    return (t.get("railway") == "tram_stop" or t.get("station") == "light_rail"
            or t.get("light_rail") == "yes" or "Light Rail" in t.get("network", "") + t.get("network:en", ""))


def is_heavy(t):
    if is_lr(t):
        return False
    if not (t.get("railway") in ("station", "halt") or t.get("public_transport") == "station"):
        return False
    ops = t.get("network", "") + t.get("operator", "") + t.get("network:en", "") + t.get("operator:en", "")
    return "MTR" in ops or t.get("station") in ("subway", "train")


def matches(e, en, zh):
    t = tags(e)
    return norm(t.get("name:en")) == norm(en) or t.get("name:zh", t.get("name", "")).split(" ")[0] == zh


def mean(cands):
    pts = [ll(e) for e in cands]
    return (round(sum(p[0] for p in pts) / len(pts), 6), round(sum(p[1] for p in pts) / len(pts), 6))


def dist(a, b):
    r = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    return 2 * r * math.asin(math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))


def main():
    osm = json.load(open(os.path.join(DATA, "osm.json")))["elements"]
    out = {"mtr": {}, "lrt": {}}

    heavy = {}
    for r in csv.DictReader(open(os.path.join(DATA, "mtr_lines_and_stations.csv"), encoding="utf-8-sig")):
        if r["Station Code"]:
            h = heavy.setdefault(r["Station Code"], {"en": r["English Name"], "zh": r["Chinese Name"], "lines": []})
            if r["Line Code"] not in h["lines"]:
                h["lines"].append(r["Line Code"])
    for code, h in heavy.items():
        cands = [e for e in osm if is_heavy(tags(e)) and matches(e, h["en"], h["zh"])]
        if code in MANUAL:
            lat, lon = MANUAL[code]
        elif cands:
            lat, lon = mean(cands)
        else:
            raise SystemExit("no coordinates for %s %s -- add it to MANUAL" % (code, h["en"]))
        out["mtr"][code] = {"en": h["en"], "zh": h["zh"], "lat": lat, "lon": lon,
                            "lines": sorted(h["lines"], key=LINE_ORDER.index)}

    lrt = {}
    for r in csv.DictReader(open(os.path.join(DATA, "light_rail_routes_and_stops.csv"), encoding="utf-8-sig")):
        s = lrt.setdefault(r["Stop ID"], {"en": r["English Name"], "zh": r["Chinese Name"], "routes": []})
        if r["Line Code"] not in s["routes"]:
            s["routes"].append(r["Line Code"])
    for sid, s in lrt.items():
        cands = [e for e in osm if is_lr(tags(e)) and matches(e, s["en"], s["zh"])]
        if not cands:
            raise SystemExit("no coordinates for LRT %s %s" % (sid, s["en"]))
        lat, lon = mean(cands)
        out["lrt"][sid] = {"en": s["en"], "zh": s["zh"], "lat": lat, "lon": lon,
                           "routes": sorted(s["routes"], key=lambda x: (int(re.sub(r"\D", "", x)), x))}

    m = out["mtr"]
    g = lambda k: (m[k]["lat"], m[k]["lon"])
    for a, b in [("HOK", "CEN"), ("DIH", "WTS"), ("KOW", "AUS"), ("TUM", "SIH")]:
        print("%s-%s %d m" % (a, b, dist(g(a), g(b))))
    nn = sorted((min(dist(g(k), g(j)) for j in m if j != k), k) for k in m)
    print("MTR nearest-neighbour: min %s, max %s" % (nn[0], nn[-1]))
    print("%d MTR stations, %d LRT stops" % (len(m), len(out["lrt"])))
    with open(os.path.join(ROOT, "app", "stations.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
