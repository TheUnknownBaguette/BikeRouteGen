"""Offline tests: cutting "ride in and straight back out" spurs from a route.

Run:  python tests/test_spurs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import routing
from windroute.geometry import _polyline_km


def _line(lat, lng, dlat, dlng, n):
    return [(lat + dlat * k, lng + dlng * k) for k in range(1, n + 1)]


def test_dead_end_with_turning_circle_is_cut():
    """East along Main, a 300 m side street north into a cul-de-sac bulb and back
    (the way back is offset ~8 m, so it's not the same points), then on east."""
    main1 = [(41.5, -87.85)] + _line(41.5, -87.85, 0.0, 0.001, 5)          # ~420 m
    corner = main1[-1]
    up = _line(corner[0], corner[1], 0.0009, 0.0, 3)                         # ~300 m north
    bulb = [(up[-1][0] + 0.0001, up[-1][1] + 0.0001), (up[-1][0] + 0.0001, up[-1][1] - 0.0001)]
    down = [(p[0], p[1] - 0.0001) for p in reversed(up)]                      # back, 8 m west
    main2 = _line(corner[0], corner[1], 0.0, 0.001, 5)
    coords = main1 + up + bulb + down + [corner] + main2      # back at the same corner node
    names = (["Main"] * len(main1) + ["Dead End Ct"] * (len(up) + len(bulb) + len(down))
             + ["Main"] * (1 + len(main2)))
    names[len(main1) - 1] = "Dead End Ct"            # leaving the corner, onto the side street
    eles = [200.0] * len(coords)
    out, out_e, out_n, removed = routing._trim_spurs(coords, eles, names)
    assert 0.55 < removed < 0.75                      # ~2 x 300 m
    assert len(out) == len(out_e) == len(out_n)
    assert "Dead End Ct" not in out_n
    assert abs(_polyline_km(out) - (_polyline_km(coords) - removed)) < 1e-6


def test_lollipop_junction_retrace_is_cut():
    """Stem comes north up Gougar; the candy starts by going 500 m back south on it."""
    stem = [(41.5, -87.9)] + _line(41.5, -87.9, 0.001, 0.0, 10)
    back = list(reversed(stem[-6:-1]))                # 5 pts back down the stem road
    candy = back + _line(back[-1][0], back[-1][1], 0.0, 0.002, 5)
    coords = stem + candy
    out, _, _, removed = routing._trim_spurs(coords)
    assert removed > 0.8                              # ~2 x 0.5 km
    assert out[len(stem) - 6] == stem[-6]             # cut back to where the candy turns


def test_u_turn_on_a_divided_road_is_cut():
    """East 400 m on one carriageway, back on the other one 40 m south."""
    west = [(41.5, -87.9)] + _line(41.5, -87.9, 0.0, 0.001, 4)
    east = _line(41.5, west[-1][1], 0.0, 0.0012, 4)
    back = [(41.49964, p[1]) for p in reversed(east[:-1])]           # 40 m south
    on = [(41.49964, west[-1][1])] + _line(41.49964, west[-1][1], -0.001, 0.0, 5)
    coords = west + east + back + on
    out, _, _, removed = routing._trim_spurs(coords)
    assert removed > 0.7                              # ~2 x 400 m, less the 40 m hop
    assert len(out) == len(west) + len(on)


def test_plain_loop_and_tiny_jitter_untouched():
    sq = ([(41.5, -87.9)] + _line(41.5, -87.9, 0.0, 0.001, 10)
          + _line(41.5, -87.89, 0.001, 0.0, 10) + _line(41.51, -87.89, 0.0, -0.001, 10)
          + _line(41.51, -87.9, -0.001, 0.0, 10))
    out, _, _, removed = routing._trim_spurs(sq)
    assert removed == 0 and out == sq
    jitter = sq[:5] + [(sq[4][0] + 0.0001, sq[4][1])] + sq[4:]      # an 11 m poke out and back
    out, _, _, removed = routing._trim_spurs(jitter)
    assert removed == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
