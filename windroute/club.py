"""Club mode: set start points, and a strong preference for the club's own roads.

A club runs the same planner as the main site with two additions, both driven by
the club's library of known-good rides (GPX/TCX files in its folder):

- **Club roads.** Every road those rides use, as a grid of small cells. A route
  earns a bonus (`RouteWeights.w_club`) for the share of its in-town riding on them,
  so it leaves and re-enters town the way the club does, while the country part is
  ranked on wind, traffic and surface like any other plan.
- **Exits.** The usual ways out of town, found by following the rides from each
  start until they spread apart. They seed some candidates (the "club" shape routes
  through an exit's waypoints), because the routing service rarely picks the club's
  streets on its own; a bonus can only choose among routes it's offered.

`club.json` (hand-edited) names the club and its start points; `python -m
windroute.cli club-build <dir>` reads the rides and writes `club_data.json` (roads +
exits). Edit an exit's `enabled` / `home_only` there to change how it's used.
"""
from __future__ import annotations

import json
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .geocode import USER_AGENT
from .geometry import _bearing, _haversine_km, compass_label

CELL_DEG = 0.0002          # club-road grid (~22 m N-S, ~17 m E-W here)
NEAR_CELLS = 1             # a route point counts if a club cell is this many cells away
TOWN_KM_DEFAULT = 6.5      # "in town": within this of the start (club.json can override)
EXIT_FIND_KM = 4.5         # rides are grouped by where they cross this far out ...
EXIT_REACH_KM = 9.0        # ... and followed at most this far to see where they part
EXIT_SHARED_M = 40.0       # rides within this of the typical path are still "together"
EXIT_MIN_RIDES = 4         # an exit needs at least this many ride legs
EXIT_WAYPOINTS = 4         # waypoints a club candidate routes through on each exit
EXIT_MATCH_KM = 1.0        # a rebuilt exit ending this close to an old one is the same exit
DATA_FILE = "club_data.json"


# --------------------------------------------------------------------------- #
# Reading ride files
# --------------------------------------------------------------------------- #
def read_track(path) -> list:
    """[(lat, lng), ...] from a TCX course/activity or a GPX file."""
    root = ET.parse(path).getroot()
    pts = []
    if root.tag.endswith("TrainingCenterDatabase"):
        for tp in root.iter():
            if not tp.tag.endswith("Trackpoint"):
                continue
            la = lo = None
            for el in tp.iter():
                if el.tag.endswith("LatitudeDegrees"):
                    la = float(el.text)
                elif el.tag.endswith("LongitudeDegrees"):
                    lo = float(el.text)
            if la is not None and lo is not None:
                pts.append((la, lo))
        return pts
    for tag in ("trkpt", "rtept"):
        for el in root.findall(".//{*}" + tag):
            try:
                pts.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
            except (KeyError, ValueError):
                continue
        if pts:
            break
    return pts


def read_rides(routes_dir) -> list:
    """Every ride in the folder as {"file", "coords"}, exact duplicates dropped."""
    rides, seen = [], set()
    for p in sorted(Path(routes_dir).iterdir()):
        if p.suffix.lower() not in (".tcx", ".gpx"):
            continue
        try:
            coords = read_track(p)
        except ET.ParseError:
            continue
        if len(coords) < 2:
            continue
        key = (len(coords), coords[0], coords[-1])
        if key in seen:
            continue
        seen.add(key)
        rides.append({"file": p.name, "coords": coords})
    return rides


# --------------------------------------------------------------------------- #
# Club roads
# --------------------------------------------------------------------------- #
def _cell(lat, lng):
    return (math.floor(lat / CELL_DEG), math.floor(lng / CELL_DEG))


def road_cells(rides) -> set:
    """Grid cells every ride passes through (points interpolated every ~10 m)."""
    cells = set()
    for r in rides:
        co = r["coords"]
        for a, b in zip(co, co[1:]):
            steps = max(1, int(_haversine_km(a, b) * 100))      # every ~10 m
            for k in range(steps + 1):
                t = k / steps
                cells.add(_cell(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return cells


class ClubRoads:
    """Lookup of the club's roads (see `road_cells`)."""

    def __init__(self, cells):
        self.cells = set(cells)

    def on_club_road(self, p) -> bool:
        ci, cj = _cell(*p)
        n = NEAR_CELLS
        return any((ci + di, cj + dj) in self.cells
                   for di in range(-n, n + 1) for dj in range(-n, n + 1))

    def town_fraction(self, coords, center, town_km) -> float:
        """Share of the riding within `town_km` of `center` that's on club roads
        (0 when the route never rides in town)."""
        total = club = 0.0
        for a, b in zip(coords, coords[1:]):
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            if _haversine_km(center, mid) > town_km:
                continue
            d = _haversine_km(a, b)
            total += d
            if self.on_club_road(mid):
                club += d
        return club / total if total > 0 else 0.0


# --------------------------------------------------------------------------- #
# Exits: where the club's rides leave town
# --------------------------------------------------------------------------- #
def _legs(rides, center, start, max_start_km=0.3):
    """Each ride from `start` as two legs leading out of town: the way out, and the
    way home reversed (so both read start -> country), up to EXIT_REACH_KM out."""
    legs = []
    for r in rides:
        co = r["coords"]
        if _haversine_km(co[0], start) > max_start_km:
            continue
        for direction, leg in (("out", co), ("home", co[::-1])):
            far = [_haversine_km(center, p) for p in leg]
            # out to EXIT_REACH_KM, or to its farthest point if it turns back sooner
            i = next((k for k, d in enumerate(far) if d >= EXIT_REACH_KM),
                     max(range(len(far)), key=far.__getitem__))
            if any(_haversine_km(center, p) >= EXIT_FIND_KM for p in leg[:i + 1]):
                legs.append({"file": r["file"], "dir": direction, "coords": leg[:i + 1]})
    return legs


def _crossing(leg, center):
    return next(p for p in leg["coords"] if _haversine_km(center, p) >= EXIT_FIND_KM)


def _shared_until(path, others, begin=0):
    """Index along `path`, from `begin` on, where fewer than half of `others` (and
    >= 2) still ride within EXIT_SHARED_M of it: where the rides spread out."""
    need = max(2, len(others) // 2)
    grids = []
    for o in others:
        g = set()
        for p in o["coords"]:
            g.add(_cell(*p))
        grids.append(g)
    n = max(1, int(EXIT_SHARED_M / 20))
    last = begin
    for i in range(begin, len(path)):
        ci, cj = _cell(*path[i])
        near = sum(1 for g in grids if any((ci + di, cj + dj) in g
                                          for di in range(-n, n + 1)
                                          for dj in range(-n, n + 1)))
        if near < need:
            break
        last = i
    return last


def _medoid(legs):
    """The leg that shares the most road with the rest (the typical path)."""
    cells = [{(round(a, 3), round(b, 3)) for a, b in L["coords"]} for L in legs]
    if len(legs) == 1:
        return legs[0]
    best = max(range(len(legs)), key=lambda i: sum(
        len(cells[i] & cells[j]) / max(1, min(len(cells[i]), len(cells[j])))
        for j in range(len(legs)) if j != i))
    return legs[best]


def _thin(coords, step_km=0.025):
    out = [coords[0]]
    for p in coords[1:]:
        if _haversine_km(out[-1], p) >= step_km:
            out.append(p)
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def find_exits(rides, starts, center) -> list:
    """The club's ways out of town, each with a typical path from every start that
    uses it. Rides from all starts are grouped by where they cross EXIT_FIND_KM out;
    each group's path runs until the rides spread apart."""
    all_legs = []
    for s in starts:
        for L in _legs(rides, center, (s["lat"], s["lng"])):
            L["start"] = s["id"]
            all_legs.append(L)
    groups = []
    for L in all_legs:
        p = _crossing(L, center)
        for g in groups:
            if _haversine_km(g["at"], p) < 0.8:
                g["legs"].append(L)
                break
        else:
            groups.append({"at": p, "legs": [L]})
    groups = [g for g in groups if len(g["legs"]) >= EXIT_MIN_RIDES]
    groups.sort(key=lambda g: _bearing(center, g["at"]))
    exits = []
    for g in groups:
        typical = _medoid(g["legs"])
        cross = next(i for i, p in enumerate(typical["coords"])
                     if _haversine_km(center, p) >= EXIT_FIND_KM)
        end = _shared_until(typical["coords"],
                            [L for L in g["legs"] if L is not typical], begin=cross)
        end_pt = typical["coords"][end]
        paths = {}
        for s in starts:
            mine = [L for L in g["legs"] if L["start"] == s["id"]]
            leg = _medoid(mine) if mine else typical
            # follow this start's typical leg to the point nearest the group's end
            k = min(range(len(leg["coords"])),
                    key=lambda i: _haversine_km(leg["coords"][i], end_pt))
            path = leg["coords"][:k + 1] if mine else leg["coords"][:end + 1]
            paths[s["id"]] = [[round(a, 5), round(b, 5)] for a, b in _thin(path)]
        brg = _bearing(center, end_pt)
        exits.append({
            "id": f"{compass_label(brg)}-{len(exits) + 1}",
            "name": compass_label(brg), "bearing": round(brg, 1),
            "end": [round(end_pt[0], 5), round(end_pt[1], 5)],
            "end_km": round(_haversine_km(center, end_pt), 2),
            "rides": len(g["legs"]),
            "rides_out": sum(1 for L in g["legs"] if L["dir"] == "out"),
            "enabled": True, "home_only": False, "paths": paths,
        })
    return exits


def name_exits(exits, pause_s=1.1):
    """Name each exit after the road it leaves town on (OSM Nominatim reverse
    lookup of its end point): 'S via Curtis Rd'. Network; best-effort."""
    for ex in exits:
        try:
            r = requests.get("https://nominatim.openstreetmap.org/reverse",
                             params={"lat": ex["end"][0], "lon": ex["end"][1],
                                     "format": "jsonv2", "zoom": 17},
                             headers={"User-Agent": USER_AGENT}, timeout=20)
            road = (r.json().get("address") or {}).get("road")
        except (requests.RequestException, ValueError):
            road = None
        if road:
            ex["name"] = f"{compass_label(ex['bearing'])} via {road}"
        time.sleep(pause_s)                     # Nominatim: max 1 request/second
    return exits


# --------------------------------------------------------------------------- #
# The club, as the planner and web app use it
# --------------------------------------------------------------------------- #
@dataclass
class Club:
    name: str
    starts: list                           # [{id, name, address, lat, lng}]
    exits: list = field(default_factory=list)
    roads: ClubRoads = None
    town_km: float = TOWN_KM_DEFAULT
    weight: float = 0.5                    # RouteWeights.w_club in club plans
    center: tuple = (0.0, 0.0)
    embed_on: list = field(default_factory=list)   # sites allowed to frame the club page
    website: str = ""                      # the club's own site ("<- Club site" link)
    theme: str = ""                        # style.css theme ([data-brand="..."]), e.g. illini
    route_colors: list = field(default_factory=list)  # top pick + alternatives on the map

    def start(self, start_id):
        return next((s for s in self.starts if s["id"] == start_id), None)

    def exits_for(self, start_id) -> list:
        """Enabled exits as [{name, bearing, waypoints, home_only, km}] for a start."""
        out = []
        for ex in self.exits:
            path = ex["paths"].get(start_id)
            if not ex.get("enabled", True) or not path:
                continue
            out.append({"id": ex["id"], "name": ex["name"], "bearing": ex["bearing"],
                        "home_only": bool(ex.get("home_only")),
                        "waypoints": exit_waypoints(path),
                        "km": sum(_haversine_km(a, b) for a, b in zip(path, path[1:]))})
        return out

    def club_frac(self, coords, start_id) -> float:
        s = self.start(start_id) or self.starts[0]
        return self.roads.town_fraction(coords, (s["lat"], s["lng"]), self.town_km)


def exit_waypoints(path, n=EXIT_WAYPOINTS) -> list:
    """`n` points spread evenly (by distance) along an exit, the last at its end,
    skipping the start itself: the route is pinned to the club's way out."""
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + _haversine_km(a, b))
    total = cum[-1]
    if total <= 0:
        return [tuple(path[-1])]
    pts = []
    for k in range(1, n + 1):
        target = total * k / n
        i = min(range(len(cum)), key=lambda j: abs(cum[j] - target))
        pts.append(tuple(path[i]))
    return pts


def load(path) -> Club:
    """A Club from its `club.json` (+ the `club_data.json` built beside it)."""
    path = Path(path)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    data_path = path.parent / cfg.get("data", DATA_FILE)
    data = (json.loads(data_path.read_text(encoding="utf-8"))
            if data_path.exists() else {})
    cells = {(int(a), int(b)) for a, b in data.get("cells", [])}
    starts = cfg["starts"]
    center = (sum(s["lat"] for s in starts) / len(starts),
              sum(s["lng"] for s in starts) / len(starts))
    return Club(name=cfg.get("name", "Club"), starts=starts,
                exits=data.get("exits", []), roads=ClubRoads(cells),
                town_km=float(data.get("town_km", TOWN_KM_DEFAULT)),
                weight=float(cfg.get("club_road_weight", 0.5)), center=center,
                embed_on=list(cfg.get("embed_on", [])), website=cfg.get("website", ""),
                theme=cfg.get("theme", ""), route_colors=list(cfg.get("route_colors", [])))


def build(club_json, name_roads=True) -> dict:
    """Read the club's rides and write `club_data.json` (roads + exits) beside
    `club_json`. Returns the data. Exit flags you edited are kept by exit id."""
    club_json = Path(club_json)
    cfg = json.loads(club_json.read_text(encoding="utf-8"))
    routes_dir = club_json.parent / cfg.get("routes", "routes")
    rides = read_rides(routes_dir)
    starts = cfg["starts"]
    center = (sum(s["lat"] for s in starts) / len(starts),
              sum(s["lng"] for s in starts) / len(starts))
    exits = find_exits(rides, starts, center)
    data_path = club_json.parent / cfg.get("data", DATA_FILE)
    old = []
    if data_path.exists():
        old = json.loads(data_path.read_text(encoding="utf-8")).get("exits", [])
    # Keep what was set on each exit (id, name, enabled, home_only). Exits are matched
    # by where they end, not by id: ids come from the bearing from the middle of the
    # starts, which shifts when a start is added.
    fresh = []
    for ex in exits:
        prev = min(old, default=None,
                   key=lambda o: _haversine_km(tuple(o["end"]), tuple(ex["end"])))
        if prev is not None and _haversine_km(tuple(prev["end"]), tuple(ex["end"])) <= EXIT_MATCH_KM:
            for k in ("id", "name", "enabled", "home_only"):
                ex[k] = prev.get(k, ex[k])
        else:
            fresh.append(ex)
    if name_roads:
        name_exits(fresh)
    town_km = float(cfg.get("town_km", TOWN_KM_DEFAULT))
    data = {"built": time.strftime("%Y-%m-%d"), "rides": len(rides),
            "town_km": round(town_km, 1), "exits": exits,
            "cells": sorted([list(c) for c in road_cells(rides)])}
    data_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data

