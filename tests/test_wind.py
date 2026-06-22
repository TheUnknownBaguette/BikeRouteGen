"""Offline tests for wind-biased routing (work-plan Task 7).

No network: ORS is stubbed; the Valhalla seam is checked for its default-off gate +
pure helpers. Run:  python tests/test_wind.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from windroute import engine, valhalla


# --- avoid-corridor builder ------------------------------------------------ #
def test_corridor_excludes_endpoints():
    pts = [engine._destination(41.5, -87.85, 90.0, 0.5 * i) for i in range(25)]  # ~12 km E
    mp = engine._corridor_multipolygon(pts, buffer_m=350.0, clearance_m=600.0)
    assert mp and mp["type"] == "MultiPolygon" and mp["coordinates"]
    start, end = pts[0], pts[-1]
    for poly in mp["coordinates"]:
        ring = poly[0]
        clat = sum(p[1] for p in ring[:-1]) / 4.0
        clng = sum(p[0] for p in ring[:-1]) / 4.0
        assert engine._haversine_km((clat, clng), start) >= 0.5    # clear of start
        assert engine._haversine_km((clat, clng), end) >= 0.5      # clear of turnaround


def test_corridor_none_when_too_short():
    assert engine._corridor_multipolygon([(41.5, -87.85), (41.5009, -87.85)]) is None


# --- the `wind` shape (ORS stubbed) ---------------------------------------- #
def _fake_ors(fail_on_avoid=False):
    calls = []

    def fake(api_key, profile, coordinates, timeout, round_trip=None, avoid_polygons=None):
        calls.append(avoid_polygons)
        if fail_on_avoid and avoid_polygons is not None:
            raise requests.HTTPError("avoided return unroutable")
        a = (coordinates[0][1], coordinates[0][0])      # (lat,lng)
        b = (coordinates[-1][1], coordinates[-1][0])
        n = 24
        pts = [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
               for i in range(n + 1)]
        dist = sum(engine._haversine_km(p, q) for p, q in zip(pts, pts[1:]))
        return pts, [200.0] * (n + 1), dist, 1.0, 0.0, 0.0, 0.0, 0.0

    return fake, calls


def test_wind_loop_avoids_outbound_on_return():
    fake, calls = _fake_ors()
    orig = engine._ors_directions
    engine._ors_directions = fake
    try:
        c = engine._make_wind_loop("k", "cycling-regular", 41.5, -87.85, 40.0, 247.5,
                                   40, seed=0)
        assert c.shape == "wind"
        assert len(calls) == 2
        assert calls[0] is None                          # outbound: no avoid
        assert calls[1] is not None                      # return avoids the corridor
        assert calls[1]["type"] == "MultiPolygon"
        assert c.distance_km > 0
    finally:
        engine._ors_directions = orig


def test_wind_loop_falls_back_when_return_blocked():
    fake, calls = _fake_ors(fail_on_avoid=True)
    orig = engine._ors_directions
    engine._ors_directions = fake
    try:
        c = engine._make_wind_loop("k", "cycling-regular", 41.5, -87.85, 40.0, 247.5,
                                   40, seed=0)
        assert c.shape == "wind"
        assert len(calls) == 3                            # out, avoided(fail), plain return
        assert calls[2] is None
    finally:
        engine._ors_directions = orig


def test_wind_is_optin_shape():
    assert "wind" in engine.SHAPES
    assert "wind" in engine.shapes_for("mountain", ["loop", "wind"])     # explicit survives
    assert "wind" not in engine.shapes_for(None, ["loop", "lollipop", "rectangle"])


# --- Valhalla seam (gated off by default) ---------------------------------- #
def test_valhalla_disabled_by_default():
    os.environ.pop(valhalla.URL_ENV, None)
    assert valhalla.enabled() is False


def test_valhalla_enabled_when_configured():
    os.environ[valhalla.URL_ENV] = "http://localhost:8002"
    try:
        assert valhalla.enabled() is True
    finally:
        os.environ.pop(valhalla.URL_ENV, None)


def test_valhalla_route_body():
    b = valhalla._route_body(41.5, -87.85, 41.6, -87.9)
    assert b["costing"] == "bicycle"
    assert b["locations"][0] == {"lat": 41.5, "lon": -87.85}
    assert b["locations"][1] == {"lat": 41.6, "lon": -87.9}


def test_valhalla_decode_polyline():
    # classic precision-5 test vector
    coords = valhalla._decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert len(coords) == 3
    assert abs(coords[0][0] - 38.5) < 1e-6 and abs(coords[0][1] + 120.2) < 1e-6
    assert abs(coords[2][0] - 43.252) < 1e-3


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
