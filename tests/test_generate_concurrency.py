"""Offline tests for concurrent candidate generation (CODE_HEALTH Task A2).

No network: the per-shape `_make_*` builders are stubbed to return canned
candidates (with a small artificial delay) so we can assert that parallelizing
`generate_candidates` keeps the SAME result set + ordering as the serial path,
skips a failing seed, and is meaningfully faster than serial.

Run:  python tests/test_generate_concurrency.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from windroute import engine, routing   # generate_candidates lives in routing now;
                                         # monkeypatches must target the home module

BUILD_DELAY = 0.05   # simulated per-ORS-call latency


def _stub_builders(monkeypatched, fail_loop_idx=None):
    """Replace every _make_* with a fast stub that records its (shape, seed).

    Each stub sleeps BUILD_DELAY (to model a network round-trip) and tags the
    returned Candidate's shape so we can read back the exact build order. Returns
    a restore() callable. If `fail_loop_idx` is set, the loop builder raises
    requests.HTTPError on that seed index (to test seed-skip).
    """
    saved = {}

    def make(shape):
        def _f(*a, **k):
            time.sleep(BUILD_DELAY)
            seed = k.get("seed", k.get("n_sides"))
            return engine.Candidate(coords=[(41.5, -87.8), (41.6, -87.8)],
                                    distance_km=40.0, ascent_m=0.0,
                                    paved_frac=1.0, unpaved_frac=0.0, shape=shape)
        return _f

    names = {"loop": "_make_polygon_loop", "rectangle": "_make_rectangle",
             "lollipop": "_make_lollipop", "out-and-back": "_make_out_back",
             "roundtrip": "_make_roundtrip", "wind": "_make_wind_loop",
             "staging": "_make_staging"}
    for shape, fn in names.items():
        saved[fn] = getattr(routing, fn)

    seen = {"loop_calls": 0}

    def loop_stub(*a, **k):
        i = seen["loop_calls"]
        seen["loop_calls"] += 1
        if fail_loop_idx is not None and i == fail_loop_idx:
            raise requests.HTTPError("boom")
        time.sleep(BUILD_DELAY)
        return engine.Candidate(coords=[(41.5, -87.8), (41.6, -87.8)],
                                distance_km=40.0, ascent_m=0.0, paved_frac=1.0,
                                unpaved_frac=0.0, shape="loop")

    routing._make_polygon_loop = loop_stub
    for shape, fn in names.items():
        if fn != "_make_polygon_loop":
            setattr(routing, fn, make(shape))

    def restore():
        for fn, orig in saved.items():
            setattr(routing, fn, orig)
    return restore


def test_concurrent_matches_serial_set_and_order():
    restore = _stub_builders(None)
    try:
        serial = engine.generate_candidates(
            41.5, -87.8, 40.0, "road", "k", n=9,
            shapes=("loop", "lollipop", "rectangle"), workers=1)
        # reset loop counter implicitly via a fresh stub
    finally:
        restore()
    restore = _stub_builders(None)
    try:
        concurrent_ = engine.generate_candidates(
            41.5, -87.8, 40.0, "road", "k", n=9,
            shapes=("loop", "lollipop", "rectangle"), workers=6)
    finally:
        restore()
    assert [c.shape for c in serial] == [c.shape for c in concurrent_], \
        "concurrent result order must match serial (plan order)"
    assert len(concurrent_) == 9


def test_concurrent_is_faster_than_serial():
    restore = _stub_builders(None)
    try:
        t0 = time.perf_counter()
        engine.generate_candidates(41.5, -87.8, 40.0, "road", "k", n=12,
                                   shapes=("loop",), workers=1)
        serial_s = time.perf_counter() - t0
    finally:
        restore()
    restore = _stub_builders(None)
    try:
        t0 = time.perf_counter()
        engine.generate_candidates(41.5, -87.8, 40.0, "road", "k", n=12,
                                   shapes=("loop",), workers=6)
        conc_s = time.perf_counter() - t0
    finally:
        restore()
    # 12 calls * 0.05s = 0.6s serial; with 6 workers ~0.1s. Allow generous slack.
    assert conc_s < serial_s / 2, f"expected speedup; serial={serial_s:.3f} conc={conc_s:.3f}"


def test_failing_seed_is_skipped_not_fatal():
    restore = _stub_builders(None, fail_loop_idx=2)
    try:
        out = engine.generate_candidates(41.5, -87.8, 40.0, "road", "k", n=6,
                                         shapes=("loop",), workers=4)
    finally:
        restore()
    assert len(out) == 5, f"one seed should be skipped, got {len(out)}"
    assert all(c.shape == "loop" for c in out)


def test_all_failing_raises_runtimeerror():
    restore = _stub_builders(None, fail_loop_idx=None)
    # make every loop call fail
    try:
        def boom(*a, **k):
            raise requests.HTTPError("down")
        routing._make_polygon_loop = boom
        raised = False
        try:
            engine.generate_candidates(41.5, -87.8, 40.0, "road", "k", n=4,
                                       shapes=("loop",), workers=4)
        except RuntimeError:
            raised = True
        assert raised, "all-fail should raise RuntimeError"
    finally:
        restore()


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
