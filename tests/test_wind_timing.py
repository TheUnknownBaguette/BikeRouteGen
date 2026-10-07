"""Offline tests for time/space-varying wind scoring (the wind you'll MEET).

No network: wind fields are built by hand and the Open-Meteo call is stubbed.
Covers the core premise change: a steady wind still scores exactly like the
classic into-wind-first `wind_score`, but a wind that dies or shifts during the
ride flips the preference (e.g. tailwind out, calm home beats headwind out, calm
home). Run:  python tests/test_wind_timing.py
"""
import datetime as dt
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import engine, planner, wind
from windroute.geometry import _destination
from windroute.models import WindField

START = (41.50, -87.85)


def _field(series, points=(START,)):
    """WindField from [(hours, from_deg, mph), ...], same wind at every point."""
    hours = [h for h, _, _ in series]
    u = [[s * math.sin(math.radians(d)) for _, d, s in series] for _ in points]
    v = [[s * math.cos(math.radians(d)) for _, d, s in series] for _ in points]
    return WindField(points=list(points), hours=hours, u=u, v=v)


def _out_back(bearing, reach_km=20.0):
    out = [START] + [_destination(*START, bearing, reach_km * i / 20) for i in range(1, 21)]
    return out + out[-2::-1]


def _square_loop(first_bearing, side_km=8.0):
    pts, p = [START], START
    for k in range(4):
        for i in range(1, 9):
            pts.append(_destination(*p, (first_bearing + 90 * k) % 360, side_km * i / 8))
        p = pts[-1]
    return pts


STEADY_N = _field([(0, 0.0, 15.0), (6, 0.0, 15.0)])          # 15 mph from the north, all day
# A 40 km out-and-back at 16 mph turns around at ~0.6-1.0 h depending on the wind
# (faster with it behind you, slower into it): the wind dies in that window.
DYING_N = _field([(0, 0.0, 15.0), (0.62, 0.0, 15.0), (0.7, 0.0, 0.0), (6, 0.0, 0.0)])


def test_steady_field_matches_classic_score():
    """In a steady wind the timed score is the classic first-vs-second-half score."""
    for route in (_out_back(0.0), _out_back(200.0), _square_loop(30.0)):
        classic = engine.wind_score(route, 0.0)
        timed, _, _, _ = engine.timed_wind_score(route, STEADY_N, 16.0)
        assert abs(timed - classic) < 0.02, (timed, classic)


def test_steady_wind_keeps_into_wind_first():
    north_first, _, _, _ = engine.timed_wind_score(_out_back(0.0), STEADY_N, 16.0)
    south_first, _, _, _ = engine.timed_wind_score(_out_back(180.0), STEADY_N, 16.0)
    assert north_first > 1.5 and south_first < -1.5


def test_dying_wind_prefers_tailwind_out():
    """Wind dies ~halfway: headwind out + calm home loses to tailwind out + calm home."""
    north_first, out_n, back_n, _ = engine.timed_wind_score(_out_back(0.0), DYING_N, 16.0)
    south_first, out_s, back_s, _ = engine.timed_wind_score(_out_back(180.0), DYING_N, 16.0)
    assert south_first > north_first
    assert out_n > 7 and abs(back_n) < 3           # headwind out, ~calm home
    assert out_s < -7 and abs(back_s) < 3          # tailwind out, ~calm home


def test_speed_changes_what_wind_you_meet():
    """A slow rider is still out when the wind dies; a fast one is home before."""
    fast, _, _, _ = engine.timed_wind_score(_out_back(0.0, 10.0), DYING_N, 30.0)
    slow, _, _, _ = engine.timed_wind_score(_out_back(0.0, 10.0), DYING_N, 8.0)
    assert fast > slow


def test_shifting_wind_is_tracked():
    """Wind backs from N to S mid-ride: heading north first is now headwind BOTH ways."""
    shift = _field([(0, 0.0, 12.0), (0.62, 0.0, 12.0), (0.7, 180.0, 12.0), (6, 180.0, 12.0)])
    north_first, out_n, back_n, _ = engine.timed_wind_score(_out_back(0.0), shift, 16.0)
    south_first, out_s, back_s, _ = engine.timed_wind_score(_out_back(180.0), shift, 16.0)
    assert out_n > 5 and back_n > 5                # into it both legs
    assert out_s < -5 and back_s < -5              # tailwind both legs
    assert south_first > north_first


def test_ground_speed_follows_headwind():
    assert engine.ground_speed_mph(16.0, 0.0) == 16.0
    assert engine.ground_speed_mph(16.0, 12.0) == 13.0       # slower into it
    assert engine.ground_speed_mph(16.0, -12.0) == 19.0      # faster with it
    assert engine.ground_speed_mph(16.0, 60.0) == 8.0        # clamped
    # the effect grows with the wind
    assert (16.0 - engine.ground_speed_mph(16.0, 20.0)) > (16.0 - engine.ground_speed_mph(16.0, 8.0))


def test_headwind_leg_takes_longer_than_tailwind_leg():
    """Into a steady wind: the out leg is slower, so you turn around later than
    halfway through the ride and the whole ride takes longer than in still air."""
    route = _out_back(0.0)                                   # north, into a N wind
    segs, total = engine._ride_segments(route, STEADY_N, 16.0)
    half = len(segs) // 2
    _, out_t = engine._ride_segments(route[:half + 1], STEADY_N, 16.0)
    assert out_t > 0.55 * total                              # out leg is the slow one
    calm = _field([(0, 0.0, 0.0), (6, 0.0, 0.0)])
    _, calm_t = engine._ride_segments(route, calm, 16.0)
    assert total > calm_t
    *_, hours = engine.timed_wind_score(route, STEADY_N, 16.0)
    assert abs(hours - total) < 1e-9


def test_headwind_slowdown_changes_when_you_meet_the_wind():
    """Wind turns at 0.7 h. Into a strong headwind you're still on the way out
    when it turns; at a constant pace you'd already have been heading home."""
    turn = _field([(0, 0.0, 20.0), (0.65, 0.0, 20.0), (0.7, 180.0, 20.0), (6, 180.0, 20.0)])
    route = _out_back(0.0)
    _, out_mph, _, _ = engine.timed_wind_score(route, turn, 16.0)
    saved = engine.scoring.WIND_SPEED_EFFECT
    engine.scoring.WIND_SPEED_EFFECT = 0.0                   # constant pace
    try:
        _, out_const, _, _ = engine.timed_wind_score(route, turn, 16.0)
    finally:
        engine.scoring.WIND_SPEED_EFFECT = saved
    assert out_mph < out_const                               # met the turned wind sooner on the way out


def test_aim_bearing_steady_and_dying():
    steady = engine.best_aim_bearing(STEADY_N, *START, 40.0, 16.0, 0.0)
    assert steady == 0.0                           # steady: straight into the wind
    dying = engine.best_aim_bearing(DYING_N, *START, 40.0, 16.0, 0.0)
    assert abs((dying - 180 + 180) % 360 - 180) <= 60   # dying: aim downwind first


def test_field_interpolation_wraps_direction():
    """350° -> 10° interpolates through north, not through south."""
    f = _field([(0, 350.0, 10.0), (1, 10.0, 10.0)])
    d, s = f.at(*START, 0.5)
    assert min(d, 360 - d) < 1.0 and s > 9.0


def test_field_space_weights_nearest_point():
    far = _destination(*START, 0.0, 20.0)
    f = WindField(points=[START, far], hours=[0.0],
                  u=[[0.0], [0.0]], v=[[5.0], [20.0]])   # 5 mph at start, 20 up north
    _, s_start = f.at(*START, 0.0)
    _, s_far = f.at(*far, 0.0)
    _, s_mid = f.at(*_destination(*START, 0.0, 10.0), 0.0)
    assert s_start == 5.0 and s_far == 20.0 and 5.0 < s_mid < 20.0


def test_evaluate_uses_field_and_fills_head_mph():
    a = engine.Candidate(coords=_out_back(0.0), distance_km=40, ascent_m=0,
                         paved_frac=1.0, unpaved_frac=0.0, shape="out-and-back")
    b = engine.Candidate(coords=_out_back(180.0), distance_km=40, ascent_m=0,
                         paved_frac=1.0, unpaved_frac=0.0, shape="out-and-back")
    w = engine.Wind(0.0, 15.0, 20.0, "x", field=DYING_N)
    ranked = engine.evaluate([a, b], w, "road", 40.0, 3.0, speed_mph=16.0)
    assert ranked[0] is b                          # tailwind-out wins once wind dies
    assert engine.wind_verdict(b) == "tailwind first"
    assert "tailwind out" in engine.wind_summary(b)
    assert b.ride_hours > 0 and a.ride_hours > b.ride_hours   # into-it-first is slower
    # Same routes, no field (classic steady assumption): into-the-wind wins.
    w0 = engine.Wind(0.0, 15.0, 20.0, "x")
    assert engine.evaluate([a, b], w0, "road", 40.0, 3.0)[0] is a


def test_staging_loop_timed_from_arrival():
    """The scored loop of a staging route starts the clock after the transit leg."""
    stem = _out_back(90.0, 10.0)[:21]
    loop = _square_loop(0.0, 5.0)
    loop = [stem[-1]] + [(p[0] + stem[-1][0] - START[0], p[1] + stem[-1][1] - START[1])
                         for p in loop[1:]]
    assert abs(engine._route_offset_km(stem + loop[1:], loop) - 10.0) < 0.2
    assert engine._route_offset_km(loop, None) == 0.0


def test_open_meteo_multi_location_builds_field():
    """One request with several coordinates -> start Wind + a field over all points."""
    times = [f"2026-06-21T{h:02d}:00" for h in range(24)]
    one = {"hourly": {"time": times, "wind_speed_10m": [10.0] * 24,
                      "wind_direction_10m": [270.0] * 24, "wind_gusts_10m": [15.0] * 24}}

    class Resp:
        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return [one] * self.n if self.n > 1 else one

    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["lat"] = params["latitude"]
        return Resp(len(params["latitude"].split(",")))

    saved = wind.requests.get
    wind.requests.get = fake_get
    try:
        w = engine.get_wind(*START, dt.datetime(2026, 6, 21, 8), radius_km=48.0)
        single = engine.get_wind(*START, dt.datetime(2026, 6, 21, 8))
    finally:
        wind.requests.get = saved
    assert len(seen["lat"].split(",")) == 1        # last call was the single-point one
    assert w.speed_mph == 10.0 and w.direction_from_deg == 270.0
    assert len(w.field.points) == 1 + wind.FIELD_RING_POINTS
    assert w.field.hours[0] == -1.0 and w.field.hours[-1] == 12.0
    d, s = w.field.at(*START, 2.0)
    assert abs(d - 270.0) < 1e-6 and abs(s - 10.0) < 1e-6
    assert len(single.field.points) == 1


def test_wind_change_note():
    w = engine.Wind(0.0, 15.0, 20.0, "x", field=DYING_N)
    note = planner._wind_change_note(w, *START, dt.datetime(2026, 6, 21, 8), 2.0)
    assert "15 mph N" in note and "0 mph" in note
    w = engine.Wind(0.0, 15.0, 20.0, "x", field=STEADY_N)
    assert planner._wind_change_note(w, *START, dt.datetime(2026, 6, 21, 8), 2.0) == ""


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
