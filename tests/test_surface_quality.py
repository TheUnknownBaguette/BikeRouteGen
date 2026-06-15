"""Offline tests for gravel quality tagging (work-plan Task 3c).

Pure tag -> bucket mapping, no network. Run:  python tests/test_surface_quality.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute.surface import classify_quality_tags


def test_good_gravel_surfaces():
    assert classify_quality_tags({"surface": "fine_gravel"}) == "good"
    assert classify_quality_tags({"surface": "compacted"}) == "good"


def test_good_gravel_tracktype():
    assert classify_quality_tags({"tracktype": "grade2"}) == "good"
    assert classify_quality_tags({"tracktype": "grade3"}) == "good"


def test_unrideable_surfaces():
    assert classify_quality_tags({"surface": "mud"}) == "bad"
    assert classify_quality_tags({"surface": "ground"}) == "bad"
    assert classify_quality_tags({"surface": "sand"}) == "bad"


def test_unrideable_tracktype_and_smoothness():
    assert classify_quality_tags({"tracktype": "grade5"}) == "bad"
    assert classify_quality_tags({"smoothness": "very_horrible"}) == "bad"


def test_bad_beats_good():
    # a way tagged both nice-surface and awful-smoothness reads as bad (safety)
    assert classify_quality_tags({"surface": "compacted",
                                  "smoothness": "impassable"}) == "bad"


def test_none_for_plain_or_paved():
    assert classify_quality_tags({"surface": "asphalt"}) is None
    assert classify_quality_tags({"highway": "residential"}) is None
    assert classify_quality_tags({"tracktype": "grade1"}) is None   # solid, not graded gravel


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
