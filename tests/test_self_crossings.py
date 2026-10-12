"""Offline tests: counting real self-crossings, not touches.

Run:  python tests/test_self_crossings.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute.geometry import _self_intersections as crossings

ROAD = [(0.0, x / 10) for x in range(11)]


def test_two_stretches_crossing_in_the_open():
    assert crossings([(0, 0), (1, 1), (1, 0.5), (0, 1)]) == 1


def test_crossing_at_a_shared_intersection_point():
    assert crossings([(0, 0), (0.5, 0.5), (1, 1), (1, 0), (0.5, 0.5), (0, 1), (0, 0)]) == 1


def test_riding_back_along_the_same_road_is_not_a_crossing():
    assert crossings(ROAD + ROAD[-2::-1]) == 0


def test_touching_and_turning_back_is_not_a_crossing():
    assert crossings([(0, 0), (0, 1), (0, 2), (1, 2), (0, 1), (0.5, 0)]) == 0


def test_sharing_a_stretch_then_leaving_on_the_other_side_is_one_crossing():
    out = [(0, 0), (0, 1), (0, 2), (0, 3), (1, 3), (1, 1)]
    assert crossings(out + [(0, 1), (0, 2), (-1, 2)]) == 1    # in from the north, out south
    assert crossings(out + [(0, 1), (0, 2), (1, 2)]) == 0     # in and out on the north


def test_joining_the_way_out_to_ride_home_is_not_a_crossing():
    # back on a side path beside the outbound road, merging onto it for home
    out = [(0, 0), (0, 1), (0, 2), (0, 3), (-1, 3), (-1, 2.5)]
    home = [(-0.01, 2.2), (0, 2), (0, 1), (0, 0)]
    assert crossings(out + home) == 0


def test_a_repeated_point_within_one_pass_is_not_a_crossing():
    assert crossings([(0, 0), (0, 1), (0, 1.5), (0, 1.5), (0, 2), (0, 3)]) == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
