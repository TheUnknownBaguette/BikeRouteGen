"""Learn the rider's real preferences from recorded Ride with GPS trips.

The scorer in `engine.evaluate` is a hand-tuned weighted sum. This module measures
the *same* features on tracks the rider actually rode — plus geometry the scorer
doesn't model (preferred distances, compass direction out of the start,
loop-vs-out-and-back habit) — and turns the aggregate into a plain-language report
with suggested weight changes. It does NOT change any weights; the rider reviews
the suggestions first.

Pipeline (the CLI drives the network parts so it can show progress):
    feats = [trip_features(coords, departed_at, surf=OverpassSurface().build([coords]))
             for each cached trip]
    profile = analyze_trips(feats)
    for line in suggest_weight_changes(profile): print(line)
"""
from __future__ import annotations

import datetime as dt

from . import engine
from .surface import _haversine_km
from .corrections import downsample

LOOP_CLOSE_KM = 0.5        # start within this of end -> a closed loop
OVERLAP_MATCH_M = 35.0     # a point this close to a far-away point on the track = retrace
OVERLAP_GAP_M = 25.0       # resample tracks to roughly this spacing before measuring
OVERLAP_SKIP_M = 200.0     # ignore matches within this along-track distance (local road)
OUT_BACK_OVERLAP = 0.45    # self-overlap above this -> out-and-back
KM_PER_MILE = 1.609344


# --------------------------------------------------------------------------- #
# Per-trip geometry
# --------------------------------------------------------------------------- #
def _self_overlap(coords, cell_deg=0.0015) -> float:
    """Fraction of points that retrace another, far-along-the-track point.

    Recorded GPS tracks are dense (points metres apart), so a naive
    index-neighbour test flags every point as overlapping its own road. We first
    resample to ~OVERLAP_GAP_M spacing, then for each point look for any *other*
    point more than OVERLAP_SKIP_M away along the track yet within
    OVERLAP_MATCH_M on the ground. ~1.0 for out-and-backs, ~0 for clean loops
    (only the start/finish neighbourhood overlaps)."""
    pts = downsample(coords, min_gap_m=OVERLAP_GAP_M, cap=2000)
    n = len(pts)
    if n < 8:
        return 0.0
    skip = max(2, int(OVERLAP_SKIP_M / OVERLAP_GAP_M))   # index window to ignore

    def cell(p):
        return int(p[0] / cell_deg), int(p[1] / cell_deg)

    grid: dict = {}
    for i, p in enumerate(pts):
        grid.setdefault(cell(p), []).append((i, p))

    matched = 0
    for i, p in enumerate(pts):
        ci, cj = cell(p)
        hit = False
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for j, q in grid.get((ci + di, cj + dj), ()):
                    if abs(i - j) <= skip:
                        continue
                    if _haversine_km(p, q) * 1000.0 < OVERLAP_MATCH_M:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
        if hit:
            matched += 1
    return matched / n


def _outbound_bearing(coords) -> float:
    """Bearing from the start to the point farthest from the start (the 'out' leg)."""
    start = coords[0]
    far, far_d = coords[0], 0.0
    for p in coords:
        d = _haversine_km(start, p)
        if d > far_d:
            far, far_d = p, d
    return engine._bearing(start, far)


# RWGPS's own track_type -> our shape vocabulary.
RWGPS_SHAPE = {
    "out_and_back": "out-and-back",
    "loop": "loop",
    "point_to_point": "point-to-point",
}


def _classify_shape(coords, overlap, track_type=None) -> str:
    """Prefer RWGPS's own track_type; fall back to the computed overlap metric."""
    if track_type:
        mapped = RWGPS_SHAPE.get(track_type.lower())
        if mapped:
            return mapped
    closed = _haversine_km(coords[0], coords[-1]) <= LOOP_CLOSE_KM
    if overlap >= OUT_BACK_OVERLAP:
        return "out-and-back"
    return "loop" if closed else "point-to-point"


def _angle_diff(a, b) -> float:
    """Smallest absolute difference between two bearings, in degrees [0, 180]."""
    return abs((a - b + 180) % 360 - 180)


# --------------------------------------------------------------------------- #
# Per-trip features
# --------------------------------------------------------------------------- #
def trip_features(coords, departed_at=None, surf=None, do_wind=True,
                  track_type=None, activity_type=None) -> dict | None:
    """Measure one trip. `surf` is a built OverpassSurface (or None to skip OSM);
    `departed_at` (datetime or ISO str) enables the historical wind backfill;
    `track_type` is RWGPS's own shape (preferred over the computed metric).

    Returns None for a track too short to analyse.
    """
    coords = [c for c in coords if c]
    if len(coords) < 4:
        return None
    dist_km = sum(_haversine_km(a, b) for a, b in zip(coords, coords[1:]))
    overlap = _self_overlap(coords)
    bearing = _outbound_bearing(coords)
    feat = {
        "distance_km": dist_km,
        "distance_mi": dist_km / KM_PER_MILE,
        "outbound_bearing": bearing,
        "sector": engine.compass_label(bearing),
        "self_overlap": overlap,
        "shape": _classify_shape(coords, overlap, track_type),
        "activity_type": activity_type,
        "n_points": len(coords),
        "unpaved_frac": None, "bikelane_frac": None,
        "busy_frac": None, "path_frac": None, "path_run_frac": None,
        "wind_score": None, "wind_align_deg": None, "wind_speed_mph": None,
    }

    if surf is not None:
        res = surf.classify(coords)
        if res:
            feat["unpaved_frac"] = res[1]
        lane = surf.classify_bikelane(coords)
        if lane is not None:
            feat["bikelane_frac"] = lane
        ways = surf.classify_waytype(coords)
        if ways is not None:
            feat["busy_frac"], feat["path_frac"] = ways
        run = surf.path_run_frac(coords)
        if run is not None:
            feat["path_run_frac"] = run

    when = _coerce_dt(departed_at)
    if do_wind and when is not None:
        try:
            wind = engine.get_wind_historical(coords[0][0], coords[0][1], when)
            feat["wind_score"] = engine.wind_score(coords, wind.into_wind_bearing)
            feat["wind_align_deg"] = _angle_diff(bearing, wind.into_wind_bearing)
            feat["wind_speed_mph"] = wind.speed_mph
        except Exception:                       # weather backfill is best-effort
            pass
    return feat


def _coerce_dt(when):
    if when is None or isinstance(when, dt.datetime):
        return when
    try:
        return dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _percentile(values, pct) -> float:
    """Nearest-rank percentile of a list (pct in 0..100)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return 0.0
    k = max(0, min(len(xs) - 1, round(pct / 100.0 * (len(xs) - 1))))
    return xs[k]


def _present(feats, key):
    return [f[key] for f in feats if f.get(key) is not None]


def analyze_trips(feats: list[dict]) -> dict:
    """Aggregate per-trip features into a rider profile (pure, no network)."""
    feats = [f for f in feats if f]
    n = len(feats)
    profile: dict = {"n_trips": n}
    if n == 0:
        return profile

    dist = _present(feats, "distance_mi")
    profile["distance_mi"] = {
        "median": _percentile(dist, 50), "p25": _percentile(dist, 25),
        "p75": _percentile(dist, 75), "min": min(dist), "max": max(dist),
    }

    # 16-sector outbound-direction histogram.
    sectors = {s: 0 for s in engine.COMPASS_16}
    for f in feats:
        sectors[f["sector"]] = sectors.get(f["sector"], 0) + 1
    profile["sectors"] = sectors
    profile["dominant_sectors"] = [s for s, _ in
                                   sorted(sectors.items(), key=lambda kv: -kv[1])[:3]
                                   if sectors[s] > 0]

    shapes: dict = {}
    for f in feats:
        shapes[f["shape"]] = shapes.get(f["shape"], 0) + 1
    profile["shapes"] = shapes
    profile["mean_self_overlap"] = sum(_present(feats, "self_overlap")) / n

    def stat(key):
        vals = _present(feats, key)
        if not vals:
            return None
        return {"n": len(vals), "mean": sum(vals) / len(vals),
                "median": _percentile(vals, 50), "p90": _percentile(vals, 90)}

    profile["unpaved_frac"] = stat("unpaved_frac")
    profile["busy_frac"] = stat("busy_frac")
    profile["path_frac"] = stat("path_frac")
    profile["path_run_frac"] = stat("path_run_frac")
    profile["bikelane_frac"] = stat("bikelane_frac")

    ws = _present(feats, "wind_score")
    if ws:
        profile["wind"] = {
            "n": len(ws),
            "mean_score": sum(ws) / len(ws),
            "into_wind_share": sum(1 for w in ws if w > 0.2) / len(ws),
            "mean_align_deg": (sum(_present(feats, "wind_align_deg"))
                               / len(_present(feats, "wind_align_deg"))),
        }
    return profile


# --------------------------------------------------------------------------- #
# Suggestions (read-only — mapped to the current engine constants)
# --------------------------------------------------------------------------- #
def suggest_weight_changes(profile: dict) -> list[str]:
    """Plain-language suggestions tying the profile to the current scorer weights.

    Never edits anything — these are for the rider to review before any retune.
    """
    out: list[str] = []
    n = profile.get("n_trips", 0)
    if n == 0:
        return ["No trips analysed yet — run `import` first."]
    if n < 8:
        out.append(f"Only {n} trips analysed — treat the below as weak signal; "
                   f"import more history for confident tuning.")

    unp = profile.get("unpaved_frac")
    if unp:
        p90 = unp["p90"]
        if p90 >= 0.25:
            out.append(f"You actually ride gravel: p90 unpaved is {p90*100:.0f}%. "
                       f"The road gravel penalty (W_ROAD_GRAVEL_LIN={engine.W_ROAD_GRAVEL_LIN}, "
                       f"W_ROAD_GRAVEL_QUAD={engine.W_ROAD_GRAVEL_QUAD}) may be too harsh — "
                       f"consider relaxing it, or default more rides to ride-type 'gravel'.")
        elif p90 <= 0.05:
            out.append(f"You almost never ride gravel (p90 unpaved {p90*100:.0f}%); "
                       f"the gravel penalty looks well justified.")

    busy = profile.get("busy_frac")
    if busy:
        m = busy["mean"]
        if m <= engine.BUSY_FREE_FRAC:
            out.append(f"You keep off busy highways in practice (mean busy "
                       f"{m*100:.0f}% <= free band {engine.BUSY_FREE_FRAC*100:.0f}%); "
                       f"W_BUSY={engine.W_BUSY} is doing its job - keep it high.")
        elif m >= 0.12:
            out.append(f"You tolerate more arterial than the model assumes (mean busy "
                       f"{m*100:.0f}%); W_BUSY={engine.W_BUSY} or the free band "
                       f"({engine.BUSY_FREE_FRAC*100:.0f}%) may be slightly too strict.")

    path = profile.get("path_frac")
    runp = profile.get("path_run_frac")
    if path and runp:
        m, rp90 = path["mean"], runp["p90"]
        free = engine.PATH_RUN_FREE_FRAC
        if m >= 0.15 and rp90 <= free:
            out.append(f"Paths are {m*100:.0f}% of your riding but used as connectors "
                       f"(longest continuous run p90 {rp90*100:.0f}% <= free band "
                       f"{free*100:.0f}%): the run-based path penalty (W_PATH="
                       f"{engine.W_PATH}) leaves these free, as intended.")
        elif rp90 > free:
            out.append(f"Some rides have long unbroken path stretches (run p90 "
                       f"{rp90*100:.0f}% > free band {free*100:.0f}%): the run-based "
                       f"penalty W_PATH={engine.W_PATH} will bite on those, not on your "
                       f"connector rides.")
        elif m <= 0.03:
            out.append(f"You rarely use separated paths (mean {m*100:.0f}%); "
                       f"W_PATH={engine.W_PATH} is fine.")

    lane = profile.get("bikelane_frac")
    if lane and lane["mean"] >= 0.10:
        out.append(f"You favour on-road bike lanes (mean lane {lane['mean']*100:.0f}%); "
                   f"raising the bonus W_BIKELANE={engine.W_BIKELANE} would match that.")

    wind = profile.get("wind")
    if wind:
        ms, share = wind["mean_score"], wind["into_wind_share"]
        if ms >= 0.15 or share >= 0.5:
            out.append(f"Your real rides lean into the wind first ({share*100:.0f}% of "
                       f"rides, mean wind score {ms:+.2f}) — the wind premise holds; "
                       f"keeping the road-ride wind weight (w_wind=1.0) is supported.")
        elif ms <= 0.05 and share <= 0.3:
            out.append(f"Wind doesn't visibly drive your real routes (only "
                       f"{share*100:.0f}% into-wind-first, mean score {ms:+.2f}); the "
                       f"wind weight may be overstated relative to direction/surface — "
                       f"or you pick routes by other factors (scenery, roads) the model "
                       f"doesn't capture.")

    doms = profile.get("dominant_sectors") or []
    if doms:
        out.append(f"You strongly favour riding {', '.join(doms)}. The optimizer aims "
                   f"into the wind regardless — this is the case for the "
                   f"preferred-direction bias noted in PROJECT_CONTEXT.md.")
    return out
