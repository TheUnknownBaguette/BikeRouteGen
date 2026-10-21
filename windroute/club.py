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
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .cues import UNNAMED
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
# The route library: the club's own routes, offered as ready-made candidates
# --------------------------------------------------------------------------- #
LIBRARY_START_KM = 0.5     # a route belongs to the start it begins this close to
LIBRARY_DIST_FRAC = 0.25   # offered when within this share of the asked distance
LIBRARY_THIN_M = 5.0       # route lines are simplified to within this many metres
LIBRARY_LOOP_KM = 0.3      # start and finish this close -> a loop, ridable both ways


def read_course(path) -> dict:
    """A route file's name, points, elevations and cue sheet
    ({"name", "coords", "eles", "cues": [(lat, lng, notes)]}). GPX has no cues."""
    root = ET.parse(path).getroot()
    tag = lambda el: el.tag.rsplit("}", 1)[-1]
    # The file name, not the name inside: Ride with GPS cuts a TCX course name to 15
    # characters and swaps punctuation for dashes ("St--Joe-").
    name, coords, eles, cues = nice_name(Path(path).stem), [], [], []
    if tag(root) == "TrainingCenterDatabase":
        for el in root.iter():
            t = tag(el)
            if t == "Trackpoint":
                vals = {tag(x): x.text for x in el.iter()}
                if "LatitudeDegrees" in vals:
                    coords.append((float(vals["LatitudeDegrees"]), float(vals["LongitudeDegrees"])))
                    eles.append(float(vals.get("AltitudeMeters") or 0.0))
            elif t == "CoursePoint":
                vals = {tag(x): x.text for x in el.iter()}
                if "LatitudeDegrees" in vals:
                    cues.append((float(vals["LatitudeDegrees"]),
                                 float(vals["LongitudeDegrees"]), vals.get("Notes") or ""))
    else:
        for el in root.iter():
            if tag(el) in ("trkpt", "rtept"):
                coords.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
                ele = next((x.text for x in el if tag(x) == "ele"), None)
                eles.append(float(ele) if ele else 0.0)
    return {"name": name, "coords": coords, "eles": eles, "cues": cues}


def nice_name(stem):
    """A route's file name as a title: "Buck_s_Pond (1)" -> "Buck's Pond",
    "Around_the_Crick-_IC_Fast_Ride" -> "Around the Crick – IC Fast Ride"."""
    s = re.sub(r"\s*\(\d+\)$", "", stem)                # download copies: "(1)"
    s = re.sub(r"_s_", "'s ", s).replace("_", " ")
    s = re.sub(r"\s*-\s+", " – ", s)
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s


_ONTO = (" onto ", " on ")


def _cue_road(notes):
    """The road a cue turns onto ("Turn left onto S First St" -> "S First St"), or
    None when it names none ("Turn right", "Turn left toward Savoy")."""
    for word in _ONTO:
        if word in notes:
            return notes.split(word, 1)[1].strip() or None
    return None


def _point_names(coords, cues):
    """Road name for the stretch leaving each point, from the cue sheet. A turn that
    names no road starts its own unnamed stretch, so it still gets a cue later."""
    names = [UNNAMED + "0"] * len(coords)
    j = 0
    for k, (lat, lng, notes) in enumerate(cues):
        if notes.startswith(("Start of route", "End of route")) or not notes.strip():
            continue
        window = range(j, min(len(coords), j + 4000))
        if not window:
            break
        i = min(window, key=lambda n: _haversine_km(coords[n], (lat, lng)))
        if _haversine_km(coords[i], (lat, lng)) > 0.1:
            continue                              # not on this line: skip it
        road = _cue_road(notes) or f"{UNNAMED}{k + 1}"
        for n in range(i, len(coords)):
            names[n] = road
        j = i
    return names


def _thin_keep(coords, tol_m=LIBRARY_THIN_M):
    """Indexes kept when simplifying a line to within `tol_m` (Douglas-Peucker)."""
    n = len(coords)
    if n < 3:
        return list(range(n))
    lat0 = math.radians(coords[0][0])
    xy = [(p[1] * 111320.0 * math.cos(lat0), p[0] * 110540.0) for p in coords]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        dx, dy = xy[b][0] - ax, xy[b][1] - ay
        seg = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i][0] - ax, xy[i][1] - ay
            t = 0.0 if seg == 0 else max(0.0, min(1.0, (px * dx + py * dy) / seg))
            d = (px - t * dx) ** 2 + (py - t * dy) ** 2
            if d > best:
                best, bi = d, i
        if bi > 0 and best > tol_m * tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [i for i in range(n) if keep[i]]


def _expand_names(changes, n):
    """[[index, name], ...] change points -> one name per point."""
    names, cur, k = [], "", 0
    for i in range(n):
        while k < len(changes) and changes[k][0] <= i:
            cur = changes[k][1]
            k += 1
        names.append(cur)
    return names


def library_routes(routes_dir, starts) -> list:
    """The club's route files, simplified, each tagged with the start it begins at
    (routes that start elsewhere are left out). Duplicates are dropped."""
    out, seen = [], set()
    for p in sorted(Path(routes_dir).iterdir()):
        if p.suffix.lower() not in (".tcx", ".gpx"):
            continue
        try:
            r = read_course(p)
        except (ET.ParseError, ValueError, KeyError):
            continue
        co = r["coords"]
        if len(co) < 2:
            continue
        key = (len(co), co[0], co[-1])
        if key in seen:
            continue
        seen.add(key)
        st = min(starts, key=lambda s: _haversine_km((s["lat"], s["lng"]), co[0]))
        if _haversine_km((st["lat"], st["lng"]), co[0]) > LIBRARY_START_KM:
            continue
        names = _point_names(co, r["cues"])
        keep = _thin_keep(co)
        kco = [co[i] for i in keep]
        knames = [names[i] for i in keep]
        changes = [[i, nm] for i, nm in enumerate(knames) if i == 0 or nm != knames[i - 1]]
        has_ele = any(e for e in r["eles"])
        out.append({
            "name": r["name"], "file": p.name, "start": st["id"],
            "km": round(sum(_haversine_km(a, b) for a, b in zip(co, co[1:])), 2),
            "loop": _haversine_km(co[0], co[-1]) <= LIBRARY_LOOP_KM,
            "coords": [[round(a, 5), round(b, 5)] for a, b in kco],
            "eles": [round(r["eles"][i]) for i in keep] if has_ele else [],
            "names": changes,
        })
    return out


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
    candidate_color: str = ""              # every other candidate on the map
    library: list = field(default_factory=list)   # the club's own routes (see library_routes)

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

    def library_candidates(self, start_id, target_km, tolerance_km=0.0) -> list:
        """The club's own routes from this start that suit the distance, as
        Candidates ("club route"); loops are offered both ways round."""
        from .models import Candidate
        from .routing import _reversed_names, _smoothed_ascent
        slack = max(tolerance_km, LIBRARY_DIST_FRAC * target_km)
        out = []
        for r in self.library:
            if r["start"] != start_id or abs(r["km"] - target_km) > slack:
                continue
            coords = [tuple(p) for p in r["coords"]]
            eles = r.get("eles") or None
            names = _expand_names(r.get("names", []), len(coords))
            ways = [(coords, eles, names, r["name"])]
            if r.get("loop"):
                ways.append((coords[::-1], eles[::-1] if eles else None,
                             _reversed_names(names), f"{r['name']} (reversed)"))
            for co, el, nm, label in ways:
                out.append(Candidate(
                    coords=co, distance_km=r["km"],
                    ascent_m=_smoothed_ascent(el) if el else 0.0,
                    paved_frac=1.0, unpaved_frac=0.0, shape="club route",
                    eles=el, road_names=nm, label=label))
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
                theme=cfg.get("theme", ""), route_colors=list(cfg.get("route_colors", [])),
                candidate_color=cfg.get("candidate_color", ""),
                library=data.get("library", []))


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
    library = library_routes(routes_dir, starts)
    data = {"built": time.strftime("%Y-%m-%d"), "rides": len(rides),
            "town_km": round(town_km, 1), "exits": exits, "library": library,
            "cells": sorted([list(c) for c in road_cells(rides)])}
    data_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data

