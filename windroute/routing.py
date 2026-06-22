"""Route generation: ORS directions, geometric shapes, generation + refinement.

Network I/O for routing lives here; pure-geometry helpers are in `geometry`,
scoring is in `scoring`.
"""
from __future__ import annotations

import concurrent.futures
import math
import time

import requests

from . import valhalla
from .geometry import _bearing, _destination, _haversine_km, _polyline_km, _thin
from .models import Candidate


ORS_URL = "https://api.openrouteservice.org/v2/directions/{profile}/geojson"

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


# --------------------------------------------------------------------------- #
# Route generation (OpenRouteService, needs a free API key)
# --------------------------------------------------------------------------- #
SHAPES = ("loop", "out-and-back", "lollipop", "rectangle", "staging", "roundtrip", "wind")

# Polygon-loop variety per seed: cycle vertex counts and travel orientation so a
# handful of "loop" seeds explore different road sets / wind lines, not clones.
_LOOP_SIDES = (5, 4, 6, 5, 4, 6)

# Angular offsets (deg) tried around the aiming bearing for directional shapes,
# nearest-first so the most wind-aligned options get generated when n is small.
_BEARING_OFFSETS = [0, 30, -30, 60, -60, 90, -90, 135, -135, 180]


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


def _ors_directions(api_key, profile, coordinates, timeout, round_trip=None,
                    avoid_polygons=None):
    """One ORS directions call.

    Returns (coords, eles, dist_km, paved, unpaved, busy, path, path_run_km).
    `coordinates` is ORS-order [[lng, lat], ...]; pass `round_trip` dict for loops.
    `avoid_polygons` is a GeoJSON (Multi)Polygon ORS routes AROUND — used (Task 7)
    to push the return leg of a wind loop off the outbound roads.
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
    options = {}
    if round_trip is not None:
        options["round_trip"] = round_trip
    if avoid_polygons is not None:
        options["avoid_polygons"] = avoid_polygons
    if options:
        body["options"] = options

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
                     shape="loop", waypoints=list(verts))


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

    s_coords, s_eles, s_dist, s_pav, s_unp, s_busy, s_path, s_run = _ors_directions(
        api_key, profile, [[lng, lat], [dlng, dlat]], timeout)

    # Anchor the candy at the stem's real end node, and route it as a polygon loop.
    glat, glng = s_coords[-1]
    verts = _polygon_loop_waypoints(
        glat, glng, loop_km, bearing,
        n_sides=loop_sides[seed % len(loop_sides)],
        orient=(1 if (seed // len(loop_sides)) % 2 == 0 else -1), detour=loop_detour)
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
    verts = [(lat, lng), (a_lat, a_lng), (b_lat, b_lng), (c_lat, c_lng), (lat, lng)]
    return Candidate(coords=coords, distance_km=dist,
                     ascent_m=_smoothed_ascent(eles) if eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_run_frac=(path_run / dist if dist else 0.0),
                     path_frac=path, shape="rectangle", waypoints=verts)


def _corridor_multipolygon(coords, buffer_m=350.0, clearance_m=600.0, max_boxes=30):
    """A GeoJSON MultiPolygon of small squares along `coords`, for ORS avoid_polygons.

    Samples the polyline ~every `buffer_m` and drops a square (half-side `buffer_m`) at
    each, SKIPPING samples within `clearance_m` of the first/last point so the return
    leg's endpoints (start + turnaround) aren't trapped inside an avoid zone (which
    would make ORS return "no routable point"). Disjoint squares dodge the
    self-intersection a buffered ribbon can hit on a curvy line. None if no usable
    samples (e.g. a short/curled leg) — the caller then just routes home normally.
    """
    if len(coords) < 2:
        return None
    start, end = coords[0], coords[-1]
    step_km = max(0.1, buffer_m / 1000.0)
    clr_km = clearance_m / 1000.0
    samples, acc, prev = [], step_km, coords[0]
    for p in coords:
        acc += _haversine_km(prev, p)
        prev = p
        if acc >= step_km:
            acc = 0.0
            if _haversine_km(p, start) >= clr_km and _haversine_km(p, end) >= clr_km:
                samples.append(p)
    if not samples:
        return None
    if len(samples) > max_boxes:
        samples = samples[::(len(samples) // max_boxes) + 1]
    polys = []
    for la, lo in samples:
        dlat = buffer_m / 111320.0
        dlng = buffer_m / (111320.0 * max(0.1, math.cos(math.radians(la))))
        ring = [[lo - dlng, la - dlat], [lo + dlng, la - dlat],
                [lo + dlng, la + dlat], [lo - dlng, la + dlat], [lo - dlng, la - dlat]]
        polys.append([ring])
    return {"type": "MultiPolygon", "coordinates": polys}


def _make_wind_loop(api_key, profile, lat, lng, target_km, into_wind_bearing, timeout,
                    seed=0, detour=1.35, buffer_m=350.0):
    """Headwind-out / tailwind-home loop on DIFFERENT roads each way (Task 7 stopgap).

    Ride out to a turnaround into the wind (~half the ride), then route home AVOIDING
    the outbound corridor (`avoid_polygons`) so the tailwind return takes different
    roads — the strategy the owner rides by hand. Small per-seed jitter on aim +
    turnaround distance gives variety across seeds. If a Valhalla wind router is
    configured (experimental, off by default) the OUTBOUND corridor comes from it,
    re-traced through ORS for surface/waytype extras; otherwise plain ORS. If the
    avoided return can't be routed, fall back to a plain return so a route always
    comes back.
    """
    crow = (target_km / 2.0) / detour
    aim = (into_wind_bearing + (-10.0 if seed % 2 else 10.0) * (seed // 2)) % 360
    crow *= (1.0 + 0.05 * ((seed % 3) - 1))            # +/-5% length variety
    tlat, tlng = _destination(lat, lng, aim, crow)

    out_pts = [[lng, lat], [tlng, tlat]]
    if valhalla.enabled():                              # experimental, gated off by default
        try:
            vc = valhalla.wind_biased_leg(lat, lng, tlat, tlng, into_wind_bearing, timeout)
            if vc and len(vc) >= 2:
                out_pts = [[p[1], p[0]] for p in _thin(vc, 8)]   # retrace via ORS
        except Exception:
            out_pts = [[lng, lat], [tlng, tlat]]        # any Valhalla issue -> plain ORS

    o_coords, o_eles, o_dist, o_pav, o_unp, o_busy, o_path, o_run = _ors_directions(
        api_key, profile, out_pts, timeout)
    glat, glng = o_coords[-1]                           # real routed turnaround node

    avoid = _corridor_multipolygon(o_coords, buffer_m)
    try:
        b_coords, b_eles, b_dist, b_pav, b_unp, b_busy, b_path, b_run = _ors_directions(
            api_key, profile, [[glng, glat], [lng, lat]], timeout, avoid_polygons=avoid)
    except requests.HTTPError:                          # avoided return unroutable
        b_coords, b_eles, b_dist, b_pav, b_unp, b_busy, b_path, b_run = _ors_directions(
            api_key, profile, [[glng, glat], [lng, lat]], timeout)

    full_coords = o_coords + b_coords[1:]
    full_eles = (o_eles + b_eles[1:]) if (o_eles and b_eles) else []
    total = o_dist + b_dist
    ow, bw = o_dist, b_dist
    tot = ow + bw
    paved = (o_pav * ow + b_pav * bw) / tot if tot else 1.0
    unpaved = (o_unp * ow + b_unp * bw) / tot if tot else 0.0
    busy = (o_busy * ow + b_busy * bw) / tot if tot else 0.0
    path = (o_path * ow + b_path * bw) / tot if tot else 0.0
    path_run = max(o_run, b_run) / total if total else 0.0
    return Candidate(coords=full_coords, distance_km=total,
                     ascent_m=_smoothed_ascent(full_eles) if full_eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_frac=path, path_run_frac=path_run, shape="wind")


def _candidate_from_waypoints(api_key, profile, waypoints, shape, timeout):
    """Route a through-path over `waypoints` ((lat,lng) corners) -> a Candidate.

    The general form of the geometric builders, used by `refine_candidate` to rebuild
    a loop/rectangle after nudging a corner. Carries the waypoints so the refined
    route can be nudged again.
    """
    pts = [[lng, lat] for lat, lng in waypoints]                    # -> ORS [lng, lat]
    coords, eles, dist, paved, unpaved, busy, path, path_run = _ors_directions(
        api_key, profile, pts, timeout)
    return Candidate(coords=coords, distance_km=dist,
                     ascent_m=_smoothed_ascent(eles) if eles else 0.0,
                     paved_frac=paved, unpaved_frac=unpaved, busy_frac=busy,
                     path_frac=path, path_run_frac=(path_run / dist if dist else 0.0),
                     shape=shape, waypoints=list(waypoints))


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
                        n=8, points=5, timeout=40, sleep=0.4,
                        shapes=("loop",), into_wind_bearing=None, zone=None,
                        loop_geom=None, workers=ORS_MAX_WORKERS):
    """Generate `n` candidate routes of ~target_km from (lat, lng).

    `shapes` chooses which route forms to produce ("loop", "out-and-back",
    "lollipop"); `n` is split across them. Directional shapes (out-and-back,
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
        if shape == "roundtrip":
            return _make_roundtrip(api_key, profile, lat, lng, target_km, points, idx, timeout)
        if shape == "out-and-back":
            return _make_out_back(api_key, profile, lat, lng, target_km, bearing, timeout)
        if shape == "rectangle":
            return _make_rectangle(api_key, profile, lat, lng, target_km, bearing, timeout,
                                   cross_sign=(1 if idx % 2 == 0 else -1))
        if shape == "staging":
            return _make_staging(api_key, profile, lat, lng, target_km, zone,
                                 idx, timeout, loop_sides=loop_sides,
                                 loop_detour=loop_detour)
        if shape == "wind":
            # Aim at the TRUE into-wind bearing (not the off-wind offsets) — the
            # whole point is headwind out, tailwind home on different roads.
            return _make_wind_loop(
                api_key, profile, lat, lng, target_km,
                into_wind_bearing if into_wind_bearing is not None else 0.0,
                timeout, seed=idx)
        return _make_lollipop(api_key, profile, lat, lng, target_km, bearing,
                              idx, timeout, loop_sides=loop_sides,
                              loop_detour=loop_detour)

    # Generate concurrently — each candidate is an independent ORS round-trip, so a
    # bounded thread pool collapses ~12 sequential calls into a few batches (within
    # the free tier's burst). Results are slotted back into plan order so the route
    # set AND its tie-break ordering are identical to the serial path; a seed that
    # 404s/HTTPErrors is skipped, exactly as before. `workers=1` == fully serial.
    slots = [None] * len(specs)
    pool = max(1, min(workers, len(specs))) if specs else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=pool) as ex:
        futures = {ex.submit(_build, shape, idx): pos
                   for pos, (shape, idx) in enumerate(specs)}
        for fut in concurrent.futures.as_completed(futures):
            try:
                slots[futures[fut]] = fut.result()
            except requests.HTTPError:
                pass                              # skip a bad seed/bearing, keep the rest
    out = [c for c in slots if c is not None]

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
