"""Offline tests for the pure region classifier (`regions.classify_archetype`).

No network: every case feeds a synthetic feature vector and checks the label.
Run from the project root:  python tests/test_regions.py
(Also discoverable by pytest if it's installed.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import regions
from windroute.regions import classify_archetype


def _feat(**over):
    """A baseline 'enough data, featureless' vector, overridden per case."""
    base = {
        "n_elements": 200,
        "farmland_frac": 0.0,
        "forest_frac": 0.0,
        "residential_frac": 0.0,
        "water_frac": 0.0,
        "road_density": 2.0,
        "arterial_frac": 0.1,
        "grid_frac": 0.9,
        "coastline_km": 0.0,
        "relief_range_m": 40.0,
        "relief_std_m": 8.0,
    }
    base.update(over)
    return base


def test_grid_farmland():
    a, c = classify_archetype(_feat(farmland_frac=0.55, relief_std_m=6.0))
    assert a == "grid-farmland", a
    assert c > 0.5


def test_mountain_by_std():
    a, _ = classify_archetype(_feat(relief_std_m=180.0, relief_range_m=900.0,
                                    forest_frac=0.4))
    assert a == "mountain", a


def test_mountain_by_range():
    a, _ = classify_archetype(_feat(relief_std_m=90.0, relief_range_m=650.0))
    assert a == "mountain", a


def test_suburban_sprawl():
    a, _ = classify_archetype(_feat(residential_frac=0.45, road_density=6.0,
                                    relief_std_m=5.0))
    assert a == "suburban-sprawl", a


def test_coastal():
    a, _ = classify_archetype(_feat(coastline_km=3.5, water_frac=0.2,
                                    residential_frac=0.2, road_density=3.0))
    assert a == "coastal", a


def test_forested_rolling():
    a, _ = classify_archetype(_feat(forest_frac=0.5, relief_std_m=45.0))
    assert a == "forested-rolling", a


def test_arid_open():
    a, _ = classify_archetype(_feat(road_density=0.4, relief_std_m=20.0))
    assert a == "arid-open", a


def test_unknown_thin_data():
    a, c = classify_archetype(_feat(n_elements=5, farmland_frac=0.9))
    assert a == "unknown", a
    assert c < 0.4


def test_unknown_indecisive_falls_back():
    # enough data, but nothing clears a threshold -> unknown, flagged low-ish
    a, c = classify_archetype(_feat(farmland_frac=0.05, forest_frac=0.05,
                                    residential_frac=0.1, road_density=2.0))
    assert a == "unknown", a


def test_missing_relief_no_mountain():
    # relief unavailable must not crash and must not pick mountain
    a, _ = classify_archetype(_feat(relief_range_m=None, relief_std_m=None,
                                    farmland_frac=0.5))
    assert a == "grid-farmland", a


def test_suburban_outranks_farmland_when_dense():
    # a built-up start with some farmland on the edge should read suburban
    a, _ = classify_archetype(_feat(residential_frac=0.4, road_density=7.0,
                                    farmland_frac=0.25, relief_std_m=5.0))
    assert a == "suburban-sprawl", a


def test_archetype_label_is_known():
    for over in ({}, {"farmland_frac": 0.5}, {"relief_std_m": 200.0}):
        a, _ = classify_archetype(_feat(**over))
        assert a in regions.ARCHETYPES, a


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {t.__name__}: got {exc}")
        except Exception as exc:                              # pragma: no cover
            failures += 1
            print(f"  ERROR {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
