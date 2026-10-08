"""Offline tests: fast / multi-lane roads from OSM speed limits and lane counts.

No network: surface.overpass_json is stubbed.
Run:  python tests/test_fast_roads.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import engine, planner, surface


def test_maxspeed_units():
    assert surface.maxspeed_mph("55 mph") == 55
    assert abs(surface.maxspeed_mph("80") - 49.7) < 0.1          # km/h is OSM's default
    assert abs(surface.maxspeed_mph("50 km/h") - 31.1) < 0.1
    assert surface.maxspeed_mph("signals") is None
    assert surface.maxspeed_mph(None) is None


def test_what_counts_as_fast():
    assert surface.is_fast_road({"highway": "tertiary", "maxspeed": "55 mph"})
    assert surface.is_fast_road({"highway": "secondary", "lanes": "4"})
    assert not surface.is_fast_road({"highway": "tertiary", "maxspeed": "40 mph"})   # Lincoln Ave
    assert not surface.is_fast_road({"highway": "cycleway", "maxspeed": "55 mph"})


# Gougar Rd running north, mapped as a 55 mph road; a side path 10 m east of it.
GOUGAR = [(41.50 + 0.001 * k, -87.90) for k in range(11)]
SIDEPATH = [(lat, -87.89988) for lat, _ in GOUGAR]


def _stub_overpass(elements):
    saved = surface.overpass_json
    surface.overpass_json = lambda *a, **k: elements
    return lambda: setattr(surface, "overpass_json", saved)


def _gougar_way(**tags):
    return {"tags": {"highway": "tertiary", "name": "Gougar Road", "ref": "CH 52", **tags},
            "geometry": [{"lat": a, "lon": b} for a, b in GOUGAR]}


def test_riding_on_the_road_counts_but_the_side_path_does_not():
    restore = _stub_overpass([_gougar_way(maxspeed="55 mph")])
    try:
        src = surface.FastRoads().build([GOUGAR])
    finally:
        restore()
    on_road = src.fraction(GOUGAR, ["Gougar Road, CH 52"] * len(GOUGAR))
    beside = src.fraction(SIDEPATH, [""] * len(SIDEPATH))
    other_name = src.fraction(SIDEPATH, ["Old Plank Road Trail"] * len(SIDEPATH))
    assert on_road > 0.99 and beside == 0.0 and other_name == 0.0


def test_planner_keeps_only_the_part_not_already_charged():
    restore = _stub_overpass([_gougar_way(maxspeed="55 mph")])
    c = engine.Candidate(coords=GOUGAR, distance_km=1.1, ascent_m=0, paved_frac=1,
                         unpaved_frac=0, busy_frac=0.3, poor_road_frac=0.1,
                         road_names=["Gougar Road, CH 52"] * len(GOUGAR))
    try:
        note, src = planner._apply_fast_roads([c])
    finally:
        restore()
    assert note == "" and src is not None
    assert abs(c.fast_road_frac - 0.6) < 0.01                    # 100% - 30% - 10%


def test_overpass_failure_leaves_ranking_alone():
    saved = surface.overpass_json

    def down(*a, **k):
        raise RuntimeError("504")
    surface.overpass_json = down
    c = engine.Candidate(coords=GOUGAR, distance_km=1.1, ascent_m=0, paved_frac=1,
                         unpaved_frac=0, road_names=["Gougar Road"] * len(GOUGAR))
    try:
        note, src = planner._apply_fast_roads([c])
    finally:
        surface.overpass_json = saved
    assert src is None and c.fast_road_frac == 0.0 and "couldn't check" in note


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
