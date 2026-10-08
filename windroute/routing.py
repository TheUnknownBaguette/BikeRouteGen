"""Route generation: ORS directions, geometric shapes, generation + refinement.

Network I/O for routing lives here; pure-geometry helpers are in `geometry`,
scoring is in `scoring`.
"""
from __future__ import annotations

import concurrent.futures
import math
import threading
import time

import requests

from .geometry import _bearing, _destination, _haversine_km, _polyline_km
from .models import Candidate

# Observability (CODE_HEALTH Task C2): a process-lifetime tally of ORS directions
# calls. The process total is exact (one lock) and is the useful signal for the
# ~2000/day free-tier cap the README warns about; planner snapshots it before/after
# a plan for a best-effort per-plan count (the delta can include other plans' calls
# under concurrency, but the total is always exact). 429 back-off retries are not
# counted separately — this tracks logical routing calls (the README's "~12-15").
_ORS_CALLS = 0
_ORS_CALLS_LOCK = threading.Lock()


def ors_call_total() -> int:
    """Total ORS directions calls this process has made (thread-safe read)."""
    return _ORS_CALLS


def _count_ors_call():
    global _ORS_CALLS
    with _ORS_CALLS_LOCK:
        _ORS_CALLS += 1


# HeiGIT moved the ORS API from api.openrouteservice.org to api.heigit.org/openrouteservice
# (Oct 2026). The old host now answers every request with 403 "Quota exceeded", even
# with quota left — if that reappears with a full dashboard, check for another move.
ORS_URL = "https://api.heigit.org/openrouteservice/v2/directions/{profile}/geojson"


class OrsAccessError(RuntimeError):
    """OpenRouteService refused the API key itself (quota used up, or a bad key).

    Unlike a per-route failure (an unroutable waypoint, which just drops that
    candidate), every further call would fail the same way, so generation stops
    and the rider is told what actually happened instead of "no routes came back".
    It's a RuntimeError, so the CLI and web app already show its message.
    """


ORS_QUOTA_MSG = ("The routing service's daily limit has been used up "
                 "(OpenRouteService free plan: 2,000 route requests a day, and each plan "
                 "uses about 12-15). It resets within 24 hours - try again later.")
ORS_KEY_MSG = ("The routing service rejected the API key. Check that ORS_API_KEY is set "
               "to a valid OpenRouteService key.")


def _check_ors_access(resp):
    """Raise OrsAccessError when ORS rejected the key rather than the route."""
    if resp.status_code not in (401, 403):
        return
    try:
        text = str(resp.json().get("error", ""))
    except ValueError:
        text = resp.text or ""
    if "quota" in text.lower():
        raise OrsAccessError(ORS_QUOTA_MSG)
    raise OrsAccessError(ORS_KEY_MSG)

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

# ORS "suitability" extra: a 0-10 bike rating per stretch (10 = best). Calibrated on
# real plans (2026-10-08, Mokena / Oak Park / Champaign): ordinary streets are 8,
# paths 9, rural county roads 7 (good riding, keep), and the big arterials 5
# (Cermak, Roosevelt, Laraway, Duncan, US 52/150 ...). Nothing scored below 5.
SUIT_POOR_MAX = 5


# --------------------------------------------------------------------------- #
# Route generation (OpenRouteService, needs a free API key)
# --------------------------------------------------------------------------- #
SHAPES = ("loop", "out-and-back", "lollipop", "rectangle", "staging")

# Polygon-loop variety per seed: cycle vertex counts and travel orientation so a
# handful of "loop" seeds explore different road sets / wind lines, not clones.
_LOOP_SIDES = (5, 4, 6, 5, 4, 6)

# Angular offsets (deg) tried around the aiming bearing for directional shapes,
# nearest-first so the most wind-aligned options get generated when n is small.
_BEARING_OFFSETS = [0, 30, -30, 60, -60, 90, -90, 135, -135, 180]


def _strip_backtracks(coords, eles=None, tol_m=5.0, names=None):
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
    in lockstep so the two stay aligned, and so is `names` when given (then the
    return is (coords, eles, names)). Returns (coords, eles).
    """
    keep = []
    for i, p in enumerate(coords):
        if len(keep) >= 2 and _haversine_km(coords[keep[-2]], p) * 1000.0 <= tol_m:
            keep.pop()                    # the last kept point was a dead-end tip
            continue                      # p coincides with keep[-2], already present
        if keep and _haversine_km(coords[keep[-1]], p) * 1000.0 <= 0.5:
            continue                      # drop only exact-duplicate points
        keep.append(i)
    if len(keep) == len(coords):          # nothing to do; keep the originals
        return (coords, eles) if names is None else (coords, eles, names)
    new_coords = [coords[i] for i in keep]
    new_eles = ([eles[i] for i in keep]
                if eles and len(eles) == len(coords) else eles)
    if names is None:
        return new_coords, new_eles
    new_names = ([names[i] for i in keep]
                 if names and len(names) == len(coords) else names)
    return new_coords, new_eles, new_names


def _ors_directions(api_key, profile, coordinates, timeout):
    """One ORS directions call over an explicit list of through-waypoints.

    Returns (coords, eles, dist_km, paved, unpaved, busy, path, path_run_km, names,
    poor).
    `coordinates` is ORS-order [[lng, lat], ...]. All route shapes are built from
    explicit geometric waypoints (see the _make_* builders), so no ORS round_trip
    is used. `busy` is the fraction of distance on arterial "State Road" class
    (US-highways); `path` is the fraction on separated bike/foot paths (multiuse
    trails); `path_run_km` is the longest *contiguous* path stretch (km) on this leg;
    `names` is the road name for the stretch leaving each point (from ORS's turn-by-
    turn steps), which `cues.make_cues` turns into a cue sheet; `poor` is the fraction
    ORS rates poor for bikes that ISN'T already a busy State Road (see `_poor_fraction`).
    """
    url = ORS_URL.format(profile=profile)
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {
        "coordinates": coordinates,
        "extra_info": ["surface", "waytype", "suitability"],
        "elevation": True,
        "instructions": True,             # only for the road names (cue sheet)
    }

    _count_ors_call()
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code == 429:                  # rate limited — back off once
        time.sleep(2.5)
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    _check_ors_access(resp)                      # quota used up / bad key: stop, say so
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
    names = _step_names(props, len(coords))
    poor = _poor_fraction(extras, coords)
    # Drop the little A->B->A spurs ORS sometimes emits; subtract their mileage
    # from the ORS road distance so the reported length matches the cleaned line.
    clean, eles, names = _strip_backtracks(coords, eles, names=names)
    if len(clean) != len(coords):
        dist_km = max(0.0, dist_km - (_polyline_km(coords) - _polyline_km(clean)))
        coords = clean
    return coords, eles, dist_km, paved, unpaved, busy, path, path_run_km, names, poor


def _step_names(props, n):
    """Road name per geometry point from ORS steps (`way_points` = [from, to] indices).

    names[i] names the stretch from point i to i+1; unnamed ways are "". The last
    point repeats the name before it.
    """
    names = [""] * n
    for seg in props.get("segments", []) or []:
        for st in seg.get("steps", []) or []:
            wp = st.get("way_points") or []
            if len(wp) != 2:
                continue
            name = (st.get("name") or "").strip()
            name = "" if name == "-" else name
            for i in range(max(0, wp[0]), min(n, wp[1])):
                names[i] = name
    if n >= 2:
        names[-1] = names[-2]
    return names


def _reversed_names(names):
    """Names for a leg ridden backwards, aligned with coords[::-1]: the stretch from
    point k back to k-1 is names[k-1]. Its first entry (the leg's far end) is the
    name leaving the turnaround, so builders splice it whole after the outbound part."""
    if not names:
        return []
    return [names[k - 1] for k in range(len(names) - 1, 0, -1)] + [names[0]]


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
    coords, eles, dist, paved, unpaved, busy, path, path_run, names, poor = _ors_directions(
        api_key, profile, pts, timeout)
    return Candidate(coords=coords, distance_km=dist,
                     ascent_m=_smoothed_ascent(eles) if eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy, poor_road_frac=poor,
                     path_frac=path, path_run_frac=(path_run / dist if dist else 0.0),
                     shape="loop", eles=eles or None, waypoints=list(verts),
                     road_names=names)


def _make_out_back(api_key, profile, lat, lng, target_km, bearing, timeout, detour=1.3):
    """Route to a point ~target/2 away on `bearing`, then mirror the path home."""
    crow_km = (target_km / 2.0) / detour                         # roads aren't straight
    dlat, dlng = _destination(lat, lng, bearing, crow_km)
    coords, eles, dist, paved, unpaved, busy, path, path_run, names, poor = _ors_directions(
        api_key, profile, [[lng, lat], [dlng, dlat]], timeout)
    full_coords = coords + coords[-2::-1]                        # out + reversed (no dup turn)
    full_eles = (eles + eles[-2::-1]) if eles else []
    full_names = names[:-1] + _reversed_names(names) if names else None
    # The leg's path stretch is ridden both ways, so an out-and-back *on* a trail
    # has run_frac ~ path_run/dist (≈1.0 if the whole leg is path) — exactly the
    # "riding the path as the destination" case this should flag.
    return Candidate(coords=full_coords, distance_km=dist * 2.0,
                     ascent_m=_smoothed_ascent(full_eles) if full_eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy, poor_road_frac=poor,
                     path_frac=path, path_run_frac=(path_run / dist if dist else 0.0),
                     shape="out-and-back", eles=full_eles or None, road_names=full_names)


def _make_lollipop(api_key, profile, lat, lng, target_km, bearing, seed,
                   timeout, detour=1.3, loop_frac=0.35,
                   loop_sides=_LOOP_SIDES, loop_detour=1.25):
    """Out-and-back stem with a clean geometric 'candy' loop at the far end.

    The candy is a polygon loop (like the default "loop" shape), NOT an ORS
    round_trip, so it can't tangle or spur. It's anchored at the stem's actual
    routed endpoint (a real road node) rather than the crow-flies target, so the
    far waypoint is always routable and the stem<->candy seam has no stub. Sides
    and travel direction vary by `seed` for variety; the candy bulges further out
    along `bearing` (continuing away from home). `loop_sides`/`loop_detour` are the
    archetype loop geometry (default = grid-farmland)."""
    loop_km = max(5.0, target_km * loop_frac)
    stem_oneway = max(1.0, (target_km - loop_km) / 2.0)
    crow_km = stem_oneway / detour
    dlat, dlng = _destination(lat, lng, bearing, crow_km)

    s_coords, s_eles, s_dist, s_pav, s_unp, s_busy, s_path, s_run, s_names, s_poor = _ors_directions(
        api_key, profile, [[lng, lat], [dlng, dlat]], timeout)

    # Anchor the candy at the stem's real end node, and route it as a polygon loop.
    glat, glng = s_coords[-1]
    verts = _polygon_loop_waypoints(
        glat, glng, loop_km, bearing,
        n_sides=loop_sides[seed % len(loop_sides)],
        orient=(1 if (seed // len(loop_sides)) % 2 == 0 else -1), detour=loop_detour)
    l_coords, l_eles, l_dist, l_pav, l_unp, l_busy, l_path, l_run, l_names, l_poor = _ors_directions(
        api_key, profile, [[vlng, vlat] for vlat, vlng in verts], timeout)

    full_coords = s_coords + l_coords[1:] + s_coords[-2::-1]     # stem + candy + stem back
    full_eles = (s_eles + l_eles[1:] + s_eles[-2::-1]) if (s_eles and l_eles) else []
    full_names = (s_names[:-1] + l_names[:-1] + _reversed_names(s_names)
                  if (s_names and l_names) else None)
    total_dist = s_dist * 2.0 + l_dist

    stem_w, loop_w = 2.0 * s_dist, l_dist                        # distance-weighted blend
    tot = stem_w + loop_w
    paved = (s_pav * stem_w + l_pav * loop_w) / tot if tot else 1.0
    unpaved = (s_unp * stem_w + l_unp * loop_w) / tot if tot else 0.0
    busy = (s_busy * stem_w + l_busy * loop_w) / tot if tot else 0.0
    poor = (s_poor * stem_w + l_poor * loop_w) / tot if tot else 0.0
    path = (s_path * stem_w + l_path * loop_w) / tot if tot else 0.0
    path_run = max(s_run, l_run) / total_dist if total_dist else 0.0   # longest single run
    return Candidate(coords=full_coords, distance_km=total_dist,
                     ascent_m=_smoothed_ascent(full_eles) if full_eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy, poor_road_frac=poor,
                     path_frac=path, path_run_frac=path_run, shape="lollipop",
                     eles=full_eles or None, road_names=full_names)


def _make_staging(api_key, profile, lat, lng, target_km, zone, seed,
                  timeout, min_loop_km=8.0, detour=1.3,
                  loop_sides=_LOOP_SIDES, loop_detour=1.25):
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
    n_sides = loop_sides[seed % len(loop_sides)]
    orient = 1 if (seed // len(loop_sides)) % 2 == 0 else -1
    radius = loop_km / (loop_detour * 2.0 * n_sides * math.sin(math.pi / n_sides))
    stem_crow = max(0.5, crow - radius)
    tlat, tlng = _destination(lat, lng, bearing, stem_crow)      # stem target (near zone edge)

    s_coords, s_eles, s_dist, s_pav, s_unp, s_busy, s_path, s_run, s_names, s_poor = _ors_directions(
        api_key, profile, [[lng, lat], [tlng, tlat]], timeout)

    # Anchor the loop at the stem's real end node and bulge it toward the zone.
    glat, glng = s_coords[-1]
    verts = _polygon_loop_waypoints(glat, glng, loop_km, bearing, n_sides, orient, loop_detour)
    l_coords, l_eles, l_dist, l_pav, l_unp, l_busy, l_path, l_run, l_names, l_poor = _ors_directions(
        api_key, profile, [[vlng, vlat] for vlat, vlng in verts], timeout)

    full_coords = s_coords + l_coords[1:] + s_coords[-2::-1]     # stem + loop + stem back
    full_eles = (s_eles + l_eles[1:] + s_eles[-2::-1]) if (s_eles and l_eles) else []
    full_names = (s_names[:-1] + l_names[:-1] + _reversed_names(s_names)
                  if (s_names and l_names) else None)
    total_dist = s_dist * 2.0 + l_dist

    stem_w, loop_w = 2.0 * s_dist, l_dist                        # distance-weighted blend
    tot = stem_w + loop_w
    paved = (s_pav * stem_w + l_pav * loop_w) / tot if tot else 1.0
    unpaved = (s_unp * stem_w + l_unp * loop_w) / tot if tot else 0.0
    busy = (s_busy * stem_w + l_busy * loop_w) / tot if tot else 0.0
    poor = (s_poor * stem_w + l_poor * loop_w) / tot if tot else 0.0
    path = (s_path * stem_w + l_path * loop_w) / tot if tot else 0.0
    path_run = max(s_run, l_run) / total_dist if total_dist else 0.0   # longest single run
    return Candidate(coords=full_coords, distance_km=total_dist,
                     ascent_m=_smoothed_ascent(full_eles) if full_eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy, poor_road_frac=poor,
                     path_frac=path, path_run_frac=path_run, shape="staging",
                     eles=full_eles or None, score_coords=l_coords, road_names=full_names)


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

    coords, eles, dist, paved, unpaved, busy, path, path_run, names, poor = _ors_directions(
        api_key, profile, pts, timeout)
    verts = [(lat, lng), (a_lat, a_lng), (b_lat, b_lng), (c_lat, c_lng), (lat, lng)]
    return Candidate(coords=coords, distance_km=dist,
                     ascent_m=_smoothed_ascent(eles) if eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy, poor_road_frac=poor,
                     path_run_frac=(path_run / dist if dist else 0.0),
                     path_frac=path, shape="rectangle", eles=eles or None, waypoints=verts,
                     road_names=names)


def _candidate_from_waypoints(api_key, profile, waypoints, shape, timeout):
    """Route a through-path over `waypoints` ((lat,lng) corners) -> a Candidate.

    The general form of the geometric builders, used by `refine_candidate` to rebuild
    a loop/rectangle after nudging a corner. Carries the waypoints so the refined
    route can be nudged again.
    """
    pts = [[lng, lat] for lat, lng in waypoints]                    # -> ORS [lng, lat]
    coords, eles, dist, paved, unpaved, busy, path, path_run, names, poor = _ors_directions(
        api_key, profile, pts, timeout)
    return Candidate(coords=coords, distance_km=dist,
                     ascent_m=_smoothed_ascent(eles) if eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy, poor_road_frac=poor,
                     path_frac=path, path_run_frac=(path_run / dist if dist else 0.0),
                     shape=shape, eles=eles or None, waypoints=list(waypoints),
                     road_names=names)


def refine_candidate(cand, api_key, profile, target_km, tolerance_km, score_fn,
                     timeout=40, step_km=0.4, max_calls=6):
    """Local-search refine a waypoint-built candidate (work-plan Task 6).

    Hill-climb: nudge each interior corner a small step in the cardinal directions,
    re-route the whole loop through ORS, and KEEP the move only if it raises the
    full-objective score (`score_fn(candidate) -> total_score`, supplied by the
    caller so the non-additive surface/wind/quiet objective is honored per move)
    AND the length stays within tolerance of target. First-improvement, capped at
    `max_calls` ORS calls so the free-tier budget stays bounded.

    The seed's existing `total_score` is the baseline (we never re-score it, so the
    caller's one-time overlays — corrections etc. — aren't double-applied). Returns
    (best_candidate, ors_calls_used); `best is cand` when nothing beat the seed.
    """
    if not cand.waypoints or len(cand.waypoints) < 4 or max_calls <= 0:
        return cand, 0
    best = cand
    best_score = cand.total_score
    # Hold length: never drift further from target than the seed already is (or the
    # free tolerance band, whichever is larger) — a great wind line that's way too
    # long is not a win.
    allowed_dev = max(tolerance_km, abs(cand.distance_km - target_km))
    calls = 0
    improved = True
    while improved and calls < max_calls:
        improved = False
        for k in range(1, len(best.waypoints) - 1):        # interior corners only
            for brg in (0.0, 90.0, 180.0, 270.0):
                if calls >= max_calls:
                    break
                wp = list(best.waypoints)
                wp[k] = _destination(wp[k][0], wp[k][1], brg, step_km)
                try:
                    cand2 = _candidate_from_waypoints(api_key, profile, wp,
                                                      best.shape, timeout)
                except requests.HTTPError:
                    calls += 1
                    continue
                except OrsAccessError:                    # quota ran out mid-refine:
                    return best, calls + 1                # keep what we have
                calls += 1
                if abs(cand2.distance_km - target_km) > allowed_dev:
                    continue
                if score_fn(cand2) > best_score:
                    best, best_score = cand2, cand2.total_score
                    improved = True
                    break                                  # first-improvement: restart
            if improved:
                break
    return best, calls


ORS_MAX_WORKERS = 6   # candidate ORS calls in flight at once (free tier ~40 req/min)


def generate_candidates(lat, lng, target_km, ride_type, api_key,
                        n=8, timeout=40, sleep=0.4,
                        shapes=("loop",), into_wind_bearing=None, zone=None,
                        loop_geom=None, workers=ORS_MAX_WORKERS):
    """Generate `n` candidate routes of ~target_km from (lat, lng).

    `shapes` chooses which route forms to produce ("loop", "lollipop", "rectangle",
    "out-and-back"); `n` is split across them. Directional shapes (out-and-back,
    lollipop) are aimed at `into_wind_bearing` first (so you ride out into the
    wind, home with a tailwind) with widening offsets for variety; everything is
    still scored by `evaluate` afterward. ORS caps loop length at 100 km.

    `zone` (a dict with 'lat'/'lng' from zones.find_ride_zone) enables the
    "staging" shape: transit to that quiet ride zone, loop there scored on the
    wind, transit home. The staging shape is only produced when a zone is given.

    `loop_geom` is an optional (loop_sides_tuple, detour) pair from
    `loop_geom_for(archetype)` controlling the polygon-loop shape (more sides + a
    bigger detour for curvy terrain). None -> today's grid-farmland geometry.

    `workers` bounds how many candidate ORS calls run concurrently (each candidate
    is an independent round-trip). The default turns ~12 sequential calls into a few
    batches — the bulk of the per-plan latency — while staying inside the free-tier
    burst; the per-call 429 back-off in `_ors_directions` still applies. `workers=1`
    reproduces the old fully-serial behavior for debugging. `sleep` is retained for
    backward compatibility but is no longer used (concurrency replaces the manual
    inter-call pacing).
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
    loop_sides, loop_detour = loop_geom or (_LOOP_SIDES, 1.25)

    # Assign each plan entry its per-shape seed index up front, exactly as the serial
    # version did, so the concurrent builds are deterministic and the result order
    # doesn't depend on which future finishes first.
    seeds = {s: 0 for s in SHAPES}
    specs = []                                    # [(shape, idx), ...] in plan order
    for shape in plan:
        specs.append((shape, seeds[shape]))
        seeds[shape] += 1

    def _build(shape, idx):
        bearing = (center + _BEARING_OFFSETS[idx % len(_BEARING_OFFSETS)]) % 360
        if shape == "loop":
            # Clean geometric polygon loop; vary sides + travel direction by seed.
            return _make_polygon_loop(
                api_key, profile, lat, lng, target_km, bearing, timeout,
                n_sides=loop_sides[idx % len(loop_sides)],
                orient=(1 if (idx // len(loop_sides)) % 2 == 0 else -1),
                detour=loop_detour)
        if shape == "out-and-back":
            return _make_out_back(api_key, profile, lat, lng, target_km, bearing, timeout)
        if shape == "rectangle":
            return _make_rectangle(api_key, profile, lat, lng, target_km, bearing, timeout,
                                   cross_sign=(1 if idx % 2 == 0 else -1))
        if shape == "staging":
            return _make_staging(api_key, profile, lat, lng, target_km, zone,
                                 idx, timeout, loop_sides=loop_sides,
                                 loop_detour=loop_detour)
        return _make_lollipop(api_key, profile, lat, lng, target_km, bearing,
                              idx, timeout, loop_sides=loop_sides,
                              loop_detour=loop_detour)

    # Generate concurrently — each candidate is an independent ORS round-trip, so a
    # bounded thread pool collapses ~12 sequential calls into a few batches (within
    # the free tier's burst). Results are slotted back into plan order so the route
    # set AND its tie-break ordering are identical to the serial path; a seed that
    # 404s/HTTPErrors, times out or drops its connection is skipped, keeping the rest.
    # `workers=1` == fully serial.
    slots = [None] * len(specs)
    pool = max(1, min(workers, len(specs))) if specs else 1
    access_error = None
    rate_limited = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=pool) as ex:
        futures = {ex.submit(_build, shape, idx): pos
                   for pos, (shape, idx) in enumerate(specs)}
        for fut in concurrent.futures.as_completed(futures):
            if fut.cancelled():                   # skipped after a refused key
                continue
            try:
                slots[futures[fut]] = fut.result()
            except requests.RequestException as exc:   # bad seed, timeout, dropped
                resp = getattr(exc, "response", None)     # connection: skip, keep the rest
                rate_limited = rate_limited or (resp is not None and resp.status_code == 429)
            except OrsAccessError as exc:         # the key itself was refused: every
                access_error = access_error or exc     # other call will fail too
                for other in futures:
                    other.cancel()                # don't fire the ones not yet started
    out = [c for c in slots if c is not None]
    if access_error is not None and not out:
        raise access_error

    if not out and rate_limited:
        raise RuntimeError("The routing service is getting too many requests right now "
                           "(OpenRouteService rate limit). Wait a minute and try again.")
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


def _segment_values(extra, nseg):
    """Per-segment value from a positional ORS extra ([start, end, value] over coords),
    or None when the extra is missing."""
    if not extra or not extra.get("values"):
        return None
    out = [None] * nseg
    for entry in extra["values"]:
        try:
            s, e, v = int(entry[0]), int(entry[1]), int(entry[2])
        except (TypeError, ValueError, IndexError):
            continue
        for i in range(max(0, s), min(nseg, e)):
            out[i] = v
    return out


def _poor_fraction(extras, coords):
    """Fraction of distance ORS rates poor for bikes (suitability <= SUIT_POOR_MAX)
    on roads that are NOT busy State Roads.

    Those State Roads are already in `busy_frac`, so this is the extra signal:
    arterials the waytype class misses (county highways, city arterials). Counting
    only them keeps a US highway from being penalized twice. 0.0 with no data.
    """
    nseg = len(coords) - 1
    if nseg < 1:
        return 0.0
    suit = _segment_values(extras.get("suitability"), nseg)
    if suit is None:
        return 0.0
    wt = _segment_values(extras.get("waytype"), nseg) or [None] * nseg
    total = poor = 0.0
    for i in range(nseg):
        d = _haversine_km(coords[i], coords[i + 1])
        total += d
        if suit[i] is not None and suit[i] <= SUIT_POOR_MAX and wt[i] not in BUSY_WAYTYPES:
            poor += d
    return poor / total if total > 0 else 0.0


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
