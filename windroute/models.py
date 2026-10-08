"""Core data containers shared across windroute (no logic, no I/O)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #
@dataclass
class WindField:
    """Hourly wind over the ride area, so scoring can use the wind you'll actually
    meet at each point of the route at the time you get there (not just the wind
    at the start when you leave).

    Wind is stored as its FROM-vector components in mph (u = east, v = north of
    the direction the wind comes from), which interpolate cleanly through the
    360/0 wrap. `hours` are offsets from the ride start; `u[p][h]` / `v[p][h]` are
    at `points[p]`, hour `hours[h]`. Space is inverse-distance weighted across the
    sample points; time is linear between hours (clamped at the ends).
    """
    points: list                # [(lat, lng), ...] sample locations (start first)
    hours: list                 # [float, ...] hours since ride start, ascending
    u: list                     # [[mph, ...] per hour] per point
    v: list

    def vector_at(self, lat, lng, hours):
        """(u, v) FROM-vector in mph at a place and time (hours since start)."""
        # time: index bracket + linear weight (clamped outside the window)
        hs = self.hours
        if hours <= hs[0]:
            i0 = i1 = 0
            f = 0.0
        elif hours >= hs[-1]:
            i0 = i1 = len(hs) - 1
            f = 0.0
        else:
            i1 = next(i for i, h in enumerate(hs) if h >= hours)
            i0 = i1 - 1
            f = (hours - hs[i0]) / (hs[i1] - hs[i0])
        # space: inverse-distance weights (cheap flat-earth km; points are close)
        coslat = math.cos(math.radians(lat))
        dists = [math.hypot((plat - lat) * 111.0, (plng - lng) * 111.0 * coslat)
                 for plat, plng in self.points]
        near = min(range(len(dists)), key=dists.__getitem__)
        if dists[near] < 0.05:                     # on a sample point: use it as-is
            wts = [(near, 1.0)]
        else:
            wts = [(p, 1.0 / d ** 2) for p, d in enumerate(dists)]
        tot = sum(w for _, w in wts)
        u = v = 0.0
        for p, w in wts:
            up = self.u[p][i0] + f * (self.u[p][i1] - self.u[p][i0])
            vp = self.v[p][i0] + f * (self.v[p][i1] - self.v[p][i0])
            u += w * up
            v += w * vp
        return u / tot, v / tot

    def at(self, lat, lng, hours):
        """(direction_from_deg, speed_mph) at a place and time."""
        u, v = self.vector_at(lat, lng, hours)
        return math.degrees(math.atan2(u, v)) % 360, math.hypot(u, v)


@dataclass
class Wind:
    direction_from_deg: float   # meteorological convention: direction wind comes FROM
    speed_mph: float
    gust_mph: float
    valid_time: str             # local ISO timestamp the forecast applies to
    known: bool = True          # False when no forecast could be fetched (calm fallback);
                                # `evaluate` then neutralizes the wind term so it doesn't
                                # bias direction, and the planner adds a user-facing note.
    field: WindField = None     # hourly wind over the ride area (None = assume this
                                # single wind holds everywhere for the whole ride)

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
    poor_road_frac: float = 0.0 # fraction ORS rates poor for bikes (suitability <= 5) that
                                # isn't a State Road: the arterials busy_frac misses
    path_frac: float = 0.0      # fraction on separated bike/foot paths (multiuse trails)
    path_run_frac: float = 0.0  # LONGEST contiguous path run as a fraction of the route
                                # (the connector-vs-destination signal: a short run is a
                                # trail used to link roads; a long run is "riding the path")
    bikelane_frac: float = 0.0  # fraction on roads with an on-road bike lane (OSM only)
    good_gravel_frac: float = 0.0  # fraction on confirmed GOOD gravel (OSM quality; Task 3c)
    unrideable_frac: float = 0.0   # fraction on unrideable surface (mud/ground/grade5; OSM only)
    surface_by_source: dict = field(default_factory=dict)  # source name -> unpaved_frac
    eles: list = None           # elevation (m) per point, aligned with `coords` (for the
                                # web elevation profile). None when ORS returned no elevation.
    score_coords: list = None   # subset of coords the wind score uses (staging: the
                                # destination loop only, so the fixed transit legs to/from
                                # a ride zone don't dominate the wind line). None = whole route.
    waypoints: list = None      # the routable (lat,lng) corners this route was built from
                                # (loop/rectangle only) — the handle local-search refine nudges.
    road_names: list = None     # road name for the stretch leaving each point, aligned with
                                # `coords` ("" = unnamed) — the source of the cue sheet.
    wind_score: float = 0.0     # first-half headwind minus second-half headwind
                                # (with a changing wind: minus a net-headwind penalty)
    head_out_mph: float = 0.0   # mean headwind (+) / tailwind (-) met on the first half
    head_back_mph: float = 0.0  # ... and on the second half, at your riding speed
    ride_hours: float = 0.0     # time to ride the scored route at your pace, slowed by
                                # headwinds / sped up by tailwinds (0 = not computed)
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
