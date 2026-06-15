"""Auto-detect the nearest 'good quiet riding' zone from a start point.

The roads cyclists like have a recognizable OSM signature: a rural grid of
low-traffic roads (unclassified / tertiary / track), open farmland, and few
arterials. When you start in a suburb the good country is often off in one
direction (open farmland a few miles out); on vacation you may not know where it is.

`find_ride_zone` fans out from the start in directional sectors, pulls the
relevant road classes + farmland from OSM/Overpass in ONE query, scores each
sector by that signature, and returns the best staging zone center — or None
when nothing stands out (you're already in good country, or it's city all
around and there's nowhere better to ride to).

The decision is *relative*: a sector has to beat the typical sector by a margin
to count as "worth riding to", so the logic self-calibrates instead of relying
on absolute thresholds that would differ between Illinois and Vermont.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import requests

from .engine import _bearing, _haversine_km, _destination
from .surface import OVERPASS_URL, USER_AGENT

# OSM highway classes. The quiet rural grid is the positive signal; arterials are
# the thing to ride away from. Residential is deliberately omitted — suburbs are
# wall-to-wall residential, so it's noise that doesn't separate good from bad.
GRID_HIGHWAYS = ("unclassified", "tertiary", "tertiary_link", "track", "road")
ARTERIAL_HIGHWAYS = ("motorway", "trunk", "primary", "secondary",
                     "motorway_link", "trunk_link", "primary_link", "secondary_link")

# Farmland is the dominant signal: open cornfield country is what we're hunting
# for. Quiet-road km is only a MINOR tiebreaker — counterintuitively, a dense road
# grid correlates with SUBURBIA (more total km), the opposite of what we want, so
# rewarding it heavily backfires. Arterials are a moderate penalty (ride away from
# expressways) but mustn't drown the farmland signal.
W_GRID = 0.15     # minor reward per km of quiet rural road (tiebreaker only)
W_FARM = 1.0      # reward per farmland polygon (open-country proxy) — dominant
W_ART = 0.4       # penalty per km of arterial
FARM_CENTROID_W = 1.0   # weight a farmland polygon gets when locating the zone center
GRID_CENTROID_W = 0.1   # per-km weight quiet roads get when locating the zone center

# Extra "good country" land signals other archetypes care about (grid-farmland
# ignores these — its weights below are 0, so its scoring stays byte-identical).
FOREST_USES = {"forest"}                 # landuse=forest (natural=wood handled too)
WATER_USES = {"reservoir", "basin", "water"}   # landuse=*; natural=water handled too


# --------------------------------------------------------------------------- #
# Archetype-keyed zone scoring (work-plan Task 2)
# --------------------------------------------------------------------------- #
# "Good quiet riding" means different land cover in different country: open
# farmland in the grid, forest in the hills, the shore on the coast. `ZoneWeights`
# captures the per-sector scoring signals; `ZONE_WEIGHTS_BY_ARCHETYPE` swaps them
# by terrain. The `grid-farmland` row is the original farmland-only tuning (forest
# and water weights 0), so its scoring AND its Overpass query are unchanged. Other
# rows are a first pass — calibrate later (work-plan Task 8).
@dataclass(frozen=True)
class ZoneWeights:
    w_grid: float = W_GRID
    w_farm: float = W_FARM
    w_art: float = W_ART
    w_forest: float = 0.0
    w_water: float = 0.0
    forest_cw: float = 0.0      # centroid weight for forest polygons
    water_cw: float = 0.0       # centroid weight for water polygons


_GRID_FARMLAND_ZONE = ZoneWeights()

ZONE_WEIGHTS_BY_ARCHETYPE = {
    "grid-farmland": _GRID_FARMLAND_ZONE,
    # Hills/forest: the woods (and a little water) are the draw, farmland less so.
    "forested-rolling": ZoneWeights(w_farm=0.4, w_forest=1.0, forest_cw=1.0,
                                    w_water=0.3, water_cw=0.4),
    "mountain": ZoneWeights(w_grid=0.1, w_farm=0.2, w_art=0.5, w_forest=0.8,
                            forest_cw=1.0, w_water=0.3, water_cw=0.5),
    # Coastal: ride toward the water; forest a secondary scenic plus.
    "coastal": ZoneWeights(w_farm=0.5, w_water=1.0, water_cw=1.0,
                           w_forest=0.3, forest_cw=0.3),
    # Suburban: the goal is to ESCAPE to open country — same farmland-seeking
    # signal as grid-farmland (this is the detector's original use case).
    "suburban-sprawl": _GRID_FARMLAND_ZONE,
    # Arid-open: open country but not cultivated; lean less on farmland.
    "arid-open": ZoneWeights(w_grid=0.2, w_farm=0.5, w_art=0.4),
    "unknown": _GRID_FARMLAND_ZONE,
}


def zone_weights_for(archetype) -> ZoneWeights:
    """ZoneWeights for an archetype (None / unmapped -> grid-farmland baseline)."""
    return ZONE_WEIGHTS_BY_ARCHETYPE.get(archetype or "grid-farmland",
                                         _GRID_FARMLAND_ZONE)


def _seg_len_km(pts):
    return sum(_haversine_km(a, b) for a, b in zip(pts, pts[1:]))


def _angle_diff(a, b):
    """Smallest absolute difference between two bearings, in degrees (0..180)."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def find_ride_zone(lat, lng, search_km=20.0, inner_km=5.0, sectors=12,
                   timeout=90, url=OVERPASS_URL, min_advantage=1.6,
                   prefer_bearing=None, archetype=None):
    """Find the best 'good riding' staging zone around (lat, lng).

    Returns a dict ``{lat, lng, bearing, distance_km, score, label, sectors}`` for
    the winning direction, or None if no sector clearly beats the rest (already in
    good country, or nothing good within range). `min_advantage` is how many times
    the median sector score the winner must reach to count as a standout.

    When `prefer_bearing` (degrees) is given, the caller has explicitly asked to ride
    that way: pick the best-scoring sector within ~45 deg of that heading and skip the
    "standout" and "already in good country" gates — honor the chosen direction even
    if it isn't the globally best one. Returns None only if the lookup itself fails.

    `archetype` (from `regions.classify_region`) selects what "good country" means
    via `ZoneWeights`: farmland in the grid, forest in the hills, water on the
    coast. None / 'grid-farmland' reproduces the original farmland-only scoring and
    Overpass query exactly.
    """
    zw = zone_weights_for(archetype)
    want_forest = zw.w_forest > 0 or zw.forest_cw > 0
    want_water = zw.w_water > 0 or zw.water_cw > 0

    width = 360.0 / sectors
    grid = [0.0] * sectors
    art = [0.0] * sectors
    farm = [0.0] * sectors
    forest = [0.0] * sectors
    water = [0.0] * sectors
    # weighted centroid accumulators (locate where the good stuff actually is)
    cx = [0.0] * sectors
    cy = [0.0] * sectors
    cw = [0.0] * sectors
    farm_in = forest_in = water_in = 0.0    # positive land signals in the inner ring

    elements = _query(lat, lng, search_km, timeout, url, want_forest, want_water)
    if elements is None:
        return None

    for el in elements:
        tags = el.get("tags", {})
        hw = tags.get("highway")
        lu = tags.get("landuse")
        nat = tags.get("natural")
        is_forest = lu in FOREST_USES or nat == "wood"
        is_water = lu in WATER_USES or nat == "water"
        if "geometry" in el:
            pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
            if len(pts) < 2:
                continue
            mid = pts[len(pts) // 2]
            length = _seg_len_km(pts)
        elif "center" in el:
            c = el["center"]
            mid = (c["lat"], c["lon"])
            length = 0.0
        else:
            continue

        dist = _haversine_km((lat, lng), mid)
        if dist < inner_km:
            if lu == "farmland":
                farm_in += 1.0
            elif is_forest:
                forest_in += 1.0
            elif is_water:
                water_in += 1.0
            continue
        if dist > search_km:
            continue
        s = int(_bearing((lat, lng), mid) / width) % sectors

        if hw in GRID_HIGHWAYS:
            grid[s] += length
            w = length * GRID_CENTROID_W
            cx[s] += mid[0] * w
            cy[s] += mid[1] * w
            cw[s] += w
        elif hw in ARTERIAL_HIGHWAYS:
            art[s] += length
        elif lu == "farmland":
            farm[s] += 1.0
            cx[s] += mid[0] * FARM_CENTROID_W
            cy[s] += mid[1] * FARM_CENTROID_W
            cw[s] += FARM_CENTROID_W
        elif is_forest:
            forest[s] += 1.0
            cx[s] += mid[0] * zw.forest_cw
            cy[s] += mid[1] * zw.forest_cw
            cw[s] += zw.forest_cw
        elif is_water:
            water[s] += 1.0
            cx[s] += mid[0] * zw.water_cw
            cy[s] += mid[1] * zw.water_cw
            cw[s] += zw.water_cw

    scores = [zw.w_grid * grid[i] + zw.w_farm * farm[i] + zw.w_forest * forest[i]
              + zw.w_water * water[i] - zw.w_art * art[i]
              for i in range(sectors)]
    # The positive "good land" signal per sector (used by the gates below), weighted
    # the same way. For grid-farmland this is just w_farm*farm, so the gate ratios are
    # identical to the original farmland-only logic.
    land = [zw.w_farm * farm[i] + zw.w_forest * forest[i] + zw.w_water * water[i]
            for i in range(sectors)]
    land_in = zw.w_farm * farm_in + zw.w_forest * forest_in + zw.w_water * water_in

    if prefer_bearing is not None:
        # Forced direction: choose the best sector within ~45 deg of the heading,
        # skipping the standout / already-good gates below.
        near = [i for i in range(sectors)
                if _angle_diff((i + 0.5) * width, prefer_bearing) <= 45.0]
        if not near:                          # window narrower than a sector — nearest one
            near = [min(range(sectors),
                        key=lambda i: _angle_diff((i + 0.5) * width, prefer_bearing))]
        best = max(near, key=lambda i: scores[i])
        best_score = scores[best]
    else:
        best = max(range(sectors), key=lambda i: scores[i])
        best_score = scores[best]

        ordered = sorted(scores)
        median = ordered[len(ordered) // 2]
        # Standout test: positive, and clearly above the typical sector. When the
        # median is non-positive (urban), any positive sector is a standout.
        bar = max(median * min_advantage, 0.0001) if median > 0 else 0.0001
        if best_score <= 0 or best_score < bar:
            return None

        # "Already in good country" gate: if the home inner ring is itself rich in
        # the archetype's good land (farmland in the grid, forest in the hills, …),
        # there's nothing to stage to — just ride a wind loop from where you are.
        # Compare DENSITY (weighted land per km^2) so the small inner disc and the
        # larger sector band are compared fairly. Home wins if its density is at
        # least ~70% of the best sector's. (For grid-farmland this is exactly the
        # original farmland-density comparison.)
        inner_area = math.pi * inner_km ** 2
        sector_area = (math.pi / sectors) * (search_km ** 2 - inner_km ** 2)
        home_density = land_in / inner_area if inner_area > 0 else 0.0
        best_density = land[best] / sector_area if sector_area > 0 else 0.0
        if home_density >= 0.7 * best_density:
            return None

    if cw[best] > 0:
        zlat, zlng = cx[best] / cw[best], cy[best] / cw[best]
    else:                                    # fall back to sector-center point
        brg = (best + 0.5) * width
        zlat, zlng = _destination(lat, lng, brg, (inner_km + search_km) / 2.0)

    return {
        "lat": zlat, "lng": zlng,
        "bearing": _bearing((lat, lng), (zlat, zlng)),
        "distance_km": _haversine_km((lat, lng), (zlat, zlng)),
        "score": best_score,
        "sectors": scores,
        "label": None,
    }


def _query(lat, lng, search_km, timeout, url, want_forest=False, want_water=False):
    """One Overpass call for grid roads (geom), arterials (geom), farmland (center).

    Forest and water polygons are only requested when the archetype's weights use
    them (`want_forest`/`want_water`), so grid-farmland's query stays byte-identical.
    """
    r = int(search_km * 1000)
    grid_re = "|".join(GRID_HIGHWAYS)
    art_re = "|".join(ARTERIAL_HIGHWAYS)
    around = f"(around:{r},{lat},{lng})"
    query = (
        f"[out:json][timeout:{timeout}];"
        f'way["highway"~"^({grid_re})$"]{around};out geom;'
        f'way["highway"~"^({art_re})$"]{around};out geom;'
        f'way["landuse"="farmland"]{around};out center;'
    )
    if want_forest:
        query += (f'way["landuse"="forest"]{around};out center;'
                  f'way["natural"="wood"]{around};out center;')
    if want_water:
        query += (f'way["natural"="water"]{around};out center;'
                  f'way["landuse"~"^(reservoir|basin)$"]{around};out center;')
    try:
        resp = requests.post(url, data={"data": query}, timeout=timeout + 15,
                             headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp.json().get("elements", [])
    except requests.RequestException:
        return None
