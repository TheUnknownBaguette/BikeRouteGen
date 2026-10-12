"""Ride-length cap, the site-wide routing budget, and climb below sea level (offline)."""
from collections import deque

import pytest

import webapp
from windroute import engine, planner
from windroute.routing import _smoothed_ascent


# -- climb ------------------------------------------------------------------- #
def _ramp(a, b, n=200):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def test_climb_counts_below_sea_level():
    """Death Valley: -80 m up to -20 m is 60 m of climbing, not zero."""
    up = _ramp(-80.0, -20.0)
    assert _smoothed_ascent(up) == pytest.approx(60.0, abs=3.0)
    assert _smoothed_ascent(up[::-1]) == pytest.approx(0.0, abs=0.5)


def test_climb_crossing_sea_level():
    assert _smoothed_ascent(_ramp(-10.0, 40.0)) == pytest.approx(50.0, abs=3.0)


def test_climb_above_sea_level_unchanged():
    assert _smoothed_ascent(_ramp(200.0, 260.0)) == pytest.approx(60.0, abs=3.0)


@pytest.mark.parametrize("nodata", [0.0, -32768.0])
def test_dropouts_still_ignored(nodata):
    """A dropout in the middle of a flat 230 m plateau isn't a 230 m climb."""
    eles = [230.0] * 100
    eles[40:43] = [nodata] * 3
    assert _smoothed_ascent(eles) == pytest.approx(0.0, abs=0.5)


# -- ride cap ---------------------------------------------------------------- #
def test_ride_cap_is_250_km():
    assert engine.MAX_RIDE_KM == 250.0
    with pytest.raises(ValueError, match="250 km"):
        engine.generate_candidates(41.5, -87.8, 251.0, "road", "key")
    # under the cap the next check (the API key) is what complains
    with pytest.raises(ValueError, match="API key"):
        engine.generate_candidates(41.5, -87.8, 240.0, "road", None)


def test_planner_rejects_long_ride_before_any_lookup(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("geocoded a ride that's over the cap")
    monkeypatch.setattr(planner.engine, "geocode", boom)
    with pytest.raises(ValueError, match="155 mi"):
        planner.plan_routes("Chicago, IL", 160, unit="mi", api_key="k")


# -- site-wide routing budget --------------------------------------------------- #
def test_site_budget_blocks_plans_once_spent(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(webapp.engine, "ors_call_total", lambda: calls["n"])
    monkeypatch.setattr(webapp, "SITE_ORS_PER_DAY", 30)
    monkeypatch.setattr(webapp, "_ors_marks", deque([(0.0, 0)]))
    monkeypatch.setattr(webapp.time, "time", lambda: 1000.0)
    assert not webapp._site_quota_spent()
    calls["n"] = 29
    assert not webapp._site_quota_spent()
    calls["n"] = 30
    assert webapp._site_quota_spent()


def test_site_budget_frees_up_after_a_day(monkeypatch):
    clock = {"t": 1000.0}
    calls = {"n": 0}
    monkeypatch.setattr(webapp.engine, "ors_call_total", lambda: calls["n"])
    monkeypatch.setattr(webapp, "SITE_ORS_PER_DAY", 30)
    monkeypatch.setattr(webapp, "_ors_marks", deque([(1000.0, 0)]))
    monkeypatch.setattr(webapp.time, "time", lambda: clock["t"])
    calls["n"] = 40
    clock["t"] = 2000.0
    assert webapp._site_quota_spent()                # 40 calls today
    clock["t"] = 2000.0 + webapp.DAY_S + 1
    assert not webapp._site_quota_spent()            # those have aged out


def test_plan_endpoint_says_when_budget_is_spent(monkeypatch):
    monkeypatch.setattr(webapp, "_rate_limited", lambda ip: False)
    monkeypatch.setattr(webapp, "_site_quota_spent", lambda: True)
    monkeypatch.setattr(webapp.planner, "plan_routes",
                        lambda **kw: pytest.fail("planned past the budget"))
    r = webapp.app.test_client().post("/plan", data={"location": "Chicago, IL",
                                                     "distance": "20"})
    assert r.status_code == 429
    assert b"routing allowance" in r.data


def test_per_ip_limit_is_generous():
    assert webapp.RL_MAX >= 30
