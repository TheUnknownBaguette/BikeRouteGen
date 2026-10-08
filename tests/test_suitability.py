"""Offline tests: ORS bike-suitability -> poor_road_frac, and its scoring term.

Run:  python tests/test_suitability.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import engine, routing

# 10 equal ~111 m segments due north
COORDS = [(41.50 + 0.001 * k, -87.85) for k in range(11)]


def test_poor_counts_only_low_rated_non_state_roads():
    extras = {
        # segs 0-3 rated 8, 4-7 rated 5, 8-9 rated 4
        "suitability": {"values": [[0, 4, 8], [4, 8, 5], [8, 10, 4]]},
        # segs 6-9 are a State Road (already in busy_frac)
        "waytype": {"values": [[0, 6, 3], [6, 10, 1]]},
    }
    # poor = segs 4-9 (6), minus the State Road ones 6-9 -> segs 4-5 = 20%
    assert abs(routing._poor_fraction(extras, COORDS) - 0.2) < 1e-6


def test_no_suitability_data_means_no_penalty():
    assert routing._poor_fraction({"waytype": {"values": [[0, 10, 3]]}}, COORDS) == 0.0
    assert routing._poor_fraction({}, COORDS) == 0.0


def test_county_roads_rated_7_are_not_poor():
    extras = {"suitability": {"values": [[0, 10, 7]]}}
    assert routing._poor_fraction(extras, COORDS) == 0.0


def _cand(poor):
    return engine.Candidate(coords=[(41.5, -87.85), (41.51, -87.85), (41.51, -87.86),
                                    (41.5, -87.85)],
                            distance_km=30.0, ascent_m=0.0, paved_frac=1.0,
                            unpaved_frac=0.0, poor_road_frac=poor)


def test_scoring_penalizes_poor_roads_beyond_the_free_band():
    wind = engine.Wind(direction_from_deg=0.0, speed_mph=0.0, gust_mph=0.0,
                       valid_time="2026-10-08T08:00", known=False)
    quiet, small, arterial = _cand(0.0), _cand(0.015), _cand(0.30)
    ranked = engine.evaluate([arterial, small, quiet], wind, "road", 30.0, 3.0)
    assert ranked[-1] is arterial
    assert abs(quiet.total_score - small.total_score) < 1e-9       # inside free band
    gap = quiet.total_score - arterial.total_score
    assert abs(gap - engine.W_POOR * (0.30 - engine.POOR_FREE_FRAC)) < 1e-9


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
