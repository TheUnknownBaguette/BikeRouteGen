"""Offline tests: turn cues from road names, and the TCX course that carries them.

Run:  python tests/test_cues.py
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import cues, routing

NS = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}


def _street(lat0, lng0, dlat, dlng, n):
    return [(lat0 + dlat * k, lng0 + dlng * k) for k in range(n)]


def _l_route():
    """North 1 km on Main St, then a right turn east 1 km on Oak Ave."""
    north = _street(41.50, -87.85, 0.0009, 0.0, 11)       # ~100 m steps
    east = _street(north[-1][0], north[-1][1], 0.0, 0.0012, 11)[1:]
    coords = north + east
    names = ["Main St"] * 10 + ["Oak Ave"] * (len(coords) - 10)
    return coords, names


def test_right_turn_onto_new_road():
    coords, names = _l_route()
    cl = cues.make_cues(coords, names)
    assert len(cl) == 1
    assert cl[0]["text"] == "Turn right onto Oak Ave" and cl[0]["type"] == "Right"
    assert cl[0]["i"] == 10 and abs(cl[0]["km"] - 1.0) < 0.05


def test_reversed_leg_turns_the_other_way_at_the_same_corner():
    """An out-and-back's return: the same corner, now a left onto Main St."""
    coords, names = _l_route()
    full = coords + coords[-2::-1]
    full_names = names[:-1] + routing._reversed_names(names)
    assert len(full_names) == len(full)
    cl = cues.make_cues(full, full_names)
    texts = [c["text"] for c in cl]
    assert texts == ["Turn right onto Oak Ave", "Turn around on Oak Ave", "Turn left onto Main St"]
    back = cl[-1]
    assert full[back["i"]] == coords[10]                     # exactly the corner


def test_name_blip_is_ignored():
    coords = _street(41.50, -87.85, 0.00018, 0.0, 51)        # 1 km north, ~20 m steps
    names = ["Main St"] * 51
    names[20] = "Driveway"                                   # a 20 m mapping hiccup
    assert cues.make_cues(coords, names) == []


def test_tcx_is_valid_and_carries_the_cues():
    coords, names = _l_route()
    cl = cues.make_cues(coords, names)
    data = cues.tcx_bytes(coords, [200.0] * len(coords), cl, name="Oct 8 & ride")
    root = ET.fromstring(data)
    course = root.find("t:Courses/t:Course", NS)
    assert course.find("t:Name", NS).text == "Oct 8 & ride"
    assert len(course.findall("t:Track/t:Trackpoint", NS)) == len(coords)
    cps = course.findall("t:CoursePoint", NS)
    assert len(cps) == 1
    assert cps[0].find("t:PointType", NS).text == "Right"
    assert cps[0].find("t:Notes", NS).text == "Turn right onto Oak Ave"
    assert len(cps[0].find("t:Name", NS).text) <= 10


def test_step_names_from_ors_steps():
    props = {"segments": [{"steps": [
        {"name": "Main St", "way_points": [0, 3]},
        {"name": "-", "way_points": [3, 5]},
        {"name": "Oak Ave", "way_points": [5, 6]},
        {"name": "", "way_points": [6, 6]}]}]}
    assert routing._step_names(props, 7) == ["Main St"] * 3 + ["", ""] + ["Oak Ave"] * 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
