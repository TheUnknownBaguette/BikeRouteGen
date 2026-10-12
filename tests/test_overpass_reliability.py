"""Offline tests: Overpass mirror health, hedging, partial answers, in-flight joining,
and the plan form's /prefetch.

No network: surface._post_overpass (one mirror request) is stubbed.
Run:  python tests/test_overpass_reliability.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from windroute import planner, surface

A, B, C = surface.OVERPASS_MIRRORS


def _stub(behaviour):
    """behaviour: url -> callable(query, wait) returning elements or raising."""
    saved = surface._post_overpass
    surface._post_overpass = lambda url, query, wait: behaviour[url](query, wait)
    surface._mirror_failed.clear()
    return lambda: (setattr(surface, "_post_overpass", saved), surface._mirror_failed.clear())


def _fail(*a):
    raise requests.Timeout("slow")


def _ok(tag):
    return lambda *a: [{"id": tag}]


def _slow(tag, s):
    def f(*a):
        time.sleep(s)
        return [{"id": tag}]
    return f


def test_partial_answer_is_a_failure_not_an_empty_result():
    saved = surface.requests.post

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"elements": [], "remark": "runtime error: Query timed out in \"query\""}
    surface.requests.post = lambda *a, **k: Resp()
    try:
        surface._post_overpass(A, "q", 5)
        raise AssertionError("a timed-out query must not count as an empty answer")
    except surface.OverpassIncomplete:
        pass
    finally:
        surface.requests.post = saved


def test_failed_mirror_goes_to_the_back_of_the_line():
    restore = _stub({A: _fail, B: _ok("b"), C: _ok("c")})
    try:
        assert surface.overpass_json("q", 5) == [{"id": "b"}]
        assert surface._mirror_order() == [B, C, A]       # A just failed: tried last
    finally:
        restore()


def test_hedge_takes_the_first_mirror_to_answer():
    restore = _stub({A: _slow("a", 2.0), B: _ok("b"), C: _ok("c")})
    t = time.monotonic()
    try:
        got = surface.overpass_json("q", 5, hedge_s=0.2)
    finally:
        restore()
    assert got == [{"id": "b"}] and time.monotonic() - t < 1.5


def test_without_hedge_mirrors_are_asked_one_at_a_time():
    asked = []

    def rec(tag):
        def f(*a):
            asked.append(tag)
            time.sleep(0.3)
            return [{"id": tag}]
        return f
    restore = _stub({A: rec("a"), B: rec("b"), C: rec("c")})
    try:
        assert surface.overpass_json("q", 5) == [{"id": "a"}]
    finally:
        restore()
    assert asked == ["a"]


def test_every_mirror_failing_raises():
    restore = _stub({A: _fail, B: _fail, C: _fail})
    try:
        surface.overpass_json("q", 5, hedge_s=0.1)
        raise AssertionError("should have raised")
    except requests.Timeout:
        pass
    finally:
        restore()


def test_a_second_lookup_joins_the_one_already_running():
    calls = []
    gate = threading.Event()

    def slow_build(self, s, w, n, e, timeout=0, deadline_s=30):
        calls.append(1)
        gate.wait(5)
        self.bbox = (s, w, n, e)
        return self
    saved = surface.FastRoads.build_bbox
    surface.FastRoads.build_bbox = slow_build
    surface.clear_fast_cache()
    out = []
    try:
        first = threading.Thread(target=lambda: out.append(
            surface.fast_roads_near(41.5, -87.9, 10)))
        first.start()
        time.sleep(0.2)
        second = threading.Thread(target=lambda: out.append(
            surface.fast_roads_near(41.5, -87.9, 8)))         # inside the same area
        second.start()
        time.sleep(0.2)
        gate.set()
        first.join(5)
        second.join(5)
    finally:
        surface.FastRoads.build_bbox = saved
        surface.clear_fast_cache()
    assert len(calls) == 1 and len(out) == 2 and out[0] is out[1]


def test_prefetch_endpoint_starts_the_lookup():
    import webapp
    started = []
    saved = planner.prefetch_area
    planner.prefetch_area = lambda lat, lng, km: started.append((lat, lng, km))
    try:
        client = webapp.app.test_client()
        ok = client.post("/prefetch", json={"lat": 40.11, "lng": -88.24, "km": 56})
        bad = client.post("/prefetch", json={"lat": "x"})
    finally:
        planner.prefetch_area = saved
    assert ok.status_code == 202 and started == [(40.11, -88.24, 56.0)]
    assert bad.status_code == 400


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
