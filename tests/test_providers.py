"""Offline tests for the surface-provider registry + graceful degradation (Task 5).

No network: the Overpass fallback is exercised with a stubbed requests.post.
Run:  python tests/test_providers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import surface, planner


# --- regional provider registry -------------------------------------------- #
class _DummyProvider(surface.SurfaceProvider):
    name = "dummy"

    def __init__(self, bbox):
        self.bbox = bbox          # (s, w, n, e)
        self.ran = False

    def applies_to(self, lat, lng):
        s, w, n, e = self.bbox
        return s <= lat <= n and w <= lng <= e

    def refine(self, cands):
        self.ran = True
        return "applied dummy data"


def test_registry_empty_by_default():
    # Champaign-ish, no providers registered -> only the OSM baseline runs
    assert surface.regional_providers_for(40.5, -88.5) == []


def test_registry_dispatch_by_boundary():
    prov = _DummyProvider((40.0, -89.0, 41.0, -88.0))     # a box over E-central IL
    surface.REGIONAL_SURFACE_PROVIDERS.append(prov)
    try:
        assert prov in surface.regional_providers_for(40.5, -88.5)   # inside
        assert prov not in surface.regional_providers_for(48.0, 7.8)  # Germany, outside
    finally:
        surface.REGIONAL_SURFACE_PROVIDERS.remove(prov)
    # removing a provider changes only its region (and nothing remains registered)
    assert surface.regional_providers_for(40.5, -88.5) == []


# --- Overpass mirror fallback ---------------------------------------------- #
class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_overpass_falls_back_to_next_mirror():
    calls = []
    orig = surface.requests.post

    def fake_post(u, **kw):
        calls.append(u)
        if len(calls) == 1:
            raise surface.requests.RequestException("primary 504")
        return _Resp({"elements": [{"id": 1}]})

    surface.requests.post = fake_post
    try:
        els = surface.overpass_json("q", timeout=1)
        assert els == [{"id": 1}]
        assert len(calls) == 2          # first failed, second answered
    finally:
        surface.requests.post = orig


def test_overpass_raises_only_when_all_fail():
    orig = surface.requests.post

    def fake_post(u, **kw):
        raise surface.requests.RequestException("down")

    surface.requests.post = fake_post
    try:
        raised = False
        try:
            surface.overpass_json("q", timeout=1)
        except surface.requests.RequestException:
            raised = True
        assert raised
    finally:
        surface.requests.post = orig


# --- coverage (data-confidence signal) ------------------------------------- #
def test_coverage_fraction():
    src = surface.OverpassSurface()
    src._grid = {}
    src._add_segment(src._grid, ("paved", (40.0, -88.00), (40.0, -88.02)))
    on = [(40.0, -88.00), (40.0, -88.01), (40.0, -88.02)]   # rides along the tagged way
    off = [(41.0, -87.00), (41.0, -87.01)]                  # nowhere near it
    assert src.coverage(on) == 1.0
    assert src.coverage(off) == 0.0


# --- graceful degradation -------------------------------------------------- #
def test_surface_confidence_levels():
    assert planner._surface_confidence("ors", None)[0] == "ors-baseline"
    assert planner._surface_confidence("ors", None)[1] is None        # no nag
    assert planner._surface_confidence("osm", None)[0] == "low"       # lookup failed
    assert planner._surface_confidence("osm", 0.1)[0] == "low"        # sparse tags
    assert planner._surface_confidence("both", 0.5)[0] == "ok"        # well-covered
    assert planner._surface_confidence("osm", 0.1)[1]                 # carries a user note


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
