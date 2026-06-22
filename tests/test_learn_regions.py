"""Offline tests for region-aware tuning validation (work-plan Task 8).

No network: trips are built with synthetic coords (trip_features with surf=None,
do_wind=False). Run:  python tests/test_learn_regions.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import learn


def _trip(latc, lngc):
    coords = [(latc, lngc), (latc + 0.02, lngc), (latc + 0.02, lngc + 0.02),
              (latc, lngc + 0.02), (latc, lngc)]
    return learn.trip_features(coords, do_wind=False)


def test_trip_features_carries_start():
    f = _trip(41.52, -87.85)
    assert f["start"] == (41.52, -87.85)


def test_cluster_trips_splits_regions():
    feats = [_trip(41.52, -87.85) for _ in range(3)] + [_trip(44.0, -72.5)]  # IL x3, VT x1
    clusters = learn.cluster_trips(feats)
    assert len(clusters) == 2
    assert clusters[0]["n"] == 3            # most-trips cluster first (home)
    assert clusters[1]["n"] == 1
    assert learn._haversine_km(clusters[0]["center"], (41.52, -87.85)) < 1.0


def test_cluster_trips_one_region():
    feats = [_trip(41.52, -87.85) for _ in range(5)]
    clusters = learn.cluster_trips(feats)
    assert len(clusters) == 1 and clusters[0]["n"] == 5


def test_cluster_profiles_shape():
    feats = [_trip(41.52, -87.85) for _ in range(3)] + [_trip(44.0, -72.5)]
    profs = learn.cluster_profiles(feats)
    assert len(profs) == 2
    assert profs[0]["n"] == 3
    assert "distance_mi" in profs[0]["profile"]


def test_region_mismatch_note():
    assert learn.region_mismatch_note("grid-farmland", "mountain")
    assert "mountain" in learn.region_mismatch_note("grid-farmland", "mountain")
    assert learn.region_mismatch_note("grid-farmland", "grid-farmland") is None
    assert learn.region_mismatch_note("unknown", "mountain") is None
    assert learn.region_mismatch_note("grid-farmland", "unknown") is None
    assert learn.region_mismatch_note(None, "mountain") is None
    assert learn.region_mismatch_note("grid-farmland", None) is None


def test_save_load_training_region_roundtrip():
    p = os.path.join(tempfile.gettempdir(), "windroute_test_region.json")
    if os.path.exists(p):
        os.remove(p)
    learn.save_training_region("grid-farmland", (41.5, -87.8), 3,
                               clusters=[{"archetype": "grid-farmland",
                                          "center": [41.5, -87.8], "n": 3}], path=p)
    data = learn.load_training_region(path=p)
    assert data["training_archetype"] == "grid-farmland"
    assert data["n_trips"] == 3 and data["center"] == [41.5, -87.8]
    os.remove(p)
    assert learn.load_training_region(path=p) is None    # absent -> None, no raise


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
            print(f"  FAIL  {t.__name__}: {exc}")
        except Exception as exc:                              # pragma: no cover
            failures += 1
            print(f"  ERROR {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
