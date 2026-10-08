"""Offline tests: OpenRouteService refusing the API key (quota used up / bad key).

No network: requests.post in routing is stubbed. A refused key must stop the plan
with a message that says so, instead of "No routes came back. Check ... the start
point is on a routable road", and must not keep firing calls that will all fail.
Run:  python tests/test_ors_access.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import routing


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


def _plan_with(resp, workers=1):
    calls = []
    saved = routing.requests.post

    def fake_post(*a, **k):
        calls.append(1)
        return resp
    routing.requests.post = fake_post
    try:
        routing.generate_candidates(41.52, -87.89, 48.0, "road", "KEY", n=12,
                                    shapes=("loop", "lollipop", "rectangle"),
                                    into_wind_bearing=270.0, workers=workers)
        return None, len(calls)
    except Exception as exc:                     # the error a front-end would show
        return exc, len(calls)
    finally:
        routing.requests.post = saved


def test_quota_exceeded_says_so_and_stops_early():
    exc, n = _plan_with(_Resp(403, {"error": "Quota exceeded"}))
    assert isinstance(exc, routing.OrsAccessError) and isinstance(exc, RuntimeError)
    assert "daily limit" in str(exc) and "routable road" not in str(exc)
    assert n <= 2                                # serial: stops after the first refusal


def test_bad_key_says_so():
    exc, _ = _plan_with(_Resp(403, {"error": "Access to this API has been disallowed"}))
    assert isinstance(exc, routing.OrsAccessError) and "API key" in str(exc)
    exc, _ = _plan_with(_Resp(401, {"error": "Authorization field missing"}))
    assert "API key" in str(exc)


def test_unroutable_points_still_give_the_old_message():
    exc, _ = _plan_with(_Resp(404, {"error": {"code": 2010, "message": "Could not find routable point"}}))
    assert isinstance(exc, RuntimeError) and not isinstance(exc, routing.OrsAccessError)
    assert "No routes came back" in str(exc)


def test_rate_limit_says_wait_a_minute():
    saved = routing.time.sleep
    routing.time.sleep = lambda s: None           # skip the 429 back-off pause
    try:
        exc, _ = _plan_with(_Resp(429, {"error": "Rate Limit Exceeded"}))
    finally:
        routing.time.sleep = saved
    assert isinstance(exc, RuntimeError) and "Wait a minute" in str(exc)


def test_dropped_connection_is_skipped_not_a_crash():
    import requests
    saved = routing.requests.post

    def boom(*a, **k):
        raise requests.ConnectionError("Connection aborted.")
    routing.requests.post = boom
    try:
        routing.generate_candidates(41.52, -87.89, 48.0, "road", "KEY", n=4,
                                    shapes=("loop",), into_wind_bearing=270.0, workers=2)
    except Exception as exc:
        assert type(exc) is RuntimeError and "No routes came back" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    finally:
        routing.requests.post = saved


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
