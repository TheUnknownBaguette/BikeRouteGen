"""Pluggable road-surface sources.

The ORS directions response already carries bucketed `surface` extras
(see `engine._surface_fractions`) — that's the zero-cost baseline. This module
adds an OpenStreetMap / Overpass source that reads the finer `surface`,
`tracktype`, and `smoothness` tags directly, which tend to cover the county and
township roads gravel riders actually care about better than ORS's buckets.

A source takes a route polyline ([(lat, lng), ...]) and returns
(paved_frac, unpaved_frac), or None when it has no usable data for that route —
so a front-end can fall back to the ORS baseline.
"""
from __future__ import annotations

import math

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "windroute/0.1 (personal cycling tool)"

# OSM surface values -> paved / unpaved. Unknown or untagged is treated as
# paved, matching the ORS path's default.
UNPAVED_SURFACES = {
    "unpaved", "gravel", "fine_gravel", "compacted", "dirt", "earth", "ground",
    "grass", "grass_paver", "sand", "mud", "pebblestone", "rock", "woodchips",
    "clay", "salt", "snow", "ice",
}
PAVED_SURFACES = {
    "paved", "asphalt", "concrete", "concrete:plates", "concrete:lanes",
    "paving_stones", "sett", "cobblestone", "unhewn_cobblestone", "metal",
    "wood", "chipseal", "bricks", "brick",
}


# OSM cycleway=* values that mean "there's an on-road bike lane here". These are
# tagged on the ROAD way itself (not a separate geometry), so ORS waytype can't
# see them — only a direct OSM read can. highway=cycleway (a separate path) is
# deliberately excluded: that's counted as a path via ORS waytype, not a lane.
BIKELANE_CYCLEWAY_VALUES = {
    "lane", "track", "opposite_lane", "opposite_track", "shared_lane",
    "buffered_lane", "share_busway", "opposite_share_busway", "shoulder",
    "crossing", "yes",
}
# highway classes where bicycle=designated counts as a lane-grade signal (i.e.
# real roads, not footways/paths/cycleways which are handled elsewhere).
_ROAD_HIGHWAYS = {
    "primary", "secondary", "tertiary", "unclassified", "residential",
    "primary_link", "secondary_link", "tertiary_link", "living_street",
    "service", "road",
}

# Waytype classes, the OSM analog of the ORS waytype codes used in scoring.
# ORS doesn't run on an imported GPS track, so busy_frac / path_frac for trips
# come from these instead. BUSY = the arterial/US-highway class ORS calls "State
# Road" (waytype 1); in OSM those are trunk/primary (+ motorway, which bikes
# can't use but is harmless to list). PATH = separated multiuse trails
# (ORS waytypes 4/6/7): highway=path/footway/cycleway as their own geometry.
BUSY_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link",
}
PATH_HIGHWAYS = {"path", "footway", "cycleway"}


def _waytype_kind(tags: dict) -> str | None:
    """'busy' / 'path' / None from a way's highway tag (see BUSY/PATH_HIGHWAYS)."""
    hw = (tags.get("highway") or "").lower()
    if hw in BUSY_HIGHWAYS:
        return "busy"
    if hw in PATH_HIGHWAYS:
        return "path"
    return None


def _is_bikelane(tags: dict) -> bool:
    """True if a road carries an on-road bike lane/track (cycleway=* on the road).

    Excludes highway=cycleway (a separate path, counted via ORS waytype).
    """
    if (tags.get("highway") or "").lower() == "cycleway":
        return False
    for key in ("cycleway", "cycleway:both", "cycleway:left", "cycleway:right"):
        if (tags.get(key) or "").lower() in BIKELANE_CYCLEWAY_VALUES:
            return True
    if ((tags.get("bicycle") or "").lower() == "designated"
            and (tags.get("highway") or "").lower() in _ROAD_HIGHWAYS):
        return True
    return False


def classify_tags(tags: dict) -> str | None:
    """'paved' / 'unpaved' / None(unknown) from a way's OSM tags."""
    surf = (tags.get("surface") or "").lower()
    if surf in UNPAVED_SURFACES:
        return "unpaved"
    if surf in PAVED_SURFACES:
        return "paved"

    tt = (tags.get("tracktype") or "").lower()
    if tt:
        # grade1 is solid/compacted (often paved-like); grade2-5 get looser.
        return "paved" if tt == "grade1" else "unpaved"

    sm = (tags.get("smoothness") or "").lower()
    if sm:
        return "paved" if sm in {"excellent", "good", "intermediate"} else "unpaved"

    return None


# --------------------------------------------------------------------------- #
# Geometry helpers (no extra deps — local equirectangular approximation)
# --------------------------------------------------------------------------- #
def _haversine_km(p1, p2) -> float:
    R = 6371.0
    lat1, lon1 = map(math.radians, p1)
    lat2, lon2 = map(math.radians, p2)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _pt_seg_dist_m(p, a, b) -> float:
    """Distance in metres from point p to segment a-b, all (lat, lng)."""
    lat0 = math.radians(p[0])
    mlat = 111_320.0
    mlng = 111_320.0 * math.cos(lat0)
    ax, ay = (a[1] - p[1]) * mlng, (a[0] - p[0]) * mlat
    bx, by = (b[1] - p[1]) * mlng, (b[0] - p[0]) * mlat
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 0:
        return math.hypot(ax, ay)
    t = -(ax * dx + ay * dy) / seg2
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return math.hypot(ax + t * dx, ay + t * dy)


def _bbox(coords, pad_deg=0.01):
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    return min(lats) - pad_deg, min(lngs) - pad_deg, max(lats) + pad_deg, max(lngs) + pad_deg


# --------------------------------------------------------------------------- #
# Overpass source
# --------------------------------------------------------------------------- #
class OverpassSurface:
    """Classify route surface from OSM tags fetched via Overpass.

    Usage:
        src = OverpassSurface().build([c.coords for c in candidates])  # one query
        paved, unpaved = src.classify(candidate.coords) or (None, None)
    """
    name = "osm-overpass"

    def __init__(self, match_threshold_m=30.0, cell_deg=0.003,
                 timeout=90, url=OVERPASS_URL):
        self.match_threshold_m = match_threshold_m
        self.cell_deg = cell_deg
        self.timeout = timeout
        self.url = url
        self._grid: dict | None = None      # cell -> [(cls, a, b), ...]  (surface)
        self._bike_grid: dict | None = None  # cell -> [(True, a, b), ...] (bike lanes)
        self._way_grid: dict | None = None   # cell -> [(kind, a, b), ...] (busy/path)
        self.way_count = 0
        self.bikelane_count = 0
        self.waytype_count = 0

    # -- index construction -------------------------------------------------- #
    def build(self, coords_lists):
        """Fetch surface-tagged ways for the union bbox of several routes once."""
        all_coords = [pt for coords in coords_lists for pt in coords]
        if not all_coords:
            return self
        s, w, n, e = _bbox(all_coords)
        bb = f"({s},{w},{n},{e})"
        busy_re = "|".join(sorted(BUSY_HIGHWAYS))
        path_re = "|".join(sorted(PATH_HIGHWAYS))
        query = (
            f"[out:json][timeout:{self.timeout}];"
            f'(way["highway"]["surface"]{bb};'
            f'way["highway"]["tracktype"]{bb};'
            f'way["highway"]["smoothness"]{bb};'
            f'way["highway"]["cycleway"]{bb};'
            f'way["highway"]["cycleway:left"]{bb};'
            f'way["highway"]["cycleway:right"]{bb};'
            f'way["highway"]["cycleway:both"]{bb};'
            f'way["highway"]["bicycle"="designated"]{bb};'
            f'way["highway"~"^({busy_re})$"]{bb};'
            f'way["highway"~"^({path_re})$"]{bb};);'
            f"out tags geom;"
        )
        resp = requests.post(
            self.url, data={"data": query}, timeout=self.timeout + 15,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])

        grid: dict = {}
        bike_grid: dict = {}
        way_grid: dict = {}
        count = bike_count = waytype_count = 0
        for el in elements:
            tags = el.get("tags", {})
            cls = classify_tags(tags)
            is_lane = _is_bikelane(tags)
            kind = _waytype_kind(tags)
            if cls is None and not is_lane and kind is None:
                continue
            geom = el.get("geometry") or []
            if len(geom) < 2:
                continue
            pts = [(g["lat"], g["lon"]) for g in geom]
            for a, b in zip(pts, pts[1:]):
                if cls is not None:
                    self._add_segment(grid, (cls, a, b))
                if is_lane:
                    self._add_segment(bike_grid, (True, a, b))
                if kind is not None:
                    self._add_segment(way_grid, (kind, a, b))
            if cls is not None:
                count += 1
            if is_lane:
                bike_count += 1
            if kind is not None:
                waytype_count += 1

        self._grid = grid
        self._bike_grid = bike_grid
        self._way_grid = way_grid
        self.way_count = count
        self.bikelane_count = bike_count
        self.waytype_count = waytype_count
        return self

    def _cell(self, lat, lng):
        return int(lat / self.cell_deg), int(lng / self.cell_deg)

    def _add_segment(self, grid, seg):
        """Register a segment in every grid cell it passes through."""
        _, a, b = seg
        steps = int(max(abs(a[0] - b[0]), abs(a[1] - b[1])) / self.cell_deg) + 1
        seen = set()
        for k in range(steps + 1):
            t = k / steps
            cell = self._cell(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            if cell not in seen:
                seen.add(cell)
                grid.setdefault(cell, []).append(seg)

    # -- classification ------------------------------------------------------ #
    def classify(self, coords):
        """Return (paved_frac, unpaved_frac) or None if no index / no length."""
        if not self._grid:
            return None
        paved = unpaved = 0.0
        for a, b in zip(coords, coords[1:]):
            d = _haversine_km(a, b)
            if d <= 0:
                continue
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            if self._nearest_class(mid) == "unpaved":
                unpaved += d
            else:
                paved += d                      # paved OR unknown -> paved default
        total = paved + unpaved
        if total <= 0:
            return None
        return paved / total, unpaved / total

    def classify_bikelane(self, coords):
        """Fraction of the route running along a road with an on-road bike lane.

        Returns None if no bike-lane index was built. Each route segment whose
        midpoint sits within `match_threshold_m` of a lane-tagged road counts.
        """
        if not self._bike_grid:
            return None
        total = lane = 0.0
        for a, b in zip(coords, coords[1:]):
            d = _haversine_km(a, b)
            if d <= 0:
                continue
            total += d
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            if self._nearest_in(self._bike_grid, mid):
                lane += d
        if total <= 0:
            return None
        return lane / total

    def classify_waytype(self, coords):
        """Return (busy_frac, path_frac) or None if no waytype index was built.

        The OSM analog of ORS busy_frac/path_frac for an imported track: each
        route segment whose midpoint nearest-matches a busy arterial / a
        separated path counts toward that fraction. A segment matching neither
        (the quiet back roads we want) counts toward neither.
        """
        if not self._way_grid:
            return None
        total = busy = path = 0.0
        for a, b in zip(coords, coords[1:]):
            d = _haversine_km(a, b)
            if d <= 0:
                continue
            total += d
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            kind = self._nearest_kind(mid)
            if kind == "busy":
                busy += d
            elif kind == "path":
                path += d
        if total <= 0:
            return None
        return busy / total, path / total

    def path_run_frac(self, coords):
        """Longest *contiguous* path run as a fraction of the route, or None.

        The connector-vs-destination signal (the OSM analog of
        engine._waytype_run_km): a route segment counts as path when its midpoint
        nearest-matches a separated path. Short stretches between roads stay small;
        one long unbroken path run (e.g. an out-and-back on a trail) shows up big."""
        if not self._way_grid:
            return None
        total = best = cur = 0.0
        for a, b in zip(coords, coords[1:]):
            d = _haversine_km(a, b)
            if d <= 0:
                continue
            total += d
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            if self._nearest_kind(mid) == "path":
                cur += d
                best = max(best, cur)
            else:
                cur = 0.0
        if total <= 0:
            return None
        return best / total

    def _nearest_kind(self, p):
        ci, cj = self._cell(*p)
        best_d, best_kind = self.match_threshold_m, None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for kind, a, b in self._way_grid.get((ci + di, cj + dj), ()):
                    dist = _pt_seg_dist_m(p, a, b)
                    if dist < best_d:
                        best_d, best_kind = dist, kind
        return best_kind

    def _nearest_class(self, p):
        ci, cj = self._cell(*p)
        best_d, best_cls = self.match_threshold_m, None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for cls, a, b in self._grid.get((ci + di, cj + dj), ()):
                    dist = _pt_seg_dist_m(p, a, b)
                    if dist < best_d:
                        best_d, best_cls = dist, cls
        return best_cls

    def _nearest_in(self, grid, p):
        """True if any segment in `grid` is within match_threshold_m of point p."""
        ci, cj = self._cell(*p)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for _, a, b in grid.get((ci + di, cj + dj), ()):
                    if _pt_seg_dist_m(p, a, b) < self.match_threshold_m:
                        return True
        return False
