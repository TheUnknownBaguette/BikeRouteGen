"""Offline tests: turn penalty and bike-marking bonuses (painted lanes on busy roads,
sharrows anywhere).

No network: surface.overpass_json is stubbed.
Run:  python tests/test_turns_and_markings.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import engine, planner, routing, surface
from windroute.models import Wind
from windroute.scoring import evaluate

CALM = Wind(0, 0, 0, "", known=False)


def test_markings_from_tags():
    m = surface.bike_markings
    assert m({"cycleway": "lane"}) == (True, False)
    assert m({"cycleway:right": "lane", "cycleway:left": "shared_lane"}) == (True, True)
    assert m({"cycleway": "shared_lane"}) == (False, True)
    assert m({"cycleway": "no"}) == (False, False)


# a road running north
ROAD = [(40.10 + 0.001 * k, -88.30) for k in range(11)]


def _charged(tags, name, suit, waytype=2):
    surface.clear_fast_cache()
    saved = surface.overpass_json
    surface.overpass_json = lambda *a, **k: [{
        "tags": {"name": name, **tags},
        "geometry": [{"lat": a, "lon": b} for a, b in ROAD]}]
    c = engine.Candidate(coords=ROAD, distance_km=1.1, ascent_m=0, paved_frac=1,
                         unpaved_frac=0, poor_road_frac=1.0 if suit <= 5 else 0.0,
                         road_names=[routing.RoadName(name, waytype, suit)] * len(ROAD))
    try:
        planner._apply_fast_roads([c])
    finally:
        surface.overpass_json = saved
    return c


def test_lane_on_a_busy_road_is_not_poor_and_earns_the_bonus():
    # Bradley Ave in town: secondary, painted lane, ORS rates it 5/10
    c = _charged({"highway": "secondary", "cycleway": "lane", "maxspeed": "35 mph"},
                 "West Bradley Avenue", 5)
    assert c.poor_road_frac == 0.0 and c.busy_lane_frac > 0.99


def test_lane_on_a_quiet_road_earns_nothing():
    c = _charged({"highway": "residential", "cycleway": "lane"}, "Elm Street", 8)
    assert c.busy_lane_frac == 0.0 and c.sharrow_frac == 0.0


def test_sharrows_earn_the_bonus_on_any_road():
    c = _charged({"highway": "tertiary", "cycleway": "shared_lane"}, "West John Street", 8)
    assert c.sharrow_frac > 0.99 and c.busy_lane_frac == 0.0


def test_busy_road_without_a_lane_is_still_poor():
    c = _charged({"highway": "secondary", "maxspeed": "35 mph", "lanes": "4"},
                 "South Duncan Road", 5)
    assert c.poor_road_frac > 0.99 and c.busy_lane_frac == 0.0


def _zigzag(n_turns, km=10.0):
    """A route heading east that steps north-east-north-east... `n_turns` times."""
    pts, lat, lng = [(40.0, -88.0)], 40.0, -88.0
    step = km / (n_turns + 1) / 111.0
    names = ["Road 0"]
    for k in range(n_turns + 1):
        if k % 2 == 0:
            lng += step / 0.766
        else:
            lat += step
        pts.append((lat, lng))
        names.append(f"Road {k + 1}")
    return engine.Candidate(coords=pts, distance_km=km, ascent_m=0, paved_frac=1,
                            unpaved_frac=0, road_names=names)


def test_turns_beyond_one_per_mile_cost_points():
    few, many = _zigzag(4), _zigzag(16)               # 10 km: ~0.6/mi vs ~2.6/mi
    evaluate([few, many], CALM, "road", 10.0)
    assert few.turns < many.turns
    assert few.total_score > many.total_score


def test_marking_bonus_is_a_nudge():
    plain = _zigzag(4)
    marked = _zigzag(4)
    marked.sharrow_frac = 0.10
    evaluate([plain, marked], CALM, "road", 10.0)
    gain = marked.total_score - plain.total_score
    assert 0.0 < gain <= 0.05


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
