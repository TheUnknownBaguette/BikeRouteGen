"""Offline test for the ORS-call counter (CODE_HEALTH Task C2).

Stubs requests.post with a minimal ORS GeoJSON response and asserts that one
_ors_directions call bumps the process tally by exactly one, and that the tally is
reachable via the engine facade.

Run:  python tests/test_observability.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import engine, routing


class _FakeResp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"features": [{
            "properties": {"summary": {"distance": 1000.0}, "extras": {}},
            "geometry": {"coordinates": [[-87.85, 41.50, 200.0],
                                         [-87.85, 41.52, 201.0],
                                         [-87.87, 41.52, 202.0]]},
        }]}


def test_ors_call_counter_increments_once():
    saved = routing.requests.post
    routing.requests.post = lambda *a, **k: _FakeResp()
    try:
        before = engine.ors_call_total()
        routing._ors_directions("k", "cycling-regular",
                                [[-87.85, 41.50], [-87.87, 41.52]], timeout=5)
        after = engine.ors_call_total()
    finally:
        routing.requests.post = saved
    assert after - before == 1, f"expected +1 ORS call, got +{after - before}"


def test_ors_call_total_exposed_on_facade():
    assert engine.ors_call_total is routing.ors_call_total   # facade re-exports it
    assert isinstance(engine.ors_call_total(), int)


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
