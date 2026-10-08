"""Route scoring: weights, evaluate, route-option selection, explanations (pure)."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .geometry import (_bearing, _destination, _haversine_km, _polyline_km,
                       _self_intersections, compass_label)
from .models import Candidate, RouteOption, Wind, WindField
from .routing import _LOOP_SIDES


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


# Changing wind (the wind you'll MEET, not the wind when you leave). A loop in a
# steady wind nets out to zero headwind, so the only thing that matters is WHEN
# you take it (into it first, tailwind home). When the wind dies or shifts during
# the ride that's no longer true: riding out into a headwind that's gone by the
# time you turn home just means a headwind with no payback. So the timed score is
#     (headwind out - headwind back) - WIND_NET_WEIGHT * net headwind,
# all over the mean wind speed met. In a steady wind the net term is ~0 and this
# is exactly `wind_score`. The weight sets the break-even: with 4, riding into a
# wind first still wins as long as it holds at >= 1/3 of its strength for the ride
# home (b/a > (W-2)/(W+2)); if it'll drop below that, tailwind-out wins.
WIND_NET_WEIGHT = 4.0
DEFAULT_RIDE_SPEED_MPH = 17.0
_MPH_TO_KMH = 1.609344


def _route_offset_km(coords, score_coords):
    """Distance ridden before `score_coords` starts within `coords` (staging: the
    transit leg to the ride zone), so the scored loop is timed from when you
    actually get there. 0 when they're the same route or it can't be located."""
    if not score_coords or score_coords is coords:
        return 0.0
    try:
        i = coords.index(score_coords[0])
    except ValueError:
        return 0.0
    return _polyline_km(coords[:i + 1])


# Riding speed isn't constant: you go slower into a headwind and faster with a
# tailwind, so the clock (and so the wind you meet later) depends on the wind you
# met earlier. Ground speed = your average - WIND_SPEED_EFFECT * headwind, which
# grows with the wind's strength: at 0.25 a 12 mph headwind costs ~3 mph and a 12
# mph tailwind gives ~3 back (riders push harder into the wind, so this is milder
# than constant-power physics). Clamped to a sane band of your average.
WIND_SPEED_EFFECT = 0.25
GROUND_SPEED_MIN_FRAC = 0.5
GROUND_SPEED_MAX_FRAC = 1.6


def ground_speed_mph(speed_mph, headwind_mph):
    """Your speed on a stretch with this headwind (+) / tailwind (-) component."""
    v = speed_mph - WIND_SPEED_EFFECT * headwind_mph
    return max(GROUND_SPEED_MIN_FRAC * speed_mph, min(GROUND_SPEED_MAX_FRAC * speed_mph, v))


def _ride_segments(coords, field, speed_mph, start_hours=0.0):
    """Ride `coords` through `field`: [(d_km, headwind_mph, wind_mph), ...] plus the
    finishing clock in hours. Each segment's wind is looked up at its midpoint at
    the time you get there, and its ground speed follows that headwind."""
    segs = []
    t = start_hours
    for a, b in zip(coords, coords[1:]):
        d = _haversine_km(a, b)
        if d <= 0:
            continue
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        brg = math.radians(_bearing(a, b))
        # wind at the midpoint, timed with your base pace (segments are short)
        u, v = field.vector_at(mid[0], mid[1], t + d / 2 / (speed_mph * _MPH_TO_KMH))
        # FROM-vector . travel unit vector = headwind component (+ head, - tail)
        hw = u * math.sin(brg) + v * math.cos(brg)
        segs.append((d, hw, math.hypot(u, v)))
        t += d / (ground_speed_mph(speed_mph, hw) * _MPH_TO_KMH)
    return segs, t


def timed_wind_score(coords, field, speed_mph=DEFAULT_RIDE_SPEED_MPH,
                     start_offset_km=0.0):
    """Score a route against the wind met at each point at the time you reach it.

    Rides the route at `speed_mph`, slowed by headwinds and sped up by tailwinds
    (`ground_speed_mph`), looking up the wind from `field` (a WindField) at each
    segment's midpoint and arrival time. Returns (score, head_out_mph,
    head_back_mph, ride_hours): score is on `wind_score`'s scale (~[-2, +2], + =
    good), the middle two are the mean headwind (+) / tailwind (-) in mph on each
    half, and ride_hours is how long the route takes at that pace in that wind.
    `start_offset_km` delays the clock (riding a transit leg first, at base pace).
    """
    speed_mph = max(1.0, speed_mph)
    start = start_offset_km / (speed_mph * _MPH_TO_KMH)
    segs, end = _ride_segments(coords, field, speed_mph, start)
    hours = end - start
    total = sum(d for d, _, _ in segs)
    if total <= 0:
        return 0.0, 0.0, 0.0, 0.0
    half, run = total / 2.0, 0.0
    first, second = [], []
    for s in segs:
        (first if run < half else second).append(s)
        run += s[0]

    def wmean(group, k):
        tot = sum(s[0] for s in group)
        return sum(s[0] * s[k] for s in group) / tot if tot > 0 else 0.0

    out_mph, back_mph = wmean(first, 1), wmean(second, 1)
    met = wmean(segs, 2)                          # mean wind speed met on the ride
    if met <= 1e-6:
        return 0.0, out_mph, back_mph, hours
    net = wmean(segs, 1)
    score = ((out_mph - back_mph) - WIND_NET_WEIGHT * net) / met
    return max(-2.0, min(2.0, score)), out_mph, back_mph, hours


def best_aim_bearing(field, lat, lng, target_km, speed_mph, default_bearing,
                     step_deg=15.0, tie=0.05):
    """The heading to aim candidates at so they ride the forecast wind well.

    Candidates are seeded around one bearing (into the wind, with offsets). With a
    changing wind the best heading isn't always into the start-time wind, so try a
    straight out-and-back of the target length on every `step_deg` heading under
    the timed score and return the best. `default_bearing` (into the wind at the
    start) wins any near-tie, so a steady wind seeds exactly as before.
    """
    def probe(brg):
        reach = target_km / 2.0
        n = max(2, int(reach))                    # ~1 km steps
        out = [(lat, lng)] + [_destination(lat, lng, brg, reach * i / n)
                              for i in range(1, n + 1)]
        return timed_wind_score(out + out[-2::-1], field, speed_mph)[0]

    best_brg, best = default_bearing, probe(default_bearing)
    for k in range(int(360 / step_deg)):
        brg = k * step_deg
        s = probe(brg)
        if s > best + tie:
            best_brg, best = brg, s
    return best_brg


def _steady_field(wind: Wind) -> WindField:
    """A one-point, one-hour field holding `wind` everywhere, all ride long."""
    rad = math.radians(wind.direction_from_deg)
    return WindField(points=[(0.0, 0.0)], hours=[0.0],
                     u=[[wind.speed_mph * math.sin(rad)]],
                     v=[[wind.speed_mph * math.cos(rad)]])


def _hhmm(hours):
    """1.92 -> '1:55'."""
    m = round(hours * 60)
    return f"{m // 60}:{m % 60:02d}"


def _wind_phrase(mph, leg):
    """'12 mph headwind out' / '6 mph tailwind home' / 'little wind home'."""
    if abs(mph) < 2.0:
        return f"little wind {leg}"
    return f"{abs(mph):.0f} mph {'headwind' if mph > 0 else 'tailwind'} {leg}"


def wind_summary(c: Candidate) -> str:
    """What the wind does on this route, e.g. '9 mph headwind out, 7 mph tailwind
    home' — computed from the wind you'll meet, so it stays true when it changes."""
    return f"{_wind_phrase(c.head_out_mph, 'out')}, {_wind_phrase(c.head_back_mph, 'home')}"


def wind_verdict(c: Candidate) -> str:
    """Short label for a route's wind line (cards / candidate table)."""
    if c.wind_score > 0.2:
        return "into wind first" if c.head_out_mph >= 0 else "tailwind first"
    if c.wind_score < -0.2:
        return "wind against"
    return "neutral"


# Busy-road penalty: a small free band (unavoidable arterial crossings/connectors
# don't get punished) then a steep linear penalty on the State-Road fraction beyond
# it. Weight is high because "keep me off US-12/US-35" is a hard rider preference.
BUSY_FREE_FRAC = 0.05
W_BUSY = 1.5

# Poor-for-bikes penalty: roads ORS rates <= 5/10 that aren't State Roads (city and
# county arterials the busy class misses). A lighter weight than W_BUSY because the
# rating is coarser than "this is a US highway". Free band 2% (vs 5% for busy): 5%
# of a 25 mi ride is over a mile on something like Cermak. Before/after on real
# plans (2026-10-08): Mokena / Oak Park unchanged, Naperville moved one 7%-arterial
# route from #4 to #6; top picks unchanged everywhere — it nudges, it doesn't upend.
POOR_FREE_FRAC = 0.02
W_POOR = 1.0

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


# --------------------------------------------------------------------------- #
# Archetype- + ride-type-keyed tuning (work-plan Tasks 2 + 3)
# --------------------------------------------------------------------------- #
# The weights above were tuned for ONE place + ride type (flat IL grid-farmland
# ROAD rides, 108 real trips). To let the scorer travel, the route-scoring
# tunables live in a `RouteWeights` record. Two axes vary it:
#   - ride type: `ROAD_WEIGHTS` vs `GRAVEL_WEIGHTS` (Task 3a). Road PENALIZES
#     gravel; gravel SEEKS it (a target-band reward, Task 3b). Gravel also rides a
#     lower base wind weight (terrain/surface matter more than the wind line).
#   - archetype: `WEIGHTS_BY_ARCHETYPE` (Task 2), overlaid on the ride-type base.
# The `grid-farmland` ROAD row is built straight from the constants above, so it is
# byte-identical to today, and the default path (no classification / `unknown`,
# road) reproduces current behaviour exactly. `evaluate` reads only this record —
# no ride-type `if` in the formula. Non-grid / gravel rows are a conservative FIRST
# PASS — calibrate against real rides later (work-plan Task 8).
@dataclass(frozen=True)
class RouteWeights:
    """The route-scoring tunables `evaluate` reads, swappable per archetype/ride."""
    wind_scale: float = 1.0            # archetype multiplier on the base wind weight
    w_wind: float = 1.0                # base wind weight (road 1.0 / gravel lower)
    # Road gravel PENALTY (linear + convex on confirmed unpaved). 0 for gravel.
    road_gravel_lin: float = W_ROAD_GRAVEL_LIN
    road_gravel_quad: float = W_ROAD_GRAVEL_QUAD
    # Gravel SEEK reward (Task 3b): weight + target band for diminishing returns.
    # 0 for road rides (so it's a no-op there).
    gravel_seek: float = 0.0
    gravel_seek_lo: float = 0.5        # reward ramps to full by this unpaved frac
    gravel_seek_hi: float = 0.75       # ... holds to here, then gently tapers
    w_good_gravel: float = 0.0         # bonus for CONFIRMED good gravel (gravel only)
    # Hard-avoid unrideable surface (mud/ground/grade5) for BOTH ride types (3c).
    # Only bites when OSM quality data was consulted (unrideable_frac else 0).
    w_unrideable: float = 2.5
    w_dist: float = 0.5                # distance-excess penalty coefficient
    w_busy: float = W_BUSY
    busy_free_frac: float = BUSY_FREE_FRAC
    w_poor: float = W_POOR
    poor_free_frac: float = POOR_FREE_FRAC
    w_path: float = W_PATH
    path_run_free_frac: float = PATH_RUN_FREE_FRAC
    w_bikelane: float = W_BIKELANE
    w_tidy: float = W_TIDY
    tidy_free_per_km: float = TIDY_FREE_PER_KM


# grid-farmland ROAD == today's constants (single source of truth, byte-identical).
_GRID_FARMLAND_WEIGHTS = RouteWeights()


def as_gravel(rw: RouteWeights) -> RouteWeights:
    """Turn a road weight profile into its gravel counterpart (Task 3a/3b).

    Swaps the gravel PENALTY for a SEEK reward, lowers the base wind weight, and
    rewards confirmed good gravel — while keeping the archetype's other tuning
    (wind_scale, path/lane/busy/tidy, hard-avoid). A no-op-preserving transform:
    road-only fields go to 0, gravel-only fields turn on.
    """
    return replace(rw, w_wind=0.55, road_gravel_lin=0.0, road_gravel_quad=0.0,
                   gravel_seek=1.2, w_good_gravel=0.3)


# Named grid-farmland base profiles per ride type (the work plan's "first-class
# vectors"). Archetype variation is overlaid by `weights_for`.
ROAD_WEIGHTS = _GRID_FARMLAND_WEIGHTS
GRAVEL_WEIGHTS = as_gravel(ROAD_WEIGHTS)

WEIGHTS_BY_ARCHETYPE = {
    "grid-farmland": _GRID_FARMLAND_WEIGHTS,
    # Mountains/forest: wind matters a little less (terrain dominates), and a
    # separated path or rail-trail is more often the only sane corridor, so the
    # long-path penalty eases a touch. First pass — not yet calibrated.
    "mountain": RouteWeights(wind_scale=0.8, w_path=0.4, path_run_free_frac=0.35),
    "forested-rolling": RouteWeights(wind_scale=0.9, w_path=0.45,
                                     path_run_free_frac=0.30),
    # Coastal: shore roads and waterfront paths are the draw; ease the path
    # penalty, keep wind (sea breezes are real and worth riding into first).
    "coastal": RouteWeights(w_path=0.45, path_run_free_frac=0.30),
    # Suburban: protected lanes/paths matter most for safety; reward lanes more
    # and don't over-punish path runs (often the safest line out of a suburb).
    "suburban-sprawl": RouteWeights(w_bikelane=0.8, w_path=0.45,
                                    path_run_free_frac=0.35),
    # Arid-open: like grid-farmland but emptier; keep the baseline.
    "arid-open": _GRID_FARMLAND_WEIGHTS,
    # Unknown / anything unmapped -> safe grid-farmland defaults.
    "unknown": _GRID_FARMLAND_WEIGHTS,
}

# Loop geometry per archetype: (candidate n-gon side counts cycled per seed,
# detour factor). Organic/mountain country has curvy roads, so a polygon loop with
# MORE sides and a LARGER detour snaps its vertices onto the real graph instead of
# cutting crow-flies lines across nothing. grid-farmland keeps today's exact values.
LOOP_GEOM_BY_ARCHETYPE = {
    "grid-farmland": (_LOOP_SIDES, 1.25),
    "mountain": ((6, 7, 8, 6, 7, 8), 1.4),
    "forested-rolling": ((6, 5, 7, 6, 5, 7), 1.35),
    "coastal": (_LOOP_SIDES, 1.3),
    "suburban-sprawl": (_LOOP_SIDES, 1.25),
    "arid-open": (_LOOP_SIDES, 1.25),
    "unknown": (_LOOP_SIDES, 1.25),
}

# Default route shapes that make sense per archetype. The wind-aligned RECTANGLE is
# a grid-country trick (long section-line legs); it cuts nonsense lines across curvy
# terrain, so mountain/forested drop it. grid-farmland keeps the full set.
SHAPES_BY_ARCHETYPE = {
    "grid-farmland": ("loop", "lollipop", "rectangle"),
    "mountain": ("loop", "lollipop"),
    "forested-rolling": ("loop", "lollipop"),
    "coastal": ("loop", "lollipop", "rectangle"),
    "suburban-sprawl": ("loop", "lollipop", "rectangle"),
    "arid-open": ("loop", "lollipop", "rectangle"),
    "unknown": ("loop", "lollipop", "rectangle"),
}


def weights_for(archetype, ride_type="road") -> RouteWeights:
    """RouteWeights for an (archetype, ride_type).

    Picks the archetype's road profile (None / unmapped -> grid-farmland baseline),
    then for a gravel ride transforms it with `as_gravel`. `weights_for(None,
    "road")` is the grid-farmland baseline object (identity preserved), so the
    default road path stays byte-identical.
    """
    base = WEIGHTS_BY_ARCHETYPE.get(archetype or "grid-farmland",
                                    _GRID_FARMLAND_WEIGHTS)
    return as_gravel(base) if str(ride_type).lower().strip() == "gravel" else base


def loop_geom_for(archetype):
    """(loop_sides_tuple, detour) for an archetype (default grid-farmland)."""
    return LOOP_GEOM_BY_ARCHETYPE.get(archetype or "grid-farmland",
                                      LOOP_GEOM_BY_ARCHETYPE["grid-farmland"])


def shapes_for(archetype, requested):
    """Filter `requested` shapes to those sensible for `archetype`, order preserved.

    The archetype provides the *allowed* default set; an explicit user shape that
    the archetype rejects (e.g. a rectangle in the mountains) is dropped. Special
    shapes the caller adds deliberately ('staging', 'out-and-back') are always
    honoured — they're opt-in, not archetype defaults. Never returns an empty list
    (falls back to the requested list, then to 'loop').
    """
    allowed = set(SHAPES_BY_ARCHETYPE.get(archetype or "grid-farmland",
                                          SHAPES_BY_ARCHETYPE["grid-farmland"]))
    always = {"staging", "out-and-back"}
    out = [s for s in requested if s in allowed or s in always]
    return out or list(requested) or ["loop"]


def _gravel_seek_reward(unpaved_frac, lo, hi):
    """Diminishing-returns reward for riding gravel (Task 3b), in ~[0, 1].

    Rises linearly to full by `lo`, holds across the target band [lo, hi], then
    tapers gently above `hi` (floored, never to 0) so a mostly-gravel route is
    still good but not infinitely better — and an area that only offers ~30% gravel
    still earns a solid reward (a sane route comes back instead of nothing)."""
    u = unpaved_frac
    if u <= 0:
        return 0.0
    if u < lo:
        return u / lo if lo > 0 else 1.0
    if u <= hi:
        return 1.0
    return max(0.7, 1.0 - (u - hi))          # gentle taper above the band


def evaluate(candidates, wind: Wind, ride_type: str, target_km: float,
             tolerance_km: float = 0.0, weights: "RouteWeights" = None,
             busy_baseline: float = 0.0, speed_mph: float = DEFAULT_RIDE_SPEED_MPH):
    """Score every candidate and return them sorted best-first.

    `tolerance_km` is a free buffer: a route whose length is within this many km
    of `target_km` gets no distance penalty. Only the distance *beyond* the band
    is penalized, so e.g. a 28-mi loop and a 32-mi loop both count as "on target"
    when you asked for 30 mi +/- 3.

    Routes are also penalized for time spent on arterial "State Road" class
    (US-highways) beyond a small free band, so quiet back-road routes win.

    `busy_baseline` (Task 4a) is the corridor's *unavoidable* arterial fraction —
    the quietest level any candidate achieves. The busy penalty is charged only on
    arterial mileage ABOVE this baseline (plus the free band), so where every route
    must use some arterial, the relatively-quietest still wins instead of all being
    tanked. Default 0.0 reproduces the absolute penalty exactly.

    `weights` (a `RouteWeights`, default the grid-farmland baseline = today's
    constants) lets the caller pass an archetype-tuned set; `None` reproduces
    current behaviour exactly.

    When the wind carries a `field` (hourly wind over the ride area), the wind
    term is timed: each route is ridden at `speed_mph` and scored on the wind it
    meets where and when it gets there (`timed_wind_score`). Without a field the
    start-time wind is assumed to hold for the whole ride, as before.
    """
    w = weights or weights_for(None, ride_type)
    into = wind.into_wind_bearing
    for c in candidates:
        geom = c.score_coords or c.coords
        if wind.known and wind.field is not None:
            c.wind_score, c.head_out_mph, c.head_back_mph, c.ride_hours = timed_wind_score(
                geom, wind.field, speed_mph,
                _route_offset_km(c.coords, c.score_coords))
            wind_norm = (c.wind_score + 2.0) / 4.0       # -> ~0..1
        elif wind.known:
            c.wind_score = wind_score(geom, into)
            # steady wind: the same score as a single-point, single-hour field
            _, c.head_out_mph, c.head_back_mph, c.ride_hours = timed_wind_score(
                geom, _steady_field(wind), speed_mph)
            wind_norm = (c.wind_score + 2.0) / 4.0       # -> ~0..1
        else:
            # No forecast available (planner notes it): make the wind term a
            # constant so it doesn't bias direction — rank on the other signals.
            c.wind_score = c.head_out_mph = c.head_back_mph = 0.0
            c.ride_hours = _polyline_km(geom) / (max(1.0, speed_mph) * _MPH_TO_KMH)
            wind_norm = 0.5

        # `surface_score` is the display figure (paved on road / unpaved on gravel);
        # the SCORING is fully weights-driven, no ride-type branch in the formula.
        c.surface_score = c.unpaved_frac if ride_type == "gravel" else c.paved_frac
        # Road: a steep, ramping penalty on KNOWN gravel (0 weight on gravel rides).
        # Gravel: a diminishing-returns SEEK reward + a bonus for confirmed good
        # gravel (0 weight on road rides). Both ride types hard-avoid unrideable
        # surface (mud/ground/grade5; unrideable_frac is 0 without OSM quality data,
        # so road grid-farmland stays byte-identical).
        surf_term = (w.gravel_seek * _gravel_seek_reward(c.unpaved_frac,
                                                         w.gravel_seek_lo,
                                                         w.gravel_seek_hi)
                     + w.w_good_gravel * c.good_gravel_frac
                     - (w.road_gravel_lin * c.unpaved_frac
                        + w.road_gravel_quad * c.unpaved_frac ** 2)
                     - w.w_unrideable * c.unrideable_frac)

        excess = max(0.0, abs(c.distance_km - target_km) - tolerance_km)
        dist_penalty = -excess / max(target_km, 1.0)
        # Charge busy only on arterial mileage above the corridor's unavoidable
        # baseline (Task 4a) and the free band — so unavoidable arterials don't
        # tank every route; the quietest available still wins.
        busy_penalty = -max(0.0, c.busy_frac - busy_baseline - w.busy_free_frac)
        poor_penalty = -max(0.0, c.poor_road_frac - w.poor_free_frac)
        # Penalize only the LONGEST contiguous path run beyond the connector band,
        # so trails used to link roads ride free but a long path stretch doesn't.
        path_penalty = -max(0.0, c.path_run_frac - w.path_run_free_frac)
        lane_bonus = c.bikelane_frac                  # 0 unless OSM was consulted
        # Tidiness: count self-crossings on the scored geometry (the loop, for
        # staging) and penalize them per km beyond a small free band, so a tangled
        # round_trip loop loses to a clean one.
        c.self_intersections = _self_intersections(geom)
        tidy_penalty = -max(0.0, c.self_intersections / max(_polyline_km(geom), 1.0)
                            - w.tidy_free_per_km)
        c.total_score = ((w.w_wind * w.wind_scale * wind_norm) + surf_term
                         + (w.w_dist * dist_penalty) + (w.w_busy * busy_penalty)
                         + (w.w_poor * poor_penalty)
                         + (w.w_path * path_penalty) + (w.w_bikelane * lane_bonus)
                         + (w.w_tidy * tidy_penalty))

    return sorted(candidates, key=lambda c: c.total_score, reverse=True)


def explain(best: Candidate, wind: Wind, ride_type: str) -> str:
    """One-line human rationale for why this route was chosen."""
    bits = [best.shape]
    if not wind.known:
        bits.append("no wind forecast")
    elif best.wind_score < -0.2:
        bits.append(f"wind line is compromised ({wind_summary(best)}; no good "
                    f"option for this loop shape today)")
    else:
        bits.append(wind_summary(best))
    if ride_type == "gravel":
        gq = (f", {best.good_gravel_frac * 100:.0f}% good" if best.good_gravel_frac else "")
        bits.append(f"{best.unpaved_frac * 100:.0f}% unpaved{gq}")
    elif best.unpaved_frac < 0.01:
        bits.append("no known gravel")
    else:
        bits.append(f"{best.unpaved_frac * 100:.0f}% known gravel")
    if best.unrideable_frac > 0:
        bits.append(f"{best.unrideable_frac * 100:.0f}% unrideable surface")
    if best.busy_frac <= BUSY_FREE_FRAC and best.poor_road_frac <= POOR_FREE_FRAC:
        bits.append("stays on quiet roads")
    else:
        if best.busy_frac > BUSY_FREE_FRAC:
            bits.append(f"{best.busy_frac * 100:.0f}% on busy highways")
        if best.poor_road_frac > POOR_FREE_FRAC:
            bits.append(f"{best.poor_road_frac * 100:.0f}% on busy arterials")
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
    reasons = []
    if lead == "wind":
        reasons.append(f"strongest wind line - {wind_summary(c)} "
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
        if not wind.known:
            reasons.append("balanced pick; no wind forecast available")
        elif c.wind_score > 0.2:
            reasons.append(f"good wind line: {wind_summary(c)}")
        elif c.wind_score < -0.2:
            reasons.append(f"best available, though the wind line is compromised "
                           f"today ({wind_summary(c)})")
        else:
            reasons.append(f"balanced pick; wind is roughly neutral ({wind_summary(c)})")

    if lead != "distance":
        reasons.append(f"{c.distance_km:.1f} km, +{c.ascent_m:.0f} m"
                       + (f", ~{_hhmm(c.ride_hours)} at your pace" if c.ride_hours else ""))
    if ride_type == "gravel":
        gq = (f" ({c.good_gravel_frac * 100:.0f}% good)" if c.good_gravel_frac else "")
        reasons.append(f"{c.unpaved_frac * 100:.0f}% unpaved{gq}")
    elif c.unpaved_frac >= 0.01 and lead != "quiet":
        reasons.append(f"{c.unpaved_frac * 100:.0f}% known gravel")
    if c.unrideable_frac > 0:
        reasons.append(f"{c.unrideable_frac * 100:.0f}% unrideable surface (avoided)")
    if lead != "quiet" and c.busy_frac > BUSY_FREE_FRAC:
        reasons.append(f"{c.busy_frac * 100:.0f}% on busy highways")
    if lead != "quiet" and c.poor_road_frac > POOR_FREE_FRAC:
        reasons.append(f"{c.poor_road_frac * 100:.0f}% on busy arterials")
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
         lambda c: -(c.busy_frac + c.poor_road_frac
                     + 0.5 * max(0.0, c.path_run_frac - PATH_RUN_FREE_FRAC)),
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
