"""Region archetype classifier — Task 1 of the route-algorithm work plan.

The current scoring weights encode ONE place (flat Illinois grid-farmland, tuned
against 108 real rides). Before any of that can generalize, the planner needs to
know what kind of country it's looking at. `classify_region` samples the area
around a start point and labels it with a terrain *archetype* so the rest of the
pipeline (zone detection, scoring weights, shape choice — Task 2 onward) can adapt.

Design:
  - `classify_region(center, radius_km)` does the I/O: ONE Overpass read for the
    road network + land-use polygons, plus one keyless Open-Meteo *elevation* read
    for coarse relief. It builds a raw feature vector and calls the pure classifier.
    Results are cached per coarse region cell so a session doesn't re-fetch.
  - `classify_archetype(features)` is PURE — feature vector in, (archetype,
    confidence) out — so the decision logic is unit-testable offline with no network.

Relief note: the work plan suggested reusing `engine._smoothed_ascent`, but that
measures ascent *along an ordered route*. For region-level relief the right signal
is elevation **range / spread over a sampled grid**, which is what we compute here.

The thresholds below are a deliberately simple first cut — the archetype set and
its cutoffs are meant to be calibrated against real rides later (work-plan Task 8).
Every cutoff is a named constant so that tuning is a one-line edit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import requests

from .engine import _haversine_km, _destination
from .surface import OVERPASS_URL, USER_AGENT

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"   # Open-Meteo, keyless

# Archetype labels. `unknown` is the honest fallback when OSM coverage is too thin
# to decide — callers treat it as grid-farmland weights but flag low confidence.
ARCHETYPES = (
    "grid-farmland", "forested-rolling", "mountain", "suburban-sprawl",
    "coastal", "arid-open", "unknown",
)

# --- OSM tag groups -------------------------------------------------------- #
# Real roads we count for density + class mix. Footways/paths/cycleways/service
# are excluded: they bloat the payload and aren't the road-network signal.
GRID_HIGHWAYS = ("unclassified", "tertiary", "tertiary_link", "track", "road",
                 "residential", "living_street")
ARTERIAL_HIGHWAYS = ("motorway", "trunk", "primary", "secondary",
                     "motorway_link", "trunk_link", "primary_link", "secondary_link")
_ALL_HIGHWAYS = GRID_HIGHWAYS + ARTERIAL_HIGHWAYS

# Land-use / natural polygon classes, bucketed into the four area fractions the
# classifier keys off. Everything else (industrial, commercial, …) is ignored.
FARMLAND_USES = {"farmland", "farmyard", "meadow", "orchard", "vineyard",
                 "allotments"}
FOREST_USES = {"forest", "wood"}                       # landuse=forest / natural=wood
RESIDENTIAL_USES = {"residential", "retail", "commercial", "industrial"}
WATER_USES = {"water", "reservoir", "basin", "wetland"}  # landuse=* / natural=water

# --- Classifier thresholds (calibratable) ---------------------------------- #
DEFAULT_RADIUS_KM = 10.0
MIN_ELEMENTS = 25              # fewer OSM elements than this -> too thin -> unknown
MOUNTAIN_RELIEF_RANGE_M = 500.0   # elevation max-min over the sample grid
MOUNTAIN_RELIEF_STD_M = 120.0     # elevation spread over the sample grid
ROLLING_RELIEF_STD_M = 35.0       # "rolling" (vs dead flat) for forested terrain
COASTLINE_MIN_KM = 1.0        # this much mapped coastline -> coastal
SUBURB_RESIDENTIAL_FRAC = 0.30
SUBURB_ROAD_DENSITY = 4.0     # km of road per km^2 (suburbs are dense road grids)
FARMLAND_FRAC_MIN = 0.20
FOREST_FRAC_MIN = 0.30
OPEN_ROAD_DENSITY_MAX = 1.5   # arid-open: very sparse road network
OPEN_RESIDENTIAL_MAX = 0.05


@dataclass
class RegionProfile:
    """A start point's terrain archetype plus the raw features behind the call.

    `features` is kept inspectable/loggable on purpose (work-plan acceptance:
    "feature vector logged") so calibration can see *why* a place was labelled.
    `confidence` is ~0..1; low values mean thin data or a borderline call and tell
    downstream tuning to lean on the safe grid-farmland defaults.
    """
    archetype: str
    confidence: float
    features: dict
    center: tuple = (0.0, 0.0)
    radius_km: float = DEFAULT_RADIUS_KM
    note: str = ""                              # human one-liner for the CLI/web

    @property
    def low_confidence(self) -> bool:
        return self.confidence < 0.4


# Per-cell cache so repeated plans in the same area don't re-hit Overpass within a
# session. Key rounds the center to ~0.1 deg (~11 km) — the classifier is a coarse,
# area-level read, so neighbouring starts share an answer.
_CACHE: dict = {}


def _cell_key(lat, lng, radius_km):
    return (round(lat, 1), round(lng, 1), round(radius_km))


def classify_region(center, radius_km=DEFAULT_RADIUS_KM, timeout=90,
                    url=OVERPASS_URL, use_cache=True) -> RegionProfile:
    """Classify the country around `center` ((lat, lng)) into a terrain archetype.

    One Overpass read (roads + land-use polygons) + one Open-Meteo elevation read
    (coarse relief). Returns a `RegionProfile`. Never raises on a network failure:
    a failed fetch yields an `unknown` profile with low confidence so a front-end
    can degrade gracefully rather than abort the whole plan.
    """
    lat, lng = center
    key = _cell_key(lat, lng, radius_km)
    if use_cache and key in _CACHE:
        return _CACHE[key]

    elements = _query_overpass(lat, lng, radius_km, timeout, url)
    if elements is None:                       # fetch failed entirely
        prof = RegionProfile("unknown", 0.15, {"error": "overpass-failed"},
                             (lat, lng), radius_km,
                             note="region: OSM lookup failed; treating as unknown.")
        if use_cache:
            _CACHE[key] = prof
        return prof

    features = _extract_features(elements, lat, lng, radius_km)
    relief = _sample_relief(lat, lng, radius_km, timeout)
    features.update(relief)                     # relief_range_m / relief_std_m (or None)

    archetype, confidence = classify_archetype(features)
    prof = RegionProfile(archetype, confidence, features, (lat, lng), radius_km,
                         note=_describe(archetype, confidence, features))
    if use_cache:
        _CACHE[key] = prof
    return prof


# --------------------------------------------------------------------------- #
# Pure classification (no I/O — unit-testable)
# --------------------------------------------------------------------------- #
def classify_archetype(f: dict):
    """Map a feature vector to (archetype, confidence). Pure.

    A prioritized decision list: the most distinctive signals (relief, coastline)
    are tested first, then land cover, with `arid-open` and `unknown` as the
    open-country / no-data fallbacks. `confidence` rises the further the deciding
    feature sits past its threshold.
    """
    n = f.get("n_elements", 0)
    if n < MIN_ELEMENTS:
        return "unknown", 0.2

    farm = f.get("farmland_frac", 0.0)
    forest = f.get("forest_frac", 0.0)
    resid = f.get("residential_frac", 0.0)
    road_density = f.get("road_density", 0.0)
    coast = f.get("coastline_km", 0.0)
    relief_range = f.get("relief_range_m")
    relief_std = f.get("relief_std_m")

    # 1) Mountain — decided by relief, when we have it.
    if relief_std is not None and (relief_range >= MOUNTAIN_RELIEF_RANGE_M
                                   or relief_std >= MOUNTAIN_RELIEF_STD_M):
        conf = _conf(relief_std / MOUNTAIN_RELIEF_STD_M)
        return "mountain", conf

    # 2) Coastal — actual mapped ocean coastline in range (not just inland water).
    if coast >= COASTLINE_MIN_KM:
        return "coastal", _conf(coast / COASTLINE_MIN_KM)

    # 3) Suburban sprawl — wall-to-wall residential on a dense road grid.
    if resid >= SUBURB_RESIDENTIAL_FRAC and road_density >= SUBURB_ROAD_DENSITY:
        return "suburban-sprawl", _conf(resid / SUBURB_RESIDENTIAL_FRAC)

    # 4) Grid-farmland — open cultivated country, low relief (the home region).
    flat = relief_std is None or relief_std < MOUNTAIN_RELIEF_STD_M
    if farm >= FARMLAND_FRAC_MIN and flat:
        return "grid-farmland", _conf(farm / FARMLAND_FRAC_MIN)

    # 5) Forested-rolling — forest cover, usually with some relief.
    if forest >= FOREST_FRAC_MIN:
        boost = 0.05 if (relief_std or 0.0) >= ROLLING_RELIEF_STD_M else 0.0
        return "forested-rolling", min(0.95, _conf(forest / FOREST_FRAC_MIN) + boost)

    # 6) Arid-open — sparse roads, little farmland/forest/residential (range, desert).
    if (road_density <= OPEN_ROAD_DENSITY_MAX and farm < FARMLAND_FRAC_MIN
            and forest < FOREST_FRAC_MIN and resid < OPEN_RESIDENTIAL_MAX):
        return "arid-open", 0.55

    # 7) Nothing decisive — fall back to grid-farmland weights, flagged low-confidence.
    return "unknown", 0.35


def _conf(ratio: float) -> float:
    """Map a (feature / threshold) ratio >= 1 to a confidence in ~[0.55, 0.95]."""
    return max(0.55, min(0.95, 0.45 + 0.25 * ratio))


def _describe(archetype: str, confidence: float, f: dict) -> str:
    """A compact human one-liner summarizing the call and the headline features."""
    bits = [f"region: {archetype} (confidence {confidence:.0%})"]
    parts = []
    if f.get("farmland_frac"):
        parts.append(f"{f['farmland_frac']*100:.0f}% farmland")
    if f.get("forest_frac"):
        parts.append(f"{f['forest_frac']*100:.0f}% forest")
    if f.get("residential_frac"):
        parts.append(f"{f['residential_frac']*100:.0f}% built-up")
    if f.get("road_density"):
        parts.append(f"{f['road_density']:.1f} km/km² roads")
    if f.get("relief_std_m") is not None:
        parts.append(f"relief ±{f['relief_std_m']:.0f} m")
    if f.get("coastline_km"):
        parts.append(f"{f['coastline_km']:.1f} km coast")
    if parts:
        bits.append("; ".join(parts))
    return " — ".join(bits)


# --------------------------------------------------------------------------- #
# Feature extraction (Overpass) + relief (Open-Meteo elevation)
# --------------------------------------------------------------------------- #
def _query_overpass(lat, lng, radius_km, timeout, url):
    """One Overpass call: real roads + land-use/natural polygons + coastline.

    Returns the element list, or None on any failure (so the caller degrades to an
    `unknown` profile instead of crashing the plan).
    """
    r = int(radius_km * 1000)
    hw_re = "|".join(_ALL_HIGHWAYS)
    around = f"(around:{r},{lat},{lng})"
    query = (
        f"[out:json][timeout:{timeout}];"
        f"("
        f'way["highway"~"^({hw_re})$"]{around};'
        f'way["landuse"]{around};'
        f'way["natural"]{around};'
        f");"
        f"out geom;"
    )
    try:
        resp = requests.post(url, data={"data": query}, timeout=timeout + 15,
                             headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp.json().get("elements", [])
    except (requests.RequestException, ValueError):
        return None


def _extract_features(elements, lat, lng, radius_km) -> dict:
    """Turn raw Overpass elements into the classifier's feature vector.

    Roads contribute length (km) by class; land-use/natural polygons contribute
    AREA (km^2, shoelace) by bucket; coastline ways contribute length. Area
    fractions are over the disc area (pi*r^2). Fractions are clamped to <=1 because
    overlapping/edge-spanning polygons can otherwise sum past the disc area.
    """
    region_area = math.pi * radius_km ** 2
    grid_km = arterial_km = 0.0
    farm_a = forest_a = resid_a = water_a = 0.0
    coastline_km = 0.0
    n_rel = 0

    for el in elements:
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue
        pts = [(g["lat"], g["lon"]) for g in geom]
        tags = el.get("tags", {})
        hw = tags.get("highway")
        if hw in ARTERIAL_HIGHWAYS:
            arterial_km += _line_km(pts)
            n_rel += 1
            continue
        if hw in GRID_HIGHWAYS:
            grid_km += _line_km(pts)
            n_rel += 1
            continue
        if tags.get("natural") == "coastline":
            coastline_km += _line_km(pts)
            n_rel += 1
            continue

        lu = tags.get("landuse")
        nat = tags.get("natural")
        bucket = _landuse_bucket(lu, nat)
        if bucket is None:
            continue
        area = _poly_area_km2(pts, lat)
        if area <= 0:
            continue
        n_rel += 1
        if bucket == "farmland":
            farm_a += area
        elif bucket == "forest":
            forest_a += area
        elif bucket == "residential":
            resid_a += area
        elif bucket == "water":
            water_a += area

    total_road_km = grid_km + arterial_km

    def frac(a):
        return max(0.0, min(1.0, a / region_area)) if region_area > 0 else 0.0

    return {
        "n_elements": n_rel,
        "area_km2": round(region_area, 2),
        "farmland_frac": round(frac(farm_a), 4),
        "forest_frac": round(frac(forest_a), 4),
        "residential_frac": round(frac(resid_a), 4),
        "water_frac": round(frac(water_a), 4),
        "road_km": round(total_road_km, 2),
        "road_density": round(total_road_km / region_area, 4) if region_area else 0.0,
        "arterial_frac": round(arterial_km / total_road_km, 4) if total_road_km else 0.0,
        "grid_frac": round(grid_km / total_road_km, 4) if total_road_km else 0.0,
        "coastline_km": round(coastline_km, 3),
    }


def _landuse_bucket(lu, nat):
    """Map a landuse/natural tag value to one of our four area buckets, or None."""
    if lu in FARMLAND_USES:
        return "farmland"
    if lu in FOREST_USES or nat in FOREST_USES:
        return "forest"
    if lu in RESIDENTIAL_USES:
        return "residential"
    if lu in WATER_USES or nat in WATER_USES:
        return "water"
    return None


def _line_km(pts):
    return sum(_haversine_km(a, b) for a, b in zip(pts, pts[1:]))


def _poly_area_km2(pts, lat0):
    """Shoelace area (km^2) of a closed lat/lng ring via local equirectangular m.

    Open ways (first != last point) are treated as closed for a coarse area; the
    error is small for the near-closed land-use polygons OSM emits. Returns 0 for
    degenerate rings.
    """
    if len(pts) < 3:
        return 0.0
    m_per_deg_lat = 111320.0
    m_per_deg_lng = 111320.0 * math.cos(math.radians(lat0))
    xs = [lng * m_per_deg_lng for _lat, lng in pts]
    ys = [lat * m_per_deg_lat for lat, _lng in pts]
    area2 = 0.0
    for i in range(len(pts) - 1):
        area2 += xs[i] * ys[i + 1] - xs[i + 1] * ys[i]
    # close the ring if the source way wasn't explicitly closed
    area2 += xs[-1] * ys[0] - xs[0] * ys[-1]
    return abs(area2) / 2.0 / 1.0e6


def _sample_relief(lat, lng, radius_km, timeout):
    """Coarse relief from a grid of elevations (Open-Meteo, keyless, one call).

    Samples a square grid within the disc and returns the elevation range + std.
    Returns {relief_range_m: None, relief_std_m: None} on any failure so the
    classifier simply can't pick 'mountain' rather than crashing.
    """
    pts = _relief_grid(lat, lng, radius_km)
    try:
        resp = requests.get(
            ELEVATION_URL,
            params={"latitude": ",".join(f"{p[0]:.4f}" for p in pts),
                    "longitude": ",".join(f"{p[1]:.4f}" for p in pts)},
            timeout=timeout, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        elev = [e for e in resp.json().get("elevation", []) if e is not None]
    except (requests.RequestException, ValueError):
        return {"relief_range_m": None, "relief_std_m": None}
    if len(elev) < 4:
        return {"relief_range_m": None, "relief_std_m": None}
    mean = sum(elev) / len(elev)
    std = math.sqrt(sum((e - mean) ** 2 for e in elev) / len(elev))
    return {"relief_range_m": round(max(elev) - min(elev), 1),
            "relief_std_m": round(std, 1)}


def _relief_grid(lat, lng, radius_km, steps=5):
    """A `steps`x`steps` grid of sample points within the disc (<=25 points)."""
    pts = []
    for i in range(steps):
        fy = (i / (steps - 1)) * 2 - 1 if steps > 1 else 0.0     # -1..1
        for j in range(steps):
            fx = (j / (steps - 1)) * 2 - 1 if steps > 1 else 0.0
            if fx * fx + fy * fy > 1.0:
                continue                                          # outside the disc
            # offset (fy north, fx east) by up to radius_km
            p = _destination(lat, lng, 0.0, radius_km * fy)       # north/south
            p = _destination(p[0], p[1], 90.0, radius_km * fx)    # east/west
            pts.append(p)
    return pts or [(lat, lng)]
