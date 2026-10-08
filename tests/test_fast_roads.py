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
    surface.clear_fast_cache()                     # each test sees only its own stub
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
    surface.clear_fast_cache()
    c = engine.Candidate(coords=GOUGAR, distance_km=1.1, ascent_m=0, paved_frac=1,
                         unpaved_frac=0, road_names=["Gougar Road"] * len(GOUGAR))
    try:
        note, src = planner._apply_fast_roads([c])
    finally:
        surface.overpass_json = saved
    assert src is None and c.fast_road_frac == 0.0 and "couldn't check" in note


def test_lookup_is_cached_for_the_area():
    calls = []
    restore = _stub_overpass([_gougar_way(maxspeed="55 mph")])
    real = surface.overpass_json
    surface.overpass_json = lambda *a, **k: calls.append(1) or real(*a, **k)
    try:
        a = surface.fast_roads_near(41.505, -87.90, 5.0)
        b = surface.fast_roads_near(41.506, -87.90, 4.0)      # replan, inside the same area
        c = surface.cached_fast_roads(41.501, -87.901, 41.509, -87.899)
    finally:
        restore()
        surface.clear_fast_cache()
    assert len(calls) == 1 and a is b is c


def test_a_slow_lookup_does_not_hold_up_the_plan():
    import concurrent.futures
    import threading
    import time
    gate = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    job = pool.submit(gate.wait, 5)                   # a lookup that hasn't come back
    c = engine.Candidate(coords=GOUGAR, distance_km=1.1, ascent_m=0, paved_frac=1,
                         unpaved_frac=0, road_names=["Gougar Road"] * len(GOUGAR))
    saved = planner.FAST_WAIT_S
    planner.FAST_WAIT_S = 0.2
    t = time.monotonic()
    try:
        note, src = planner._apply_fast_roads([c], job)
    finally:
        planner.FAST_WAIT_S = saved
        gate.set()
        pool.shutdown()
    assert time.monotonic() - t < 1.0
    assert src is None and c.fast_road_frac == 0.0 and "still loading" in note


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
