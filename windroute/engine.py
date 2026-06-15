"""Core logic. No printing, no I/O beyond HTTP — pure functions a front-end calls."""
from __future__ import annotations

import math
import re
import time
import datetime as dt
from dataclasses import dataclass, field

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ORS_URL = "https://api.openrouteservice.org/v2/directions/{profile}/geojson"
USER_AGENT = "windroute/0.1 (personal cycling tool)"

# Ride type -> ORS cycling profile. Road rides use "cycling-regular" rather than
# "cycling-road" on purpose: cycling-road hard-avoids multiuse paths and bike
# lanes (it kept us off the paved Hickory Creek trail entirely — 0% vs 72% on
# cycling-regular), which fights the rider's "use a good paved trail to dodge
# traffic" preference. cycling-regular makes paths/lanes available; the mild path
# penalty (W_PATH) keeps roads preferred and the gravel penalty + OSM surface keep
# real gravel out of road rides, so the balance lives in scoring, not the profile.
PROFILE_BY_RIDE = {
    "road": "cycling-regular",
    "gravel": "cycling-mountain",
    "mixed": "cycling-regular",
}

# ORS "surface" extra-info codes, bucketed. This is approximate — OSM surface
# tagging is incomplete, so treat the paved/unpaved split as a strong hint,
# not gospel (you'll still want to eyeball gravel in Street View).
PAVED_CODES = {1, 3, 4, 5, 6, 7, 14}            # paved / asphalt / concrete / etc.
UNPAVED_CODES = {2, 8, 9, 10, 11, 12, 15, 16, 17, 18}  # gravel / dirt / ground / etc.

# ORS "waytype" extra-info codes. 1 = "State Road" is the arterial/US-highway
# class (US-12, US-35, etc.) — busy, fast traffic, what quiet-road riders avoid.
# The pleasant county/township roads are 2 "Road" and 3 "Street", so penalizing
# only code 1 steers off highways without punishing the good back roads.
BUSY_WAYTYPES = {1}

# Separated bike/foot paths: 4 = Path, 6 = Cycleway, 7 = Footway. These are the
# off-road multiuse trails the rider mildly dislikes (passing pedestrians) but
# tolerates to dodge traffic. Mildly penalized so they lose to quiet roads but
# still beat busy highways. NOTE: on-road bike *lanes* are tagged on the road
# itself (cycleway=lane), so ORS waytype can't see them — only OSM can; those are
# handled separately via OverpassSurface + bikelane_frac.
PATH_WAYTYPES = {4, 6, 7}

COMPASS_16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# Full-word compass directions -> bearing in degrees (0=N, 90=E). The 16-point
# abbreviations (N, SSE, …) are handled via COMPASS_16; this covers the spelled-out
# forms a person is likely to type ("south", "southeast", "south south west").
_COMPASS_WORDS = {
    "north": 0.0, "northnortheast": 22.5, "northeast": 45.0, "eastnortheast": 67.5,
    "east": 90.0, "eastsoutheast": 112.5, "southeast": 135.0, "southsoutheast": 157.5,
    "south": 180.0, "southsouthwest": 202.5, "southwest": 225.0, "westsouthwest": 247.5,
    "west": 270.0, "westnorthwest": 292.5, "northwest": 315.0, "northnorthwest": 337.5,
}


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #
@dataclass
class Wind:
    direction_from_deg: float   # meteorological convention: direction wind comes FROM
    speed_mph: float
    gust_mph: float
    valid_time: str             # local ISO timestamp the forecast applies to

    @property
    def into_wind_bearing(self) -> float:
        """Heading you ride to go straight INTO the wind (== the 'from' direction)."""
        return self.direction_from_deg % 360


@dataclass
class Candidate:
    coords: list                # [(lat, lng), ...]
    distance_km: float
    ascent_m: float
    paved_frac: float
    unpaved_frac: float
    shape: str = "loop"         # "loop" | "out-and-back" | "lollipop"
    busy_frac: float = 0.0      # fraction on arterial "State Road" class (US-highways)
    path_frac: float = 0.0      # fraction on separated bike/foot paths (multiuse trails)
    path_run_frac: float = 0.0  # LONGEST contiguous path run as a fraction of the route
                                # (the connector-vs-destination signal: a short run is a
                                # trail used to link roads; a long run is "riding the path")
    bikelane_frac: float = 0.0  # fraction on roads with an on-road bike lane (OSM only)
    surface_by_source: dict = field(default_factory=dict)  # source name -> unpaved_frac
    score_coords: list = None   # subset of coords the wind score uses (staging: the
                                # destination loop only, so the fixed transit legs to/from
                                # a ride zone don't dominate the wind line). None = whole route.
    wind_score: float = 0.0     # first-half headwind minus second-half headwind
    surface_score: float = 0.0
    self_intersections: int = 0 # times the route crosses itself (tangle / messiness signal)
    total_score: float = 0.0


@dataclass
class RouteOption:
    """One route surfaced to the rider, with why it's worth considering.

    `select_route_options` returns a primary recommendation plus a few
    alternatives, each leading on a DIFFERENT benefit (a stronger wind line,
    quieter roads, more bike lane, a different direction) so the choices are
    genuinely distinct rather than three near-identical loops differing only by
    round-trip seed.
    """
    candidate: Candidate
    role: str = "alternative"   # "recommended" | "alternative"
    headline: str = ""          # short label, e.g. "Quieter roads"
    reasons: list = field(default_factory=list)  # human-readable bullet points


# --------------------------------------------------------------------------- #
# Geocoding + wind (Open-Meteo, free, no key)
# --------------------------------------------------------------------------- #
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}


def geocode(place: str):
    """Return (lat, lng, label) for a location string.

    Accepts three forms, picked automatically so you can start a ride from an
    exact spot (e.g. the corner near your house that reaches the bike path),
    not just a town centroid:
      1. Raw coordinates ``"41.5358,-87.8890"`` -> used directly (most precise).
      2. A street address ``"19150 88th Ave, Mokena, IL"`` -> OSM Nominatim, which
         resolves house numbers / streets (Open-Meteo only knows town centroids).
      3. A town / "City, ST" name -> Open-Meteo (fast), falling back to Nominatim.
    """
    coords = _parse_coords(place)
    if coords:
        lat, lng = coords
        return lat, lng, f"{lat:.5f}, {lng:.5f}"

    # Anything with a digit (house number, route number, ZIP) is address-like and
    # belongs to Nominatim first; plain town names go to the faster Open-Meteo.
    if any(ch.isdigit() for ch in place):
        try:
            return _geocode_nominatim(place)
        except (ValueError, requests.RequestException):
            pass                                   # fall through to town geocoder

    try:
        return _geocode_openmeteo(place)
    except (ValueError, requests.RequestException):
        return _geocode_nominatim(place)           # odd names, or Open-Meteo rate-limited


def _parse_coords(place: str):
    """Parse a coordinate pair into (lat, lng), or None if it isn't one.

    Handles decimal ``"41.5267,-87.8717"`` and the degrees-minutes-seconds form
    Google Maps copies, ``"41°31'36.3\"N 87°52'18.0\"W"``.
    """
    dms = _parse_dms(place)
    if dms:
        return dms
    parts = place.replace(" ", "").split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
        return lat, lng
    return None


# DMS like 41°31'36.3"N — three numbers (deg/min/sec) then a hemisphere letter.
# Non-digit runs (\D+) absorb whatever symbols were used for °, ', ".
_DMS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)\D*([NSEW])",
    re.IGNORECASE)


def _parse_dms(place: str):
    """Parse a 'D°M\\'S\"H D°M\\'S\"H' degrees-minutes-seconds pair, else None."""
    matches = _DMS_RE.findall(place.strip())
    if len(matches) != 2:
        return None
    lat = lng = None
    for deg, minu, sec, hemi in matches:
        val = float(deg) + float(minu) / 60.0 + float(sec) / 3600.0
        hemi = hemi.upper()
        if hemi in ("S", "W"):
            val = -val
        if hemi in ("N", "S"):
            lat = val
        else:
            lng = val
    if lat is None or lng is None:
        return None
    if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
        return lat, lng
    return None


def _geocode_openmeteo(place: str):
    """Town / "City, ST" geocoding via Open-Meteo (no key). Raises if not found."""
    params: dict = {"count": 1}
    # "City, ST" / "City, State" notation: strip the suffix and add country filter
    if "," in place:
        city_part, region_part = place.split(",", 1)
        region_part = region_part.strip()
        if region_part.upper() in _US_STATES or len(region_part) > 2:
            params["name"] = city_part.strip()
            params["country"] = "US"
        else:
            params["name"] = place
    else:
        params["name"] = place

    r = requests.get(GEOCODE_URL, params=params, timeout=20)
    r.raise_for_status()
    results = r.json().get("results")
    if not results:
        raise ValueError(f"Could not find a location matching {place!r}")
    top = results[0]
    label = ", ".join(
        p for p in (top.get("name"), top.get("admin1"), top.get("country_code")) if p
    )
    return top["latitude"], top["longitude"], label


def suggest_places(query: str, count: int = 6):
    """Type-ahead place suggestions for a partial query (towns/cities).

    Backs the web form's location autocomplete. Uses Open-Meteo geocoding, which is
    built for name search and fine with this volume — unlike Nominatim, whose usage
    policy forbids per-keystroke autocomplete. Returns a list of
    ``{"label", "lat", "lng"}``; each label is a "City, Region, CC" string that
    geocode() can resolve again on submit. Never raises — returns [] on any problem.
    """
    q = (query or "").strip()
    name = q.split(",", 1)[0].strip() if "," in q else q   # match on the city part
    if len(name) < 2:
        return []
    try:
        # Over-fetch, then re-rank locally: Open-Meteo returns exact-name matches
        # first, so a tiny same-named village outranks the populous place the user
        # almost certainly means (e.g. "Moke, CD" ahead of "Mokena, IL").
        r = requests.get(GEOCODE_URL, params={
            "name": name, "count": 20,
            "language": "en", "format": "json"}, timeout=8)
        r.raise_for_status()
        results = r.json().get("results") or []
    except (requests.RequestException, ValueError):
        return []
    nlow = name.lower()
    results.sort(key=lambda t: (
        not str(t.get("name", "")).lower().startswith(nlow),   # prefix matches first
        -(t.get("population") or 0),                            # then most populous
    ))
    out = []
    for top in results:
        label = ", ".join(
            p for p in (top.get("name"), top.get("admin1"), top.get("country_code")) if p
        )
        if label and "latitude" in top and "longitude" in top:
            out.append({"label": label, "lat": top["latitude"], "lng": top["longitude"]})
        if len(out) >= count:
            break
    return out


def _geocode_nominatim(place: str):
    """Street-address geocoding via OSM Nominatim (no key; handles house numbers).

    Nominatim's usage policy asks for a real User-Agent and modest volume — one
    call per plan is well within that. Builds a short label from the address parts
    instead of Nominatim's very long display_name.
    """
    r = requests.get(
        NOMINATIM_URL,
        params={"q": place, "format": "jsonv2", "limit": 1, "addressdetails": 1},
        headers={"User-Agent": USER_AGENT}, timeout=20,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        raise ValueError(f"Could not find a location matching {place!r}")
    top = results[0]
    addr = top.get("address", {})
    town = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("hamlet") or addr.get("suburb"))
    house = " ".join(x for x in (addr.get("house_number"), addr.get("road")) if x)
    label = ", ".join(p for p in (
        house or None, town, addr.get("state"),
        (addr.get("country_code") or "").upper() or None) if p)
    return float(top["lat"]), float(top["lon"]), label or top.get("display_name", place)


def get_wind(lat: float, lng: float, when: dt.datetime) -> Wind:
    """Wind forecast for the hour nearest `when` (naive local time).

    Open-Meteo is the primary source (free, no key, worldwide). If it fails — most
    notably HTTP 429 when running from a shared cloud IP that Open-Meteo throttles
    (e.g. a free hosting tier) — fall back to the US National Weather Service
    (`api.weather.gov`, keyless, US-only). Locally Open-Meteo just works and NWS is
    never touched.
    """
    try:
        return _wind_from_open_meteo(lat, lng, when)
    except requests.RequestException:
        return _wind_from_nws(lat, lng, when)


def _wind_from_open_meteo(lat: float, lng: float, when: dt.datetime) -> Wind:
    r = requests.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lng,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": 7,
        },
        timeout=20,
    )
    r.raise_for_status()
    h = r.json()["hourly"]
    idx = _nearest_time_index(h["time"], when)
    return Wind(
        direction_from_deg=float(h["wind_direction_10m"][idx]),
        speed_mph=float(h["wind_speed_10m"][idx]),
        gust_mph=float(h["wind_gusts_10m"][idx]),
        valid_time=h["time"][idx],
    )


def _wind_from_nws(lat: float, lng: float, when: dt.datetime) -> Wind:
    """US National Weather Service hourly wind (keyless, US-only).

    Two calls: /points/{lat},{lng} gives the hourly-forecast URL, then that URL
    returns hourly periods with windSpeed ('10 mph' / '5 to 10 mph') and
    windDirection (a compass label). NWS requires a descriptive User-Agent and
    only covers US locations (a point outside the US 404s).
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    pt = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lng:.4f}",
                      headers=headers, timeout=20)
    pt.raise_for_status()
    hourly_url = pt.json()["properties"]["forecastHourly"]
    fc = requests.get(hourly_url, headers=headers, timeout=20)
    fc.raise_for_status()
    periods = fc.json().get("properties", {}).get("periods") or []
    if not periods:
        raise ValueError("NWS returned no forecast periods for this location")

    target = when.replace(minute=0, second=0, microsecond=0)
    best, best_diff = None, None
    for per in periods:
        t = dt.datetime.fromisoformat(per["startTime"]).replace(tzinfo=None)
        diff = abs((t - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff, best = diff, per
    return Wind(
        direction_from_deg=_compass_to_deg(best.get("windDirection")),
        speed_mph=_parse_mph(best.get("windSpeed")),
        gust_mph=_parse_mph(best.get("windGust")),
        valid_time=str(best.get("startTime", ""))[:16],   # 'YYYY-MM-DDTHH:MM'
    )


def _compass_to_deg(label) -> float:
    """A 16-point compass label ('SSW') -> degrees the wind comes FROM (0=N)."""
    if not label:
        return 0.0
    try:
        return COMPASS_16.index(str(label).strip().upper()) * 22.5
    except ValueError:
        return 0.0


def _parse_mph(text) -> float:
    """Pull a speed out of an NWS string like '10 mph' or '5 to 10 mph' (-> 10)."""
    if not text:
        return 0.0
    nums = re.findall(r"\d+(?:\.\d+)?", str(text))
    return max(float(n) for n in nums) if nums else 0.0


def get_wind_historical(lat: float, lng: float, when: dt.datetime) -> Wind:
    """Wind that actually blew at the hour nearest `when` (a past ride time).

    `get_wind` only covers the 7-day forecast, so backfilling the wind for a
    recorded trip needs the Open-Meteo archive. The archive lags real time by a
    few days, so for very recent dates we fall back to the forecast endpoint's
    `past_days` window (which reaches back up to ~92 days). `when` is naive local.
    """
    if when.tzinfo is not None:
        # RWGPS timestamps carry the ride's local offset; drop it to a naive
        # local wall-clock time, which is what Open-Meteo (timezone=auto) returns
        # and what _nearest_time_index compares against. Mixing the two raises
        # "can't subtract offset-naive and offset-aware datetimes".
        when = when.replace(tzinfo=None)
    days_ago = (dt.datetime.now() - when).days
    if days_ago <= 7:
        return _wind_from_forecast_past(lat, lng, when, past_days=min(92, days_ago + 2))
    return _wind_from_archive(lat, lng, when)


def _wind_from_archive(lat: float, lng: float, when: dt.datetime) -> Wind:
    day = when.strftime("%Y-%m-%d")
    r = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": lat,
            "longitude": lng,
            "start_date": day,
            "end_date": day,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "wind_speed_unit": "mph",
            "timezone": "auto",
        },
        timeout=30,
    )
    r.raise_for_status()
    h = r.json().get("hourly") or {}
    if not h.get("time"):
        # Archive has no data yet for this (recent) date — fall back to forecast.
        return _wind_from_forecast_past(lat, lng, when, past_days=92)
    return _wind_from_hourly(h, when)


def _wind_from_forecast_past(lat: float, lng: float, when: dt.datetime,
                             past_days: int) -> Wind:
    r = requests.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lng,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "past_days": max(1, past_days),
            "forecast_days": 1,
        },
        timeout=30,
    )
    r.raise_for_status()
    return _wind_from_hourly(r.json()["hourly"], when)


def _wind_from_hourly(h: dict, when: dt.datetime) -> Wind:
    """Build a Wind from an Open-Meteo `hourly` block at the hour nearest `when`."""
    idx = _nearest_time_index(h["time"], when)
    gusts = h.get("wind_gusts_10m") or []
    gust = gusts[idx] if idx < len(gusts) and gusts[idx] is not None else 0.0
    return Wind(
        direction_from_deg=float(h["wind_direction_10m"][idx]),
        speed_mph=float(h["wind_speed_10m"][idx]),
        gust_mph=float(gust),
        valid_time=h["time"][idx],
    )


def _nearest_time_index(times, when: dt.datetime) -> int:
    target = when.replace(minute=0, second=0, microsecond=0)
    best_diff, best_i = None, 0
    for i, t in enumerate(times):
        ti = dt.datetime.fromisoformat(t)
        diff = abs((ti - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff, best_i = diff, i
    return best_i


# --------------------------------------------------------------------------- #
# Route generation (OpenRouteService, needs a free API key)
# --------------------------------------------------------------------------- #
SHAPES = ("loop", "out-and-back", "lollipop", "rectangle", "staging", "roundtrip")

# Polygon-loop variety per seed: cycle vertex counts and travel orientation so a
# handful of "loop" seeds explore different road sets / wind lines, not clones.
_LOOP_SIDES = (5, 4, 6, 5, 4, 6)

# Angular offsets (deg) tried around the aiming bearing for directional shapes,
# nearest-first so the most wind-aligned options get generated when n is small.
_BEARING_OFFSETS = [0, 30, -30, 60, -60, 90, -90, 135, -135, 180]


def _polyline_km(coords):
    """Length (km) of a lat/lng polyline."""
    return sum(_haversine_km(a, b) for a, b in zip(coords, coords[1:]))


def _self_intersections(coords):
    """Count times a route crosses itself (non-adjacent segment pairs that
    intersect). A clean loop or rectangle scores 0; a tangled round_trip loop -
    where ORS scattered via-points that cross-connect - scores high (verified:
    clean rectangle 0, messy lollipop 67).

    A deliberate retrace (the out leg + reversed out leg of an out-and-back, or a
    lollipop stem) is an exact COLLINEAR overlap, not a crossing, so it scores 0
    here - this messiness signal doesn't unfairly punish retraced shapes. O(n^2)
    with a bounding-box quick-reject, which is plenty fast at our point counts.
    """
    pts = coords
    m = len(pts) - 1
    if m < 2:
        return 0
    # A closed loop's first and last segments meet at the start; that shared vertex
    # is a touch, not a self-crossing, so don't count the (first, last) pair.
    closed = _haversine_km(pts[0], pts[-1]) * 1000.0 <= 5.0

    def ccw(a, b, d):                                  # signed area sign test
        return (d[0] - a[0]) * (b[1] - a[1]) - (b[0] - a[0]) * (d[1] - a[1])

    count = 0
    for i in range(m):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        lo_x, hi_x = (ax, bx) if ax < bx else (bx, ax)
        lo_y, hi_y = (ay, by) if ay < by else (by, ay)
        for j in range(i + 2, m):
            if closed and i == 0 and j == m - 1:
                continue                               # start-closure touch, not a cross
            cx, cy = pts[j]
            dx, dy = pts[j + 1]
            if (cx > hi_x and dx > hi_x) or (cx < lo_x and dx < lo_x):
                continue                               # bounding boxes can't overlap
            if (cy > hi_y and dy > hi_y) or (cy < lo_y and dy < lo_y):
                continue
            d1 = ccw(pts[j], pts[j + 1], pts[i])
            d2 = ccw(pts[j], pts[j + 1], pts[i + 1])
            d3 = ccw(pts[i], pts[i + 1], pts[j])
            d4 = ccw(pts[i], pts[i + 1], pts[j + 1])
            if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
                count += 1
    return count


def _strip_backtracks(coords, eles=None, tol_m=5.0):
    """Remove immediate out-and-back stubs from a single routed leg.

    ORS round_trip/directions occasionally routes a short spur onto a side road
    and straight back to the same node (A -> B -> A), which renders as a
    perpendicular spike you'd never actually ride. We unwind any vertex whose two
    neighbours coincide (within `tol_m`); applied iteratively this collapses
    multi-point spurs of any length. The matched stubs return to the EXACT prior
    node (~0 m), so a tight tolerance removes them without thinning the dense
    geometry of straight roads (verified: counts are flat from 2-5 m, then start
    eating real points past ~8 m).

    IMPORTANT: run this on a SINGLE ORS leg, before the out-and-back / lollipop /
    staging concatenation. A deliberate retrace (out leg + reversed out leg) looks
    exactly like one giant backtrack, so cleaning the *assembled* route would
    collapse the whole return. `eles` (if the same length as `coords`) is filtered
    in lockstep so the two stay aligned. Returns (coords, eles).
    """
    keep = []
    for i, p in enumerate(coords):
        if len(keep) >= 2 and _haversine_km(coords[keep[-2]], p) * 1000.0 <= tol_m:
            keep.pop()                    # the last kept point was a dead-end tip
            continue                      # p coincides with keep[-2], already present
        if keep and _haversine_km(coords[keep[-1]], p) * 1000.0 <= 0.5:
            continue                      # drop only exact-duplicate points
        keep.append(i)
    if len(keep) == len(coords):
        return coords, eles               # nothing to do; keep the originals
    new_coords = [coords[i] for i in keep]
    new_eles = ([eles[i] for i in keep]
                if eles and len(eles) == len(coords) else eles)
    return new_coords, new_eles


def _ors_directions(api_key, profile, coordinates, timeout, round_trip=None):
    """One ORS directions call.

    Returns (coords, eles, dist_km, paved, unpaved, busy, path, path_run_km).
    `coordinates` is ORS-order [[lng, lat], ...]; pass `round_trip` dict for loops.
    `busy` is the fraction of distance on arterial "State Road" class (US-highways);
    `path` is the fraction on separated bike/foot paths (multiuse trails);
    `path_run_km` is the longest *contiguous* path stretch (km) on this leg.
    """
    url = ORS_URL.format(profile=profile)
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {
        "coordinates": coordinates,
        "extra_info": ["surface", "waytype"],
        "elevation": True,
        "instructions": False,
    }
    if round_trip is not None:
        body["options"] = {"round_trip": round_trip}

    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code == 429:                  # rate limited — back off once
        time.sleep(2.5)
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()

    feat = resp.json()["features"][0]
    props = feat["properties"]
    geom = feat["geometry"]["coordinates"]
    coords = [(c[1], c[0]) for c in geom]                        # -> (lat, lng)
    eles = [c[2] for c in geom if len(c) > 2]
    dist_km = props.get("summary", {}).get("distance", 0.0) / 1000.0
    extras = props.get("extras", {})
    paved, unpaved = _surface_fractions(extras)
    busy = _waytype_fraction(extras, BUSY_WAYTYPES)
    path = _waytype_fraction(extras, PATH_WAYTYPES)
    # path_run_km uses the positional waytype values, so compute it from the raw
    # geometry BEFORE stripping stubs (which would shift the indices).
    path_run_km = _waytype_run_km(extras, coords, PATH_WAYTYPES)
    # Drop the little A->B->A spurs ORS sometimes emits; subtract their mileage
    # from the ORS road distance so the reported length matches the cleaned line.
    clean, eles = _strip_backtracks(coords, eles)
    if len(clean) != len(coords):
        dist_km = max(0.0, dist_km - (_polyline_km(coords) - _polyline_km(clean)))
        coords = clean
    return coords, eles, dist_km, paved, unpaved, busy, path, path_run_km


def _make_roundtrip(api_key, profile, lat, lng, target_km, points, seed, timeout):
    """ORS round_trip loop. Kept as an opt-in shape ("roundtrip") only: it scatters
    via-points in a ring and connects them, which often tangles or detours onto
    side roads (see _self_intersections). The default "loop" is now the clean
    geometric polygon below; this stays for variety / as a fallback."""
    coords, eles, dist, paved, unpaved, busy, path, path_run = _ors_directions(
        api_key, profile, [[lng, lat]], timeout,
        round_trip={"length": int(target_km * 1000), "points": points, "seed": seed})
    return Candidate(coords=coords, distance_km=dist,
                     ascent_m=_smoothed_ascent(eles) if eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_frac=path, path_run_frac=(path_run / dist if dist else 0.0),
                     shape="roundtrip")


def _polygon_loop_waypoints(lat, lng, target_km, bearing, n_sides, orient, detour):
    """Corner points (incl. the start) of a regular polygon loop through `start`.

    The loop is a regular `n_sides`-gon whose circumscribing circle is centered one
    radius away in the `bearing` direction, so `start` sits ON the circle and the
    loop bulges toward `bearing` (aim that into the wind to ride out fresh, home with
    a tailwind). Routing point-to-point through these corners in angular order traces
    a convex polygon - so, unlike ORS round_trip, it can't scatter via-points, tangle,
    or spur onto perpendicular roads. `orient` (+/-1) picks the travel direction.

    The radius is sized so the polygon's crow-flies perimeter times `detour` (roads
    zigzag the grid) lands near `target_km`. Returns [(lat, lng), ...] starting and
    ending at the exact start (a guaranteed-routable node).
    """
    n = max(3, int(n_sides))
    radius = target_km / (detour * 2.0 * n * math.sin(math.pi / n))   # crow radius (km)
    clat, clng = _destination(lat, lng, bearing, radius)             # circle center
    start_angle = (bearing + 180.0) % 360                            # start as seen from center
    verts = [(lat, lng)]                                             # v0 == exact start
    for k in range(1, n):
        ang = (start_angle + orient * k * 360.0 / n) % 360
        verts.append(_destination(clat, clng, ang, radius))
    verts.append((lat, lng))                                        # close back to start
    return verts


def _make_polygon_loop(api_key, profile, lat, lng, target_km, bearing, timeout,
                       n_sides=5, orient=1, detour=1.25):
    """A clean geometric loop: route through the corners of a polygon around the
    start (see _polygon_loop_waypoints). No round_trip, so no scattered via-points,
    tangles, or perpendicular spurs by construction."""
    verts = _polygon_loop_waypoints(lat, lng, target_km, bearing, n_sides, orient, detour)
    pts = [[vlng, vlat] for vlat, vlng in verts]                    # -> ORS [lng, lat]
    coords, eles, dist, paved, unpaved, busy, path, path_run = _ors_directions(
        api_key, profile, pts, timeout)
    return Candidate(coords=coords, distance_km=dist,
                     ascent_m=_smoothed_ascent(eles) if eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_frac=path, path_run_frac=(path_run / dist if dist else 0.0),
                     shape="loop")


def _make_out_back(api_key, profile, lat, lng, target_km, bearing, timeout, detour=1.3):
    """Route to a point ~target/2 away on `bearing`, then mirror the path home."""
    crow_km = (target_km / 2.0) / detour                         # roads aren't straight
    dlat, dlng = _destination(lat, lng, bearing, crow_km)
    coords, eles, dist, paved, unpaved, busy, path, path_run = _ors_directions(
        api_key, profile, [[lng, lat], [dlng, dlat]], timeout)
    full_coords = coords + coords[-2::-1]                        # out + reversed (no dup turn)
    full_eles = (eles + eles[-2::-1]) if eles else []
    # The leg's path stretch is ridden both ways, so an out-and-back *on* a trail
    # has run_frac ~ path_run/dist (≈1.0 if the whole leg is path) — exactly the
    # "riding the path as the destination" case this should flag.
    return Candidate(coords=full_coords, distance_km=dist * 2.0,
                     ascent_m=_smoothed_ascent(full_eles) if full_eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_frac=path, path_run_frac=(path_run / dist if dist else 0.0),
                     shape="out-and-back")


def _make_lollipop(api_key, profile, lat, lng, target_km, bearing, seed,
                   timeout, detour=1.3, loop_frac=0.35):
    """Out-and-back stem with a clean geometric 'candy' loop at the far end.

    The candy is a polygon loop (like the default "loop" shape), NOT an ORS
    round_trip, so it can't tangle or spur. It's anchored at the stem's actual
    routed endpoint (a real road node) rather than the crow-flies target, so the
    far waypoint is always routable and the stem<->candy seam has no stub. Sides
    and travel direction vary by `seed` for variety; the candy bulges further out
    along `bearing` (continuing away from home)."""
    loop_km = max(5.0, target_km * loop_frac)
    stem_oneway = max(1.0, (target_km - loop_km) / 2.0)
    crow_km = stem_oneway / detour
    dlat, dlng = _destination(lat, lng, bearing, crow_km)

    s_coords, s_eles, s_dist, s_pav, s_unp, s_busy, s_path, s_run = _ors_directions(
        api_key, profile, [[lng, lat], [dlng, dlat]], timeout)

    # Anchor the candy at the stem's real end node, and route it as a polygon loop.
    glat, glng = s_coords[-1]
    verts = _polygon_loop_waypoints(
        glat, glng, loop_km, bearing,
        n_sides=_LOOP_SIDES[seed % len(_LOOP_SIDES)],
        orient=(1 if (seed // len(_LOOP_SIDES)) % 2 == 0 else -1), detour=1.25)
    l_coords, l_eles, l_dist, l_pav, l_unp, l_busy, l_path, l_run = _ors_directions(
        api_key, profile, [[vlng, vlat] for vlat, vlng in verts], timeout)

    full_coords = s_coords + l_coords[1:] + s_coords[-2::-1]     # stem + candy + stem back
    full_eles = (s_eles + l_eles[1:] + s_eles[-2::-1]) if (s_eles and l_eles) else []
    total_dist = s_dist * 2.0 + l_dist

    stem_w, loop_w = 2.0 * s_dist, l_dist                        # distance-weighted blend
    tot = stem_w + loop_w
    paved = (s_pav * stem_w + l_pav * loop_w) / tot if tot else 1.0
    unpaved = (s_unp * stem_w + l_unp * loop_w) / tot if tot else 0.0
    busy = (s_busy * stem_w + l_busy * loop_w) / tot if tot else 0.0
    path = (s_path * stem_w + l_path * loop_w) / tot if tot else 0.0
    path_run = max(s_run, l_run) / total_dist if total_dist else 0.0   # longest single run
    return Candidate(coords=full_coords, distance_km=total_dist,
                     ascent_m=_smoothed_ascent(full_eles) if full_eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_frac=path, path_run_frac=path_run, shape="lollipop")


def _make_staging(api_key, profile, lat, lng, target_km, zone, seed,
                  timeout, min_loop_km=8.0, detour=1.3):
    """Transit to a detected 'good riding' zone, loop there, transit home.

    Like a lollipop, but the stem is aimed at the ride zone the detector found
    (e.g. the quiet cornfields south of a suburb) instead of a wind bearing, and
    only the destination loop is wind-scored (via score_coords). The two transit
    legs are a fixed cost of reaching good country, so letting them drive the wind
    line would be pointless — you ride them whatever the wind.

    `zone` is a dict with 'lat'/'lng'. The zone center is a farmland *centroid*
    that often sits off-road (mid-field), so we NEVER route a leg to it directly
    (that 404s with ORS code 2010 "no routable point"). Instead the stem aims at a
    crow point a loop-radius SHORT of the centroid, and the destination loop is a
    clean geometric polygon (not ORS round_trip) anchored at the stem's real routed
    endpoint and bulging toward the zone — so it centers on the centroid using only
    routable ring waypoints. The loop budget is the ride minus the crow-flies
    round-trip transit (inflated by `detour`), floored at `min_loop_km`.
    """
    zlat, zlng = zone["lat"], zone["lng"]
    crow = _haversine_km((lat, lng), (zlat, zlng))
    bearing = _bearing((lat, lng), (zlat, zlng))                 # home -> zone
    loop_km = max(min_loop_km, target_km - 2.0 * crow * detour)

    # Geometric polygon loop for the zone; end the stem a loop-radius short of the
    # centroid so the loop, bulging toward the zone, centers on it.
    n_sides = _LOOP_SIDES[seed % len(_LOOP_SIDES)]
    orient = 1 if (seed // len(_LOOP_SIDES)) % 2 == 0 else -1
    loop_detour = 1.25
    radius = loop_km / (loop_detour * 2.0 * n_sides * math.sin(math.pi / n_sides))
    stem_crow = max(0.5, crow - radius)
    tlat, tlng = _destination(lat, lng, bearing, stem_crow)      # stem target (near zone edge)

    s_coords, s_eles, s_dist, s_pav, s_unp, s_busy, s_path, s_run = _ors_directions(
        api_key, profile, [[lng, lat], [tlng, tlat]], timeout)

    # Anchor the loop at the stem's real end node and bulge it toward the zone.
    glat, glng = s_coords[-1]
    verts = _polygon_loop_waypoints(glat, glng, loop_km, bearing, n_sides, orient, loop_detour)
    l_coords, l_eles, l_dist, l_pav, l_unp, l_busy, l_path, l_run = _ors_directions(
        api_key, profile, [[vlng, vlat] for vlat, vlng in verts], timeout)

    full_coords = s_coords + l_coords[1:] + s_coords[-2::-1]     # stem + loop + stem back
    full_eles = (s_eles + l_eles[1:] + s_eles[-2::-1]) if (s_eles and l_eles) else []
    total_dist = s_dist * 2.0 + l_dist

    stem_w, loop_w = 2.0 * s_dist, l_dist                        # distance-weighted blend
    tot = stem_w + loop_w
    paved = (s_pav * stem_w + l_pav * loop_w) / tot if tot else 1.0
    unpaved = (s_unp * stem_w + l_unp * loop_w) / tot if tot else 0.0
    busy = (s_busy * stem_w + l_busy * loop_w) / tot if tot else 0.0
    path = (s_path * stem_w + l_path * loop_w) / tot if tot else 0.0
    path_run = max(s_run, l_run) / total_dist if total_dist else 0.0   # longest single run
    return Candidate(coords=full_coords, distance_km=total_dist,
                     ascent_m=_smoothed_ascent(full_eles) if full_eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_frac=path, path_run_frac=path_run, shape="staging",
                     score_coords=l_coords)


def _make_rectangle(api_key, profile, lat, lng, target_km, bearing, timeout,
                    detour=1.25, width_frac=0.12, cross_sign=1):
    """An elongated rectangle aligned with the wind: long leg into the wind, a
    short crosswind jog, a long downwind leg on a parallel road, short close.

    Four corners routed as a through-path so ORS snaps it onto the actual road
    grid (great in section-road country like Champaign). `cross_sign` (+/-1)
    picks which side the parallel return road sits on.
    """
    width_km = max(2.0, target_km * width_frac)              # short crosswind sides
    long_oneway = max(1.0, (target_km - 2.0 * width_km) / 2.0)
    long_crow = long_oneway / detour                         # roads zigzag the grid
    width_crow = width_km / detour
    cross = (bearing + 90.0 * (1 if cross_sign >= 0 else -1)) % 360

    a_lat, a_lng = _destination(lat, lng, bearing, long_crow)    # far end, into wind
    b_lat, b_lng = _destination(a_lat, a_lng, cross, width_crow)  # crosswind jog
    c_lat, c_lng = _destination(lat, lng, cross, width_crow)      # near end, offset
    pts = [[lng, lat], [a_lng, a_lat], [b_lng, b_lat], [c_lng, c_lat], [lng, lat]]

    coords, eles, dist, paved, unpaved, busy, path, path_run = _ors_directions(
        api_key, profile, pts, timeout)
    return Candidate(coords=coords, distance_km=dist,
                     ascent_m=_smoothed_ascent(eles) if eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_run_frac=(path_run / dist if dist else 0.0),
                     path_frac=path, shape="rectangle")


def generate_candidates(lat, lng, target_km, ride_type, api_key,
                        n=8, points=5, timeout=40, sleep=0.4,
                        shapes=("loop",), into_wind_bearing=None, zone=None):
    """Generate `n` candidate routes of ~target_km from (lat, lng).

    `shapes` chooses which route forms to produce ("loop", "out-and-back",
    "lollipop"); `n` is split across them. Directional shapes (out-and-back,
    lollipop) are aimed at `into_wind_bearing` first (so you ride out into the
    wind, home with a tailwind) with widening offsets for variety; everything is
    still scored by `evaluate` afterward. ORS caps loop length at 100 km.

    `zone` (a dict with 'lat'/'lng' from zones.find_ride_zone) enables the
    "staging" shape: transit to that quiet ride zone, loop there scored on the
    wind, transit home. The staging shape is only produced when a zone is given.
    """
    if target_km > 100:
        raise ValueError("OpenRouteService caps round trips at 100 km. Shorten the ride.")
    if not api_key:
        raise ValueError("No OpenRouteService API key. Get a free one and pass --api-key "
                         "or set ORS_API_KEY.")

    profile = PROFILE_BY_RIDE.get(ride_type, "cycling-regular")
    shapes = [s for s in shapes if s in SHAPES] or ["loop"]
    # The staging shape needs a detected zone; drop it if we have none, and never
    # produce it without one (the caller adds it to `shapes` only when zone is set).
    if zone is None:
        shapes = [s for s in shapes if s != "staging"] or ["loop"]

    # Build the per-candidate work plan, splitting n across the chosen shapes.
    plan = []
    i = 0
    while len(plan) < n:
        plan.append(shapes[i % len(shapes)])
        i += 1

    center = into_wind_bearing if into_wind_bearing is not None else 0.0
    out, seeds = [], {s: 0 for s in SHAPES}

    for shape in plan:
        idx = seeds[shape]
        bearing = (center + _BEARING_OFFSETS[idx % len(_BEARING_OFFSETS)]) % 360
        try:
            if shape == "loop":
                # Clean geometric polygon loop; vary sides + travel direction by seed.
                c = _make_polygon_loop(
                    api_key, profile, lat, lng, target_km, bearing, timeout,
                    n_sides=_LOOP_SIDES[idx % len(_LOOP_SIDES)],
                    orient=(1 if (idx // len(_LOOP_SIDES)) % 2 == 0 else -1))
            elif shape == "roundtrip":
                c = _make_roundtrip(api_key, profile, lat, lng, target_km, points, idx, timeout)
            elif shape == "out-and-back":
                c = _make_out_back(api_key, profile, lat, lng, target_km, bearing, timeout)
            elif shape == "rectangle":
                c = _make_rectangle(api_key, profile, lat, lng, target_km, bearing, timeout,
                                    cross_sign=(1 if idx % 2 == 0 else -1))
            elif shape == "staging":
                c = _make_staging(api_key, profile, lat, lng, target_km, zone,
                                  idx, timeout)
            else:  # lollipop
                c = _make_lollipop(api_key, profile, lat, lng, target_km, bearing,
                                   idx, timeout)
            out.append(c)
        except requests.HTTPError:
            pass                                  # skip a bad seed/bearing, keep going
        seeds[shape] += 1
        time.sleep(sleep)

    if not out:
        raise RuntimeError("No routes came back. Check the API key, or that the start "
                           "point is on a routable road and distance <= 100 km.")
    return out


def _surface_fractions(extras):
    surf = extras.get("surface")
    if not surf:
        return 1.0, 0.0
    total = paved = unpaved = 0.0
    for item in surf.get("summary", []):
        code = int(item["value"])
        dist = float(item.get("distance", 0.0))
        total += dist
        if code in PAVED_CODES:
            paved += dist
        elif code in UNPAVED_CODES:
            unpaved += dist
    if total <= 0:
        return 1.0, 0.0
    return paved / total, unpaved / total


def _waytype_fraction(extras, codes):
    """Fraction of route distance whose ORS waytype is in `codes`.

    ORS returns a per-waytype distance summary in extras['waytype']; we sum the
    matching classes over the total. Used both for busy arterials (BUSY_WAYTYPES)
    and separated bike/foot paths (PATH_WAYTYPES). 0.0 when there's no waytype data
    (older responses / no extras) — i.e. assume none rather than guess blind.
    """
    wt = extras.get("waytype")
    if not wt:
        return 0.0
    total = matched = 0.0
    for item in wt.get("summary", []):
        dist = float(item.get("distance", 0.0))
        total += dist
        if int(item["value"]) in codes:
            matched += dist
    return matched / total if total > 0 else 0.0


def _waytype_run_km(extras, coords, codes):
    """Longest *contiguous* run (km) of route on a waytype in `codes`.

    Unlike `_waytype_fraction` (which totals distance regardless of where it is),
    this uses the positional extras['waytype']['values'] — a list of
    [start_idx, end_idx, value] over the geometry coordinates — to find the single
    longest unbroken stretch. That's the connector-vs-destination signal: many short
    path segments stitched between roads stay small, while one long path stretch
    (e.g. an out-and-back down a trail) shows up as a big run. 0.0 with no data.
    """
    wt = extras.get("waytype")
    if not wt or len(coords) < 2:
        return 0.0
    values = wt.get("values")
    if not values:
        return 0.0
    nseg = len(coords) - 1
    is_path = [False] * nseg
    for entry in values:
        try:
            s, e, v = int(entry[0]), int(entry[1]), int(entry[2])
        except (TypeError, ValueError, IndexError):
            continue
        if v in codes:
            for i in range(max(0, s), min(nseg, e)):   # coords s..e -> segments s..e-1
                is_path[i] = True
    best = cur = 0.0
    for i in range(nseg):
        if is_path[i]:
            cur += _haversine_km(coords[i], coords[i + 1])
            best = max(best, cur)
        else:
            cur = 0.0
    return best


def _smoothed_ascent(eles, spike_m=15.0, smooth_win=11, climb_threshold_m=2.0):
    """Total ascent (m) from an elevation series, robust to SRTM dropouts/noise.

    ORS just sums raw point-to-point deltas, so nodata dropouts (an elevation of
    0.0 in the middle of a 230 m plateau) and ordinary SRTM jitter inflate the
    total wildly — e.g. 1700 m of "climb" on a dead-flat Illinois loop. We:
      1. treat <=0 as missing (nodata) and linearly interpolate across the gaps,
      2. median-filter residual isolated spikes,
      3. low-pass with a moving average,
      4. accumulate only rises past a small hysteresis threshold.
    Flat terrain collapses toward ~0 while genuine mountain climbs survive.
    """
    n = len(eles)
    if n < 2:
        return 0.0

    # 1) interpolate across nodata gaps (elevation <= 0)
    prev_valid = [None] * n
    last = None
    for i in range(n):
        if eles[i] > 0.0:
            last = i
        prev_valid[i] = last
    next_valid = [None] * n
    nxt = None
    for i in range(n - 1, -1, -1):
        if eles[i] > 0.0:
            nxt = i
        next_valid[i] = nxt
    if all(v is None for v in prev_valid) and all(v is None for v in next_valid):
        return 0.0
    e = list(eles)
    for i in range(n):
        if eles[i] > 0.0:
            continue
        lo, hi = prev_valid[i], next_valid[i]
        if lo is None:
            e[i] = eles[hi]
        elif hi is None:
            e[i] = eles[lo]
        else:
            e[i] = eles[lo] + (eles[hi] - eles[lo]) * ((i - lo) / (hi - lo))

    # 2) median filter to repair isolated spikes
    med = list(e)
    for i in range(n):
        lo, hi = max(0, i - 2), min(n, i + 3)
        window = sorted(e[lo:hi])
        m = window[len(window) // 2]
        if abs(e[i] - m) > spike_m:
            med[i] = m

    # 3) moving-average low-pass
    half = smooth_win // 2
    smooth = [0.0] * n
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        smooth[i] = sum(med[lo:hi]) / (hi - lo)

    # 4) accumulate ascent with a hysteresis deadband
    ascent = 0.0
    ref = smooth[0]
    for x in smooth[1:]:
        d = x - ref
        if d >= climb_threshold_m:
            ascent += d
            ref = x
        elif d <= -climb_threshold_m:
            ref = x
    return ascent


# --------------------------------------------------------------------------- #
# Geometry + scoring
# --------------------------------------------------------------------------- #
def _bearing(p1, p2) -> float:
    lat1, lon1 = map(math.radians, p1)
    lat2, lon2 = map(math.radians, p2)
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _haversine_km(p1, p2) -> float:
    R = 6371.0
    lat1, lon1 = map(math.radians, p1)
    lat2, lon2 = map(math.radians, p2)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _destination(lat, lng, bearing_deg, dist_km):
    """Point reached going `dist_km` from (lat, lng) on compass `bearing_deg`."""
    R = 6371.0
    br = math.radians(bearing_deg)
    lat1, lon1 = math.radians(lat), math.radians(lng)
    dr = dist_km / R
    lat2 = math.asin(math.sin(lat1) * math.cos(dr) +
                     math.cos(lat1) * math.sin(dr) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(dr) * math.cos(lat1),
                             math.cos(dr) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def wind_score(coords, into_wind_bearing) -> float:
    """+ means the route rides INTO the wind on the first half and gets a
    tailwind on the way home. Range roughly [-2, +2]."""
    segs = []
    total = 0.0
    for a, b in zip(coords, coords[1:]):
        d = _haversine_km(a, b)
        if d <= 0:
            continue
        # cos(travel bearing - into-wind bearing): +1 = pure headwind, -1 = pure tailwind
        hw = math.cos(math.radians(_bearing(a, b) - into_wind_bearing))
        segs.append((d, hw))
        total += d
    if total <= 0:
        return 0.0
    half = total / 2.0
    cum = 0.0
    first, second = [], []
    for d, hw in segs:
        (first if cum < half else second).append((d, hw))
        cum += d

    def wmean(group):
        s = sum(d for d, _ in group)
        return sum(d * hw for d, hw in group) / s if s > 0 else 0.0

    return wmean(first) - wmean(second)


# Busy-road penalty: a small free band (unavoidable arterial crossings/connectors
# don't get punished) then a steep linear penalty on the State-Road fraction beyond
# it. Weight is high because "keep me off US-12/US-35" is a hard rider preference.
BUSY_FREE_FRAC = 0.05
W_BUSY = 1.5

# Road-ride gravel penalty (linear + convex). `unpaved_frac` upstream counts only
# surface we have evidence for (unknown defaults to paved), so this punishes gravel
# we're sure about. The quadratic term makes the penalty bite ever harder as a route
# gets more gravelly — a mostly-gravel "road" route can't be saved by a great wind
# line. Bump W_ROAD_GRAVEL_QUAD to be even stricter about confirmed gravel.
W_ROAD_GRAVEL_LIN = 1.0
W_ROAD_GRAVEL_QUAD = 1.5

# Separated bike/foot path (multiuse trail) penalty. The rider uses trails as
# CONNECTORS to reach good riding, not as the ride itself — RWGPS trip analysis
# (2026-06-13) showed ~42% of his real mileage is on paths, almost all of it short
# stretches stitching road sections together. So the penalty is on the LONGEST
# CONTIGUOUS path run (path_run_frac), not total path mileage: a long unbroken
# stretch ("riding the trail as the destination", esp. an out-and-back) is what gets
# penalized, while connectors below the free band ride free. Weight stays well below
# W_BUSY so a trail still beats a busy highway but loses to a quiet road.
# PATH_FREE_FRAC is kept only for display thresholds in the CLI.
PATH_FREE_FRAC = 0.05
PATH_RUN_FREE_FRAC = 0.25    # a contiguous path run up to 1/4 of the route = a connector
W_PATH = 0.6                 # bumped from 0.35: now bites only on long runs, so it can
                             # be stronger without punishing the connector use he likes

# On-road bike-lane bonus (OSM-only; ORS waytype can't see lanes tagged on roads).
# A flat-ish reward for riding roads with a dedicated lane — the rider loves these.
# Applies to both ride types. Only nonzero when --surface-source osm|both consulted.
# Bumped 0.4 -> 0.6 after RWGPS trip analysis (2026-06-13): the rider's real rides
# average ~19% on-road bike lane (p90 36%), i.e. he actively seeks them, so the
# bonus was under-rewarding lanes relative to how much he values them.
W_BIKELANE = 0.6

# Tidiness penalty. ORS round_trip sometimes scatters via-points that make a loop
# cross itself (visible tangles / knots). Penalize self-intersections per km beyond
# a small free band so that, among the seeds generated, the cleanest loop wins.
# Retraced shapes (out-and-back / lollipop stem) don't self-cross, so this never
# unfairly hits them. A couple of incidental crossings ride free.
TIDY_FREE_PER_KM = 0.10
W_TIDY = 0.4

# Above this many self-crossings/km a route is a visible tangle; the option selector
# keeps such routes OUT of the alternatives (held in reserve, used only if nothing
# cleaner is distinct enough) so the "variety" fallback can't surface a knot just
# because it's a different shape. Clean loops sit ~0.0-0.04/km, tangled ones ~1/km.
TIDY_OPTION_MAX_PER_KM = 0.25


def evaluate(candidates, wind: Wind, ride_type: str, target_km: float,
             tolerance_km: float = 0.0):
    """Score every candidate and return them sorted best-first.

    `tolerance_km` is a free buffer: a route whose length is within this many km
    of `target_km` gets no distance penalty. Only the distance *beyond* the band
    is penalized, so e.g. a 28-mi loop and a 32-mi loop both count as "on target"
    when you asked for 30 mi +/- 3.

    Routes are also penalized for time spent on arterial "State Road" class
    (US-highways) beyond a small free band, so quiet back-road routes win.
    """
    into = wind.into_wind_bearing
    for c in candidates:
        c.wind_score = wind_score(c.score_coords or c.coords, into)
        wind_norm = (c.wind_score + 2.0) / 4.0           # -> ~0..1

        if ride_type == "gravel":
            c.surface_score = c.unpaved_frac             # seek gravel
            w_wind = 0.4                                  # gravel dominates, wind secondary
            surf_term = 1.0 * c.surface_score            # reward unpaved
        else:
            c.surface_score = c.paved_frac               # avoid gravel
            w_wind = 1.0
            # steep, ramping penalty on KNOWN gravel; small amounts are tolerable,
            # a half-gravel route loses more than the entire wind range can make up.
            surf_term = -(W_ROAD_GRAVEL_LIN * c.unpaved_frac
                          + W_ROAD_GRAVEL_QUAD * c.unpaved_frac ** 2)

        excess = max(0.0, abs(c.distance_km - target_km) - tolerance_km)
        dist_penalty = -excess / max(target_km, 1.0)
        busy_penalty = -max(0.0, c.busy_frac - BUSY_FREE_FRAC)
        # Penalize only the LONGEST contiguous path run beyond the connector band,
        # so trails used to link roads ride free but a long path stretch doesn't.
        path_penalty = -max(0.0, c.path_run_frac - PATH_RUN_FREE_FRAC)
        lane_bonus = c.bikelane_frac                  # 0 unless OSM was consulted
        # Tidiness: count self-crossings on the scored geometry (the loop, for
        # staging) and penalize them per km beyond a small free band, so a tangled
        # round_trip loop loses to a clean one.
        geom = c.score_coords or c.coords
        c.self_intersections = _self_intersections(geom)
        tidy_penalty = -max(0.0, c.self_intersections / max(_polyline_km(geom), 1.0)
                            - TIDY_FREE_PER_KM)
        c.total_score = ((w_wind * wind_norm) + surf_term
                         + (0.5 * dist_penalty) + (W_BUSY * busy_penalty)
                         + (W_PATH * path_penalty) + (W_BIKELANE * lane_bonus)
                         + (W_TIDY * tidy_penalty))

    return sorted(candidates, key=lambda c: c.total_score, reverse=True)


# --------------------------------------------------------------------------- #
# Helpers shared with front-ends
# --------------------------------------------------------------------------- #
def compass_label(deg: float) -> str:
    return COMPASS_16[int((deg % 360) / 22.5 + 0.5) % 16]


def parse_compass(text):
    """A compass direction ('south', 'S', 'SSE', 'south-east') -> bearing degrees.

    Returns None for anything that isn't a direction, so callers can fall back to
    treating the input as a place name. Spaces, hyphens, and case are ignored.
    """
    if not text:
        return None
    squashed = re.sub(r"[\s\-_]+", "", str(text).strip().lower())
    if not squashed:
        return None
    if squashed in _COMPASS_WORDS:
        return _COMPASS_WORDS[squashed]
    if squashed.upper() in COMPASS_16:
        return COMPASS_16.index(squashed.upper()) * 22.5
    return None


def explain(best: Candidate, wind: Wind, ride_type: str) -> str:
    """One-line human rationale for why this route was chosen."""
    bits = [best.shape]
    if best.wind_score > 0.2:
        bits.append(f"heads out into the {compass_label(wind.direction_from_deg)} "
                    f"wind, tailwind home")
    elif best.wind_score < -0.2:
        bits.append("wind line is compromised (no good option for this loop shape today)")
    else:
        bits.append("wind is roughly neutral around the loop")
    if ride_type == "gravel":
        bits.append(f"{best.unpaved_frac * 100:.0f}% unpaved")
    elif best.unpaved_frac < 0.01:
        bits.append("no known gravel")
    else:
        bits.append(f"{best.unpaved_frac * 100:.0f}% known gravel")
    if best.busy_frac <= BUSY_FREE_FRAC:
        bits.append("stays on quiet roads")
    else:
        bits.append(f"{best.busy_frac * 100:.0f}% on busy highways")
    if best.bikelane_frac >= 0.05:
        bits.append(f"{best.bikelane_frac * 100:.0f}% has a bike lane")
    if best.path_frac >= 0.05:
        if best.path_run_frac >= PATH_RUN_FREE_FRAC:
            bits.append(f"{best.path_frac * 100:.0f}% on multiuse path "
                        f"(one long {best.path_run_frac * 100:.0f}% stretch)")
        else:
            bits.append(f"{best.path_frac * 100:.0f}% on multiuse path (connectors)")
    return "; ".join(bits)


# --------------------------------------------------------------------------- #
# Route options: one recommendation + a few genuinely-different alternatives
# --------------------------------------------------------------------------- #
def _route_cells(coords, precision=3):
    """The set of ~100 m grid cells a route's geometry passes through.

    Rounding lat/lng to 3 decimals is ~110 m N-S / ~85 m E-W at this latitude, so
    two routes that ride the SAME roads land in mostly the same cells while two on
    different roads barely overlap - regardless of which direction they head.
    """
    return {(round(lat, precision), round(lng, precision)) for lat, lng in coords}


def _route_overlap(a, b, precision=3):
    """Fraction of the shorter route's road cells shared with the other route.

    ~1.0 means they ride mostly the same roads; ~0.0 means almost entirely
    different roads. This is the "different roads" signal for telling options
    apart - direction plays no part, so two routes that both head south into the
    cornfields on different roads still read as distinct."""
    ca, cb = _route_cells(a), _route_cells(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / min(len(ca), len(cb))


def _options_distinct(a: Candidate, b: Candidate,
                      max_overlap: float = 0.6, min_dist_km: float = 3.0) -> bool:
    """True if two routes are a genuinely different RIDE: a different shape, a
    meaningfully different length (>= ~2 mi), or mostly different roads. Direction
    is deliberately NOT a factor - the rider wants another option in the same good
    country (still south into the cornfields), just a different route."""
    if a.shape != b.shape:
        return True
    if abs(a.distance_km - b.distance_km) >= min_dist_km:
        return True
    return _route_overlap(a.coords, b.coords) <= max_overlap


def _messy_per_km(c: Candidate) -> float:
    """Self-crossings per km on a candidate's scored geometry (the tangle signal)."""
    geom = c.score_coords or c.coords
    return c.self_intersections / max(_polyline_km(geom), 1.0)


def _route_difference(a: Candidate, b: Candidate) -> float:
    """How different two routes ride, ~0 (same) .. ~1.6 (very). Combines different
    roads (1 - overlap), a shape change, and a length gap. Used to pick the most
    different leftover route when no benefit axis yields an alternative."""
    diff = 1.0 - _route_overlap(a.coords, b.coords)
    if a.shape != b.shape:
        diff += 0.3
    diff += min(0.3, abs(a.distance_km - b.distance_km) / 16.0)   # ~10 mi -> +0.3
    return diff


def _option_reasons(c: Candidate, wind: Wind, ride_type: str, lead: str = None):
    """Human bullet points for an option: the axis it leads on first, then a few
    short supporting facts (skipping whichever axis we just led with). Pure text."""
    cl = compass_label(wind.direction_from_deg)
    reasons = []
    if lead == "wind":
        reasons.append(f"strongest wind line - out into the {cl} wind, tailwind home "
                       f"(wind score {c.wind_score:+.2f})")
    elif lead == "quiet":
        reasons.append("quietest - least time on busy highways / long path runs")
    elif lead == "lanes":
        reasons.append(f"most on-road bike lane ({c.bikelane_frac * 100:.0f}%)")
    elif lead == "distance":
        reasons.append(f"closest to the distance you asked for ({c.distance_km:.1f} km)")
    elif lead == "variety":
        reasons.append(f"a different option - a {c.shape}, {c.distance_km:.1f} km")
    else:                                       # the recommendation: lead with the wind verdict
        if c.wind_score > 0.2:
            reasons.append(f"rides into the {cl} wind first, tailwind home")
        elif c.wind_score < -0.2:
            reasons.append("best available, though the wind line is compromised today")
        else:
            reasons.append("balanced pick; wind is roughly neutral around the loop")

    if lead != "distance":
        reasons.append(f"{c.distance_km:.1f} km, +{c.ascent_m:.0f} m")
    if ride_type == "gravel":
        reasons.append(f"{c.unpaved_frac * 100:.0f}% unpaved")
    elif c.unpaved_frac >= 0.01 and lead != "quiet":
        reasons.append(f"{c.unpaved_frac * 100:.0f}% known gravel")
    if lead != "quiet" and c.busy_frac > BUSY_FREE_FRAC:
        reasons.append(f"{c.busy_frac * 100:.0f}% on busy highways")
    if lead != "lanes" and c.bikelane_frac >= 0.05:
        reasons.append(f"{c.bikelane_frac * 100:.0f}% bike lane")
    if c.path_frac >= 0.05:
        if c.path_run_frac >= PATH_RUN_FREE_FRAC:
            reasons.append(f"{c.path_frac * 100:.0f}% path (one long "
                           f"{c.path_run_frac * 100:.0f}% stretch)")
        else:
            reasons.append(f"{c.path_frac * 100:.0f}% path (connectors)")
    return reasons


# Per-axis "unique benefit" specs an alternative can lead on, in rider-priority
# order. Each: (key, headline, value fn where higher == better on that axis,
# margin the candidate must beat the recommended route by for the benefit to be
# real). Ordered from the findings: wind premise first, then quiet roads, then
# bike lanes, with distance as a practical fallback. `lanes` is added only when
# OSM lane data was actually consulted (else bikelane_frac is uniformly 0).
def _option_axes(have_lane: bool, target_km: float):
    axes = [
        ("wind", "Stronger wind line", lambda c: c.wind_score, 0.15),
        ("quiet", "Quieter roads",
         lambda c: -(c.busy_frac + 0.5 * max(0.0, c.path_run_frac - PATH_RUN_FREE_FRAC)),
         0.05),
    ]
    if have_lane:
        axes.append(("lanes", "More bike lanes", lambda c: c.bikelane_frac, 0.10))
    axes.append(("distance", "Closer to your distance",
                 lambda c: -abs(c.distance_km - target_km), 0.8))
    return axes


def select_route_options(ranked, wind: Wind, ride_type: str, target_km: float,
                         n_alternatives: int = 2,
                         max_overlap: float = 0.6, min_dist_km: float = 3.0):
    """From scored candidates (best-first, as `evaluate` returns) pick the top
    recommendation plus up to `n_alternatives` GENUINELY DIFFERENT routes, each
    leading on a distinct benefit the rider cares about.

    Variety with a reason: rather than handing back the 2nd/3rd-best loops (often
    near-clones of the winner), we look for the best route that beats the pick on
    a *different* axis - a stronger wind line, quieter roads, more bike lane, a
    closer distance - and is a different RIDE (a different shape, a couple miles
    longer/shorter, or mostly different roads; NOT a different direction - same
    good country is fine). Axes with no real standout are skipped; any leftover
    slots fall back to "most different roads remaining" so you still get options
    on a thin field. Pure: reads candidate fields, never refetches. Returns a list
    of `RouteOption`, the recommendation first.
    """
    if not ranked:
        return []
    primary = ranked[0]
    options = [RouteOption(primary, "recommended", "Top pick",
                           _option_reasons(primary, wind, ride_type))]
    # Hold tangled routes in reserve so they never get surfaced as an alternative
    # just for being a different shape; they're drawn on only if nothing cleaner is
    # distinct enough to fill a slot.
    pool = [c for c in ranked[1:] if _messy_per_km(c) <= TIDY_OPTION_MAX_PER_KM]
    reserve = [c for c in ranked[1:] if _messy_per_km(c) > TIDY_OPTION_MAX_PER_KM]
    chosen: list = []

    have_lane = any(c.bikelane_frac > 0 for c in ranked)
    for key, headline, val, margin in _option_axes(have_lane, target_km):
        if len(chosen) >= n_alternatives:
            break
        base = val(primary)
        best = None
        for c in pool:
            if val(c) - base < margin:
                continue                        # not a real win on this axis
            if not _options_distinct(primary, c, max_overlap, min_dist_km):
                continue                        # too similar to the recommendation
            if any(not _options_distinct(o, c, max_overlap, min_dist_km) for o in chosen):
                continue                        # too similar to an already-picked alt
            if best is None or val(c) > val(best):
                best = c
        if best is not None:
            pool.remove(best)
            chosen.append(best)
            options.append(RouteOption(best, "alternative", headline,
                                       _option_reasons(best, wind, ride_type, lead=key)))

    # Fill any remaining slots with the most DIFFERENT leftover routes (variety
    # for its own sake), so the rider still gets choices even when nothing clearly
    # beats the pick on a benefit axis. "Most different" = different roads/shape/
    # length from the nearest already-chosen route, not a different direction.
    # Exhaust the clean pool first; only dip into the tangled reserve if we still
    # can't fill the slots.
    while len(chosen) < n_alternatives and (pool or reserve):
        src = pool if pool else reserve
        ref = [primary] + chosen
        cand = max(src, key=lambda c: min(_route_difference(c, r) for r in ref))
        src.remove(cand)
        chosen.append(cand)
        # Name the headline after what actually sets it apart from the pick.
        if cand.shape != primary.shape:
            headline = f"Different shape ({cand.shape})"
        elif cand.distance_km - primary.distance_km >= min_dist_km:
            headline = "A bit longer"
        elif primary.distance_km - cand.distance_km >= min_dist_km:
            headline = "A bit shorter"
        else:
            headline = "Different roads"
        options.append(RouteOption(cand, "alternative", headline,
                                   _option_reasons(cand, wind, ride_type, lead="variety")))
    return options
