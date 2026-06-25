"""Core data containers shared across windroute (no logic, no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #
@dataclass
class Wind:
    direction_from_deg: float   # meteorological convention: direction wind comes FROM
    speed_mph: float
    gust_mph: float
    valid_time: str             # local ISO timestamp the forecast applies to
    known: bool = True          # False when no forecast could be fetched (calm fallback);
                                # `evaluate` then neutralizes the wind term so it doesn't
                                # bias direction, and the planner adds a user-facing note.

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
