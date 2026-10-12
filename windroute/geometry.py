"""Geometry + compass primitives (pure: math/re only)."""
from __future__ import annotations

import math
import re


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


def _polyline_km(coords):
    """Length (km) of a lat/lng polyline."""
    return sum(_haversine_km(a, b) for a, b in zip(coords, coords[1:]))


def _self_intersections(coords):
    """Count times a route crosses itself. A clean loop or rectangle scores 0; a
    tangled round_trip loop - where ORS scattered via-points that cross-connect -
    scores high (verified: clean rectangle 0, messy lollipop 67).

    Two kinds of crossing are counted:
      - two non-adjacent segments crossing in the open (each strictly splits the
        other's endpoints);
      - the route passing twice through the SAME point (a road intersection is a
        node on both passes) and the second pass going from one side of the first
        to the other.
    Merely touching is NOT a crossing: riding back along the same road (an
    out-and-back, a lollipop stem) shares points with the way out without crossing
    it, and used to count once per shared point (an Oct 2026 recorded ride's
    out-and-back stem read as 11 crossings; it has 1). O(n^2) with a bounding-box
    quick-reject, which is plenty fast at our point counts.
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
            if d1 * d2 < 0 and d3 * d4 < 0:            # strictly through each other
                count += 1

    # Crossings where the passes MEET (the strict test above sees those as touches):
    # follow each stretch the route rides twice - one shared point (an intersection)
    # or a run of them (a shared bit of road) - and count it when the second pass
    # comes in on one side of the first and leaves on the other.
    at: dict = {}
    for k, p in enumerate(pts):
        at.setdefault((round(p[0], 7), round(p[1], 7)), []).append(k)
    used = set()
    for j in range(1, m):
        for i in at[(round(pts[j][0], 7), round(pts[j][1], 7))]:
            if i > j - 2 or i == 0 or (j, i) in used:
                continue
            step = (1 if pts[i + 1] == pts[j + 1]
                    else -1 if pts[i - 1] == pts[j + 1] else 0)
            j_end, i_end = j, i                        # follow the shared run forward
            while (step and 0 < i_end + step < m and j_end + 1 < m
                   and i_end + step < j_end and pts[i_end + step] == pts[j_end + 1]):
                j_end, i_end = j_end + 1, i_end + step
                used.add((j_end, i_end))
            side_in = _side_of(pts, i, pts[j - 1])
            side_out = _side_of(pts, i_end, pts[j_end + 1])
            if side_in is not None and side_out is not None and side_in != side_out:
                count += 1
    return count


_OVERLAP_RAD = math.radians(1.0)    # directions this close = along the road, no side


def _side_of(pts, i, q):
    """Which side (True/False) of the route through pts[i] the point q is on, or
    None when q lies along the route there (no side) or i is an end of the route.
    The side is consistent for a given direction of travel: the sector swept going
    counter-clockwise from the way in to the way out."""
    v = pts[i]

    def away(n, step):                                 # nearest neighbour not at v
        while 0 <= n < len(pts) and pts[n] == v:
            n += step
        return pts[n] if 0 <= n < len(pts) else None

    a, b = away(i - 1, -1), away(i + 1, 1)
    if a is None or b is None or q == v:
        return None
    k = math.cos(math.radians(v[0]))
    a_in, a_out, t = (math.atan2(p[0] - v[0], (p[1] - v[1]) * k) for p in (a, b, q))
    for u in (a_in, a_out):
        if abs((t - u + math.pi) % (2 * math.pi) - math.pi) < _OVERLAP_RAD:
            return None
    return ((t - a_in) % (2 * math.pi)) < ((a_out - a_in) % (2 * math.pi))


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
