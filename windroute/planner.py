"""Shared planning pipeline — the orchestration every front-end calls.

`plan_routes` runs the full sequence the CLI used to inline (geocode -> wind ->
ride-area staging -> generate candidates -> surface refine -> corrections ->
evaluate -> pick options) and returns a `PlanResult` with NO printing and NO file
writing. The CLI and the web app each call it and present the result their own way,
so the logic lives in exactly one place (the project's design rule: front-ends are
thin layers over engine + render, never reimplementing the pipeline).

Status messages that the pipeline used to print (surface refine, corrections,
ride-area outcome) are returned as plain strings in `PlanResult.notes` for the
front-end to display however it likes.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from dateutil import parser as dateparser

from . import engine, surface, zones, regions
from .corrections import CorrectionCache

SURFACE_DISAGREE = 0.10   # |ORS unpaved - OSM unpaved| above this -> flag a route


@dataclass
class PlanResult:
    """Everything a front-end needs to present a plan, computed, no I/O done."""
    location_label: str
    when: dt.datetime
    wind: engine.Wind
    zone: dict | None
    ranked: list                 # list[engine.Candidate], best-first
    options: list                # list[engine.RouteOption], recommendation first
    notes: list = field(default_factory=list)   # surface/corrections/ride-area status lines
    surface_mode: str = "ors"    # "ors" | "osm" | "both"
    region: "regions.RegionProfile | None" = None   # terrain archetype (when classify=True)


def plan_routes(location, distance, unit="mi", start="now", ride_type="road",
                shapes=("loop", "lollipop", "rectangle"), surface_source="ors",
                ride_area=None, tolerance=3.0, candidates=12, corrections=True,
                corrections_file=None, api_key=None, n_alternatives=2,
                location_label=None, classify=False) -> PlanResult:
    """Run the full planning pipeline and return a `PlanResult` (no printing/files).

    `shapes` may be a comma string ("loop,rectangle") or a sequence. `start` is
    "now" or a parseable date string. Raises on hard failures (bad location, no
    routes, missing API key) for the front-end to surface.
    """
    ride_type = ride_type.lower().strip()
    if isinstance(shapes, str):
        shape_list = [s.strip().lower() for s in shapes.split(",") if s.strip()]
    else:
        shape_list = [str(s).strip().lower() for s in shapes if str(s).strip()]
    to_km = 1.609344 if unit.lower().startswith("mi") else 1.0
    target_km = distance * to_km
    tolerance_km = tolerance * to_km
    when = (dt.datetime.now().replace(minute=0, second=0, microsecond=0)
            if str(start).lower() == "now" else dateparser.parse(start))

    notes: list = []
    lat, lng, label = engine.geocode(location)
    if location_label:                    # caller picked an exact point; keep its name
        label = location_label
    wind = engine.get_wind(lat, lng, when)

    # Step 0 (Task 1): classify the surrounding terrain so later steps can adapt.
    # Off by default and DELIBERATELY does not feed scoring/zone weights yet — that
    # wiring is Task 2, gated so grid-farmland stays byte-identical. For now it only
    # computes + surfaces the archetype (a note + PlanResult.region).
    region = None
    archetype = None
    if classify:
        region = regions.classify_region((lat, lng))   # structured field; front-ends render it
        archetype = region.archetype

    # Archetype-keyed tuning (Task 2): route-score weights, loop geometry, and the
    # default shape set adapt to the terrain. When classify is off, archetype is
    # None and every *_for(None) returns the grid-farmland baseline, so behaviour
    # (and the grid-farmland row) is byte-identical to before.
    weights = engine.weights_for(archetype, ride_type)
    loop_geom = engine.loop_geom_for(archetype)
    shape_list = engine.shapes_for(archetype, shape_list)
    if archetype and archetype not in ("grid-farmland", "unknown"):
        notes.append(f"terrain: tuning for {archetype} "
                     f"(shapes: {', '.join(shape_list)})")

    zone = None
    if ride_area:
        zone, note = _resolve_ride_area(ride_area, lat, lng, target_km, archetype)
        if note:
            notes.append(note)
        if zone:
            shape_list = shape_list + ["staging"]

    cands = engine.generate_candidates(
        lat, lng, target_km, ride_type, api_key, n=candidates,
        shapes=shape_list, into_wind_bearing=wind.into_wind_bearing, zone=zone,
        loop_geom=loop_geom)

    mode = surface_source.lower()
    if mode == "osm":
        notes.append(_apply_osm_surface(cands))
    elif mode == "both":
        notes.append(_compare_surface(cands))

    if corrections:
        note = _apply_corrections(cands, corrections_file)
        if note:
            notes.append(note)

    # Volume-first busy reframe (Task 4a): when adapting to terrain, normalize the
    # arterial penalty against the corridor's *unavoidable* arterial level (the
    # quietest candidate's busy fraction), so a region whose quietest roads are
    # still somewhat busy doesn't tank every route. Off (0.0) by default ->
    # absolute penalty, byte-identical.
    busy_baseline = 0.0
    if classify and cands:
        busy_baseline = min(c.busy_frac for c in cands)
        if busy_baseline > engine.BUSY_FREE_FRAC:
            notes.append(f"busy: arterials look hard to avoid here "
                         f"(~{busy_baseline * 100:.0f}% unavoidable); scoring the "
                         f"quietest available rather than penalizing all routes.")

    ranked = engine.evaluate(cands, wind, ride_type, target_km, tolerance_km,
                             weights=weights, busy_baseline=busy_baseline)
    options = engine.select_route_options(ranked, wind, ride_type, target_km,
                                          n_alternatives=n_alternatives)
    return PlanResult(location_label=label, when=when, wind=wind, zone=zone,
                      ranked=ranked, options=options, notes=notes, surface_mode=mode,
                      region=region)


# --------------------------------------------------------------------------- #
# Pipeline steps (moved from cli.py; return note strings instead of printing)
# --------------------------------------------------------------------------- #
def _resolve_ride_area(ride_area, lat, lng, target_km, archetype=None):
    """Turn the --ride-area value into a staging zone dict + a status note.

    'auto' auto-detects the nearest good quiet riding zone from the start; any
    other value is geocoded and used as a forced zone. Either way the zone is
    rejected if it's so far that the round-trip transit would eat most of the ride
    budget — we want most of the distance spent looping in good country, not
    commuting to it (cap: 2*crow-transit <= 0.6*target). Returns (zone|None, note).
    """
    max_transit_oneway = 0.3 * target_km
    area = ride_area.strip()
    prefer = None if area.lower() == "auto" else engine.parse_compass(area)
    if area.lower() == "auto" or prefer is not None:
        # 'auto' = best zone anywhere; a compass direction = best zone that way.
        zone = zones.find_ride_zone(lat, lng, prefer_bearing=prefer,
                                    archetype=archetype)
        if not zone:
            if prefer is not None:
                return None, (f"ride-area: couldn't find quiet riding to the "
                              f"{engine.compass_label(prefer)} within range — riding "
                              f"from the start.")
            return None, ("ride-area: you're already in good riding country (or "
                          "nothing stands out within range) — riding from the start.")
        bearing = zone["bearing"]
        dist = zone["distance_km"]
    else:
        zlat, zlng, zlabel = engine.geocode(area)
        dist = engine._haversine_km((lat, lng), (zlat, zlng))
        bearing = engine._bearing((lat, lng), (zlat, zlng))
        zone = {"lat": zlat, "lng": zlng, "bearing": bearing,
                "distance_km": dist, "label": zlabel}

    if dist > max_transit_oneway:
        return None, (
            f"ride-area: the {engine.compass_label(bearing)} zone is {dist:.1f} km "
            f"away — too far for a {target_km:.0f} km ride (transit would dominate). "
            f"Riding from the start; try a longer distance to stage there.")

    target_desc = zone.get("label") or f"{engine.compass_label(bearing)} ({bearing:.0f}°)"
    return zone, (f"ride-area: staging toward {target_desc}, ~{dist:.1f} km out; "
                  f"the destination loop is wind-scored.")


def _apply_osm_surface(cands):
    """Override each candidate's paved/unpaved fractions with OSM/Overpass data.

    One Overpass query covers all candidates. On any failure we leave the ORS
    baseline untouched and say so, rather than aborting the whole plan.
    """
    try:
        src = surface.OverpassSurface().build([c.coords for c in cands])
    except Exception as exc:                              # network / Overpass down
        return f"surface: OSM lookup failed ({exc}); kept ORS surface"
    if not src.way_count and not src.bikelane_count:
        return "surface: no OSM surface tags in this area; kept ORS surface"
    refined = lanes = bad = 0
    for c in cands:
        res = src.classify(c.coords)
        if res:
            c.paved_frac, c.unpaved_frac = res
            refined += 1
        lane = src.classify_bikelane(c.coords)
        if lane is not None:
            c.bikelane_frac = lane
            if lane > 0:
                lanes += 1
        qual = src.classify_quality(c.coords)
        if qual is not None:
            c.good_gravel_frac, c.unrideable_frac = qual
            if c.unrideable_frac > 0:
                bad += 1
    note = (f"surface: OSM/Overpass ({src.way_count} tagged ways, "
            f"{refined}/{len(cands)} loops refined; "
            f"{src.bikelane_count} bike-lane ways, {lanes} routes use one)")
    if src.quality_count:
        note += (f"; quality graded ({src.quality_count} ways, "
                 f"{bad} routes touch unrideable surface)")
    return note


def _compare_surface(cands):
    """Cross-check ORS vs OSM surface for each route.

    Records both readings on every candidate (surface_by_source['ors'/'osm']),
    flags routes where they disagree by more than SURFACE_DISAGREE, and adopts
    the finer OSM value as the one used for scoring. On Overpass failure the ORS
    baseline is kept untouched.
    """
    for c in cands:
        c.surface_by_source["ors"] = c.unpaved_frac     # current value is ORS baseline
    try:
        src = surface.OverpassSurface().build([c.coords for c in cands])
    except Exception as exc:                             # network / Overpass down
        return f"surface cross-check: OSM lookup failed ({exc}); kept ORS only"
    if not src.way_count and not src.bikelane_count:
        return "surface cross-check: no OSM surface tags in this area; kept ORS only"

    disagree = 0
    for c in cands:
        lane = src.classify_bikelane(c.coords)
        if lane is not None:
            c.bikelane_frac = lane
        qual = src.classify_quality(c.coords)
        if qual is not None:
            c.good_gravel_frac, c.unrideable_frac = qual
        res = src.classify(c.coords)
        if not res:
            continue
        paved, unpaved = res
        c.surface_by_source["osm"] = unpaved
        c.paved_frac, c.unpaved_frac = paved, unpaved    # OSM is primary for scoring
        if abs(unpaved - c.surface_by_source["ors"]) > SURFACE_DISAGREE:
            disagree += 1
    return (f"surface cross-check: ORS vs OSM over {src.way_count} tagged ways; "
            f"{disagree}/{len(cands)} routes disagree >{SURFACE_DISAGREE*100:.0f}% "
            f"(scoring uses OSM)")


def _apply_corrections(cands, corrections_file):
    """Overlay the personal correction cache on every candidate.

    Returns "" (not nagging) when the cache is empty or none of the routes touch a
    marked road, so a front-end can simply skip an empty note.
    """
    cache = CorrectionCache.load(corrections_file)
    if not cache.records:
        return ""
    cache.build()
    touched = 0
    surf_km = traf_km = 0.0
    for c in cands:
        s, t = cache.apply(c)
        if s or t:
            touched += 1
        surf_km += s
        traf_km += t
    if not touched:
        return (f"corrections: {len(cache.records)} on file, "
                f"none on these routes")
    return (f"corrections: applied {len(cache.records)} personal note(s) - "
            f"{touched}/{len(cands)} routes adjusted "
            f"({surf_km:.1f} km surface, {traf_km:.1f} km traffic)")
