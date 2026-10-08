"""Offline tests: the Open-Meteo forecast fetch retries a slow moment and is cached.

No network: requests.get in windroute.wind is stubbed.
Run:  python tests/test_wind_fetch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from windroute import wind


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_retries_once_then_caches():
    calls = []
    saved = wind.requests.get

    def flaky(*a, **k):
        calls.append(k.get("timeout"))
        if len(calls) == 1:
            raise requests.Timeout("slow moment")
        return _Resp([{"hourly": {"time": ["2026-10-08T08:00"]}}] * 2)
    wind.requests.get = flaky
    wind._forecast_cache.clear()
    pts = [(41.5, -87.9), (41.6, -87.9)]
    try:
        first = wind._open_meteo_hourlies(pts)
        again = wind._open_meteo_hourlies([(41.50002, -87.90001), (41.6, -87.9)])  # same spot
    finally:
        wind.requests.get = saved
        wind._forecast_cache.clear()
    assert len(calls) == 2 and calls[0] == wind.OPEN_METEO_TIMEOUT_S
    assert first is again and len(first) == 2


def test_gives_up_after_the_retries():
    saved = wind.requests.get

    def down(*a, **k):
        raise requests.ConnectionError("503")
    wind.requests.get = down
    wind._forecast_cache.clear()
    try:
        wind._open_meteo_hourlies([(41.5, -87.9)])
    except requests.RequestException:
        pass
    else:
        raise AssertionError("expected the error so get_wind falls back to NWS")
    finally:
        wind.requests.get = saved


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
