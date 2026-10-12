"""Offline tests: numbered state / US routes from OSM refs, charged once per stretch.

No network: surface.overpass_json is stubbed.
Run:  python tests/test_numbered_routes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import engine, planner, routing, surface
from windroute.scoring import evaluate
from windroute.models import Wind


def test_route_tiers_from_refs():
    t = surface.route_tier
    assert t({"highway": "secondary", "ref": "IL 47", "maxspeed": "55 mph"}) == "state"
    assert t({"highway": "secondary", "ref": "IL 10", "maxspeed": "35 mph"}) == "town-state"
    assert t({"highway": "secondary", "ref": "IL 47"}) == "state"      # no limit: not town
    assert t({"highway": "primary", "ref": "US 150", "maxspeed": "35 mph"}) == "national"
    assert t({"highway": "secondary", "ref": "IL 10;US 150"}) == "national"   # strongest wins
    # county numbers are the quiet roads, even when OSM calls them secondary
    assert t({"highway": "secondary", "ref": "CR 6", "maxspeed": "55 mph"}) is None
    assert t({"highway": "secondary", "ref": "CH 1;CR 1000E"}) is None
    assert t({"highway": "residential", "ref": "IL 47"}) is None       # not a main road
    # outside the US the road class decides
    assert t({"highway": "primary", "ref": "A1"}) == "national"
    assert t({"highway": "secondary", "ref": "D 123"}) == "state"


# IL 47 running north at 55 mph, mapped secondary with its route number
IL47 = [(40.10 + 0.001 * k, -88.40) for k in range(11)]


def _stub_overpass(elements):
    surface.clear_fast_cache()
    saved = surface.overpass_json
    surface.overpass_json = lambda *a, **k: elements
    return lambda: setattr(surface, "overpass_json", saved)


def _names(name, waytype, suit, n):
    return [routing.RoadName(name, waytype, suit) for _ in range(n)]


def test_state_route_is_charged_once_not_also_as_poor_or_fast():
    restore = _stub_overpass([{
        "tags": {"highway": "secondary", "name": "County Road 300 E", "ref": "IL 47",
                 "maxspeed": "55 mph"},
        "geometry": [{"lat": a, "lon": b} for a, b in IL47]}])
    # ORS rates it 5/10 (poor) and isn't calling it a State Road
    c = engine.Candidate(coords=IL47, distance_km=1.1, ascent_m=0, paved_frac=1,
                         unpaved_frac=0, poor_road_frac=1.0,
                         road_names=_names("County Road 300 E, IL 47", 2, 5, len(IL47)))
    try:
        note, src = planner._apply_fast_roads([c])
    finally:
        restore()
    assert note == "" and src is not None
    assert c.state_route_frac > 0.99
    assert c.poor_road_frac == 0.0 and c.fast_road_frac == 0.0 and c.busy_frac == 0.0


def test_unnumbered_stretches_keep_their_ors_class():
    restore = _stub_overpass([])
    c = engine.Candidate(coords=IL47, distance_km=1.1, ascent_m=0, paved_frac=1,
                         unpaved_frac=0,
                         road_names=(_names("Duncan Road", 2, 5, 6)
                                     + _names("US Highway", 1, 4, 5)))
    try:
        planner._apply_fast_roads([c])
    finally:
        restore()
    assert abs(c.poor_road_frac - 0.6) < 0.01 and abs(c.busy_frac - 0.4) < 0.01


def test_road_class_survives_pickling():
    import pickle
    n = pickle.loads(pickle.dumps(routing.RoadName("IL 47", 1, 5)))
    assert n == "IL 47" and n.waytype == 1 and n.suit == 5


def _scored(**fracs):
    c = engine.Candidate(coords=[(40.1, -88.4), (40.11, -88.4), (40.1, -88.4)],
                         distance_km=50, ascent_m=0, paved_frac=1, unpaved_frac=0,
                         **fracs)
    evaluate([c], Wind(0, 0, 0, "", known=False), "road", 50)
    return c.total_score


def test_us_routes_cost_more_than_state_routes_and_town_state_least():
    base = _scored()
    us = base - _scored(national_route_frac=0.10)
    state = base - _scored(state_route_frac=0.10)
    town = base - _scored(town_state_route_frac=0.10)
    assert us > state > town > 0
    # a little state route in town is free; the same share outside town is not
    assert _scored(town_state_route_frac=0.04) == base
    assert _scored(state_route_frac=0.04) < base


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
