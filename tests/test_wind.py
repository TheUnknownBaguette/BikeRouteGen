"""Offline tests for wind fetching + how unknown wind is scored.

No network: the wind providers are stubbed. Covers the dual-source fallback /
graceful degradation (CODE_HEALTH Task B1) and the neutral handling of an unknown
wind in `evaluate`. Run:  python tests/test_wind.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from windroute import engine, wind   # patch wind providers on their home module


def test_get_wind_degrades_on_dual_failure():
    """Both sources failing returns a calm known=False Wind, never raises."""
    saved = (wind._wind_from_open_meteo, wind._wind_from_nws)

    def boom(*a, **k):
        raise requests.RequestException("down")
    wind._wind_from_open_meteo = boom
    wind._wind_from_nws = boom
    try:
        w = engine.get_wind(41.5, -87.85, dt.datetime(2026, 6, 21, 8))
    finally:
        wind._wind_from_open_meteo, wind._wind_from_nws = saved
    assert w.known is False
    assert w.speed_mph == 0.0


def test_get_wind_falls_back_to_nws_then_succeeds():
    """Open-Meteo failing but NWS working still returns a known wind."""
    saved = (wind._wind_from_open_meteo, wind._wind_from_nws)

    def boom(*a, **k):
        raise requests.RequestException("down")
    wind._wind_from_open_meteo = boom
    wind._wind_from_nws = lambda *a, **k: engine.Wind(180.0, 9.0, 14.0, "x")
    try:
        w = engine.get_wind(41.5, -87.85, dt.datetime(2026, 6, 21, 8))
    finally:
        wind._wind_from_open_meteo, wind._wind_from_nws = saved
    assert w.known is True and w.speed_mph == 9.0


def test_evaluate_neutralizes_unknown_wind():
    """An unknown wind makes the wind term a constant across candidates."""
    a = engine.Candidate(coords=[(41.50, -87.85), (41.55, -87.85), (41.55, -87.90)],
                         distance_km=40.0, ascent_m=0.0, paved_frac=1.0, unpaved_frac=0.0)
    b = engine.Candidate(coords=[(41.50, -87.85), (41.50, -87.80), (41.45, -87.80)],
                         distance_km=40.0, ascent_m=0.0, paved_frac=1.0, unpaved_frac=0.0)
    calm = engine.Wind(0.0, 0.0, 0.0, "", known=False)
    ranked = engine.evaluate([a, b], calm, "road", 40.0, tolerance_km=3.0)
    assert all(c.wind_score == 0.0 for c in ranked)          # no directional score
    # Identical except for wind direction → identical totals when wind is neutral.
    assert abs(ranked[0].total_score - ranked[1].total_score) < 1e-9


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
