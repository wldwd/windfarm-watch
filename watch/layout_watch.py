#!/usr/bin/env python3
"""Watch the Ambrosia Wind Farm public map for layout changes.

The developer's map page (ambrosiawindfarm.com.au/map) embeds a Google My Maps
map. Its KML export holds the indicative turbine layout and the project
boundary, each with a date. This script downloads it, compares it with the
last snapshot (data/ambrosia_layout.json) and reports turbines added, removed
or moved, and any change to the layout or boundary dates.

Distances are measured from the edge of the cut-out in the project boundary
(the land excluded from the project), because that is public. No private
coordinates are stored or published.

Run after watch/watch.py. It adds to data/changes.json, alert.md and
alert_title.txt, adds a line to data/status.json "sources", and writes
data/ambrosia_turbines.csv (the current public layout, for QGIS).

Usage:
  python watch/layout_watch.py
  python watch/layout_watch.py --kml FILE   # test with a saved KML
"""
import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPSHOT = DATA / "ambrosia_layout.json"
KML_URL = "https://www.google.com/maps/d/kml?mid=15Z76ZUWZdM0ojhpCmEBtE7FC3fKjWw4&forcekml=1"
PAGE_URL = "https://ambrosiawindfarm.com.au/map/"
SOURCE_NAME = "Ambrosia public map (turbine layout)"
MOVE_THRESHOLD_M = 10
PERTH = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; WindFarmWatch/1.0; family approvals tracker)"
NS = {"k": "http://www.opengis.net/kml/2.2"}


def now_perth():
    return dt.datetime.now(PERTH).replace(microsecond=0)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(4 * (i + 1))
    raise last


# ---------- geometry ----------

def to_mga50(lat, lon):
    """GDA2020 lat/lon to MGA zone 50 easting/northing (Transverse Mercator, GRS80)."""
    a, f, k0 = 6378137.0, 1 / 298.257222101, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    phi, lam = math.radians(lat), math.radians(lon - 117)
    n = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    t, c, aa = math.tan(phi) ** 2, ep2 * math.cos(phi) ** 2, math.cos(phi) * lam
    m = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
             - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi)
             - (35 * e2 ** 3 / 3072) * math.sin(6 * phi))
    x = k0 * n * (aa + (1 - t + c) * aa ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * aa ** 5 / 120) + 500000
    y = k0 * (m + n * math.tan(phi) * (aa * aa / 2 + (5 - t + 9 * c + 4 * c * c) * aa ** 4 / 24
                                       + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * aa ** 6 / 720)) + 10000000
    return x, y


def seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    u = 0 if L == 0 else max(0, min(1, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - (ax + u * dx), p[1] - (ay + u * dy))


def inside(p, ring):
    x, y, c = p[0], p[1], False
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def dist_to_ring(p, ring):
    d = min(seg_dist(p, ring[i - 1], ring[i]) for i in range(len(ring)))
    return 0.0 if inside(p, ring) else d


# ---------- KML parsing ----------

def parse_coords(text):
    pts = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            pts.append((float(parts[1]), float(parts[0])))  # lat, lon
    return pts


def ext(pm):
    out = {}
    for d in pm.findall(".//k:ExtendedData/k:Data", NS):
        out[d.get("name")] = (d.findtext("k:value", default="", namespaces=NS) or "").strip()
    desc = pm.findtext("k:description", default="", namespaces=NS) or ""
    for k, v in re.findall(r"(\w+):\s*([^<]+)", desc):
        out.setdefault(k, v.strip())
    return out


def norm_date(s):
    m = re.match(r"(\d{4})[/-]?(\d{2})[/-]?(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else (s or "")


def parse_kml(raw):
    root = ET.fromstring(raw)
    folders, turbines, dates, boundary, holes = [], {}, set(), {}, []
    for folder in root.iter(f"{{{NS['k']}}}Folder"):
        fname = (folder.findtext("k:name", default="", namespaces=NS) or "").strip()
        folders.append(fname)
        for pm in folder.findall("k:Placemark", NS):
            e = ext(pm)
            if e.get("Turbine_No"):
                no = str(int(float(e["Turbine_No"])))
                try:
                    east, north = float(e.get("Longitude", "")), float(e.get("Latitude", ""))
                    if east < 100000 or north < 1000000:
                        raise ValueError
                except ValueError:
                    pt = parse_coords(pm.findtext(".//k:Point/k:coordinates", default="", namespaces=NS))
                    if not pt:
                        continue
                    east, north = to_mga50(*pt[0])
                turbines[no] = [round(east), round(north)]
                if e.get("Date"):
                    dates.add(norm_date(e["Date"]))
            elif e.get("Area_ha") or "boundary" in fname.lower():
                boundary = {"date": norm_date(e.get("Date", "")), "area_ha": e.get("Area_ha", "")}
                for ib in pm.findall(".//k:innerBoundaryIs//k:coordinates", NS):
                    ring = [to_mga50(lat, lon) for lat, lon in parse_coords(ib.text)]
                    if len(ring) >= 3:
                        holes.append(ring)
                boundary["holes"] = len(holes)
    return {"folders": folders, "turbines": turbines, "layout_dates": sorted(dates), "boundary": boundary}, holes


# ---------- comparison ----------

def describe(no, xy, holes):
    if not holes:
        return f"T{no}"
    d = min(dist_to_ring(xy, h) for h in holes)
    return f"T{no} ({d / 1000:.1f} km from the cut-out)"


def compare(old, new, holes):
    lines = []
    ot, nt = old.get("turbines", {}), new["turbines"]
    removed = sorted(set(ot) - set(nt), key=int)
    added = sorted(set(nt) - set(ot), key=int)
    moved = []
    for k in sorted(set(ot) & set(nt), key=int):
        d = math.hypot(ot[k][0] - nt[k][0], ot[k][1] - nt[k][1])
        if d > MOVE_THRESHOLD_M:
            moved.append((k, d))
    if old.get("layout_dates") != new["layout_dates"]:
        lines.append(f"Layout date changed from {', '.join(old.get('layout_dates') or ['none'])} to {', '.join(new['layout_dates'] or ['none'])}.")
    if len(ot) != len(nt):
        lines.append(f"Turbine count changed from {len(ot)} to {len(nt)}.")
    if removed:
        lines.append("Removed: " + ", ".join(describe(k, ot[k], holes) for k in removed) + ".")
    if added:
        lines.append("Added: " + ", ".join(describe(k, nt[k], holes) for k in added) + ".")
    if moved:
        parts = []
        for k, d in moved:
            if holes:
                before = min(dist_to_ring(ot[k], h) for h in holes)
                after = min(dist_to_ring(nt[k], h) for h in holes)
                parts.append(f"T{k} by {d:.0f} m ({before / 1000:.2f} to {after / 1000:.2f} km from the cut-out)")
            else:
                parts.append(f"T{k} by {d:.0f} m")
        lines.append("Moved: " + "; ".join(parts) + ".")
    ob, nb = old.get("boundary", {}), new["boundary"]
    if ob and nb and (ob.get("date") != nb.get("date") or ob.get("area_ha") != nb.get("area_ha") or ob.get("holes") != nb.get("holes")):
        lines.append(f"Project boundary changed: date {ob.get('date')} to {nb.get('date')}, area {ob.get('area_ha')} to {nb.get('area_ha')} ha, cut-outs {ob.get('holes')} to {nb.get('holes')}.")
    if old.get("folders") and set(old["folders"]) != set(new["folders"]):
        gone = sorted(set(old["folders"]) - set(new["folders"]))
        came = sorted(set(new["folders"]) - set(old["folders"]))
        lines.append("Map layers changed." + (f" New: {', '.join(came)}." if came else "") + (f" Gone: {', '.join(gone)}." if gone else ""))
    return lines


def nearest_summary(turbines, holes):
    if not holes or not turbines:
        return ""
    ds = sorted((min(dist_to_ring(xy, h) for h in holes), k) for k, xy in turbines.items())
    near = ", ".join(f"T{k} {d / 1000:.2f} km" for d, k in ds[:3])
    within = sum(1 for d, _ in ds if d <= 2000)
    return (f"Distances are to the edge of the land cut out of the project boundary, not to the houses. "
            f"Nearest turbines to that edge: {near}. Turbines within 2 km of it: {within}.")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kml", help="read this KML file instead of downloading")
    args = ap.parse_args()

    stamp = now_perth()
    status = load_json(DATA / "status.json", {})
    health = {"id": "ambrosia-map", "name": SOURCE_NAME, "url": PAGE_URL, "checked": stamp.isoformat(), "ok": True, "error": ""}
    try:
        raw = Path(args.kml).read_bytes() if args.kml else fetch(KML_URL)
        new, holes = parse_kml(raw)
        if not new["turbines"]:
            raise ValueError("no turbines found in the map; it may have been reorganised")
    except Exception as e:
        health["ok"], health["error"] = False, f"{type(e).__name__}: {str(e)[:160]}"
        status.setdefault("sources", [])
        status["sources"] = [s for s in status["sources"] if s.get("id") != "ambrosia-map"] + [health]
        save_json(DATA / "status.json", status)
        print(f"ERR ambrosia-map: {health['error']}")
        return 0

    old = load_json(SNAPSHOT, {})
    lines = compare(old, new, holes) if old.get("turbines") else []
    health["matches"] = len(new["turbines"])

    # Current layout as CSV (public data) for QGIS.
    with open(DATA / "ambrosia_turbines.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Turbine_No", "Easting_MGA50", "Northing_MGA50", "Layout_date"])
        for k in sorted(new["turbines"], key=int):
            w.writerow([k, *new["turbines"][k], ";".join(new["layout_dates"])])

    if lines:
        summary = " ".join(lines)
        near = nearest_summary(new["turbines"], holes)
        text = f"The Ambrosia public map changed. {summary} {near}".strip()
        changes = load_json(DATA / "changes.json", [])
        changes.insert(0, {
            "date": stamp.isoformat(), "source": SOURCE_NAME, "source_id": "ambrosia-map", "url": PAGE_URL,
            "project": "Ambrosia Wind Farm", "kind": "developer", "flag": True, "text": text[:900],
        })
        save_json(DATA / "changes.json", changes[:300])

        alert_md, alert_title = ROOT / "alert.md", ROOT / "alert_title.txt"
        block = "\n".join([f"- **{SOURCE_NAME}** · Ambrosia Wind Farm:", *[f"  - {l}" for l in lines]] +
                          ([f"  - {near}"] if near else []) + [f"  {PAGE_URL}"])
        if alert_md.exists():
            body = alert_md.read_text(encoding="utf-8").rstrip() + "\n\n" + block + "\n"
        else:
            body = ("New wind farm approval activity found on " + stamp.strftime("%a %d %b %Y, %I:%M %p") +
                    " (Perth).\n\n" + block + "\n\nCheck the map before acting.\n")
            alert_title.write_text("Ambrosia turbine layout changed on the public map\n", encoding="utf-8")
        alert_md.write_text(body, encoding="utf-8")
        health["note"] = "layout changed"
    elif not old.get("turbines"):
        health["note"] = f"baseline recorded ({len(new['turbines'])} turbines)"

    new["fetched"] = stamp.isoformat()
    new["_about"] = old.get("_about", "Last seen state of the Ambrosia public map. Written by watch/layout_watch.py.")
    save_json(SNAPSHOT, new)

    status.setdefault("sources", [])
    status["sources"] = [s for s in status["sources"] if s.get("id") != "ambrosia-map"] + [health]
    save_json(DATA / "status.json", status)
    print(f"OK  ambrosia-map: {len(new['turbines'])} turbines, layout {', '.join(new['layout_dates'])}. "
          + ("Changes: " + " ".join(lines) if lines else "No change."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
