"""Club mode (windroute/club.py): club roads, exits, the club-loop shape, the planner
and web wiring. Offline: rides are synthetic, the routing service is stubbed."""
import json

import pytest

import webapp
from windroute import club, engine, planner, routing
from windroute.geometry import _destination
from windroute.models import Candidate, Wind

START = (40.1107, -88.2320)


def _line(p, bearing, km, step_km=0.02):
    n = int(km / step_km)
    return [_destination(p[0], p[1], bearing, step_km * i) for i in range(n + 1)]


def _ride(name, out_bearing, split_km, branch_bearing):
    """Out of town due `out_bearing` for split_km, then off on `branch_bearing`, and
    straight back (enough of a ride for the exit finder)."""
    a = _line(START, out_bearing, split_km)
    b = _line(a[-1], branch_bearing, 6.0)[1:]
    there = a + b
    return {"file": name, "coords": there + there[-2::-1]}


STARTS = [{"id": "a", "name": "Shop", "lat": START[0], "lng": START[1]}]


# -- reading ride files ------------------------------------------------------- #
def test_reads_tcx_and_gpx(tmp_path):
    (tmp_path / "r.tcx").write_text(
        '<?xml version="1.0"?><TrainingCenterDatabase xmlns="http://www.garmin.com/'
        'xmlschemas/TrainingCenterDatabase/v2"><Courses><Course><Track>'
        '<Trackpoint><Position><LatitudeDegrees>40.1</LatitudeDegrees>'
        '<LongitudeDegrees>-88.2</LongitudeDegrees></Position></Trackpoint>'
        '<Trackpoint><Position><LatitudeDegrees>40.2</LatitudeDegrees>'
        '<LongitudeDegrees>-88.3</LongitudeDegrees></Position></Trackpoint>'
        '</Track></Course></Courses></TrainingCenterDatabase>')
    (tmp_path / "r.gpx").write_text(
        '<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
        '<trkpt lat="40.1" lon="-88.2"/><trkpt lat="40.2" lon="-88.3"/></trkseg></trk></gpx>')
    assert club.read_track(tmp_path / "r.tcx") == [(40.1, -88.2), (40.2, -88.3)]
    assert club.read_track(tmp_path / "r.gpx") == [(40.1, -88.2), (40.2, -88.3)]
    assert len(club.read_rides(tmp_path)) == 1          # same ride twice: kept once


# -- club roads ---------------------------------------------------------------- #
def test_town_fraction_counts_only_in_town_riding():
    road = _line(START, 90, 10)
    roads = club.ClubRoads(club.road_cells([{"coords": road}]))
    assert roads.town_fraction(road, START, 6.5) == pytest.approx(1.0)
    parallel = _line(_destination(*START, 0, 0.3), 90, 5)    # a street 300 m north
    assert roads.town_fraction(parallel, START, 6.5) == pytest.approx(0.0)
    # half on the club road, half on the parallel street
    mixed = _line(START, 90, 2) + _line(_destination(*START, 0, 0.3), 90, 2)
    assert 0.3 < roads.town_fraction(mixed, START, 6.5) < 0.7


# -- exits ----------------------------------------------------------------------- #
def test_exit_ends_where_the_rides_spread_out():
    rides = [_ride(f"e{k}", 90, 7.0, b) for k, b in enumerate((30, 60, 120, 150))]
    exits = club.find_exits(rides, STARTS, START)
    assert len(exits) == 1
    ex = exits[0]
    assert 80 <= ex["bearing"] <= 100
    assert 6.0 <= ex["end_km"] <= 7.6                   # the split point, ~7 km out
    assert ex["rides"] == 8                             # 4 rides, out + home


def test_exit_waypoints_spread_along_the_exit():
    path = [list(p) for p in _line(START, 90, 6)]
    wps = club.exit_waypoints(path, n=4)
    assert len(wps) == 4 and wps[-1] == tuple(path[-1])


def test_load_and_exits_for(tmp_path):
    rides = [_ride(f"e{k}", 90, 7.0, b) for k, b in enumerate((30, 60, 120, 150))]
    exits = club.find_exits(rides, STARTS, START)
    exits[0]["enabled"] = True
    (tmp_path / "club.json").write_text(json.dumps(
        {"name": "Test club", "starts": STARTS, "club_road_weight": 0.4}))
    (tmp_path / "club_data.json").write_text(json.dumps(
        {"exits": exits, "cells": [list(c) for c in club.road_cells(rides)], "town_km": 6.5}))
    c = club.load(tmp_path / "club.json")
    assert c.name == "Test club" and c.weight == 0.4
    got = c.exits_for("a")
    assert len(got) == 1 and got[0]["km"] > 6
    exits[0]["enabled"] = False
    (tmp_path / "club_data.json").write_text(json.dumps({"exits": exits, "cells": []}))
    assert club.load(tmp_path / "club.json").exits_for("a") == []


# -- the club-loop shape ------------------------------------------------------------ #
def _ex(name, bearing, km=5.0, home_only=False):
    return {"id": name, "name": name, "bearing": bearing, "km": km,
            "home_only": home_only, "waypoints": [_destination(*START, bearing, km)]}


def test_route_plan_follows_the_wind_and_varies():
    exits = [_ex("N", 0), _ex("E", 90), _ex("S", 180), _ex("W", 270),
             _ex("E2", 80, home_only=True)]
    out, home = routing._club_route_plan(exits, 175, 0)
    assert out["id"] == "S" and home is out                 # into the wind, same way back
    outs = {routing._club_route_plan(exits, 175, i)[0]["id"] for i in range(3)}
    assert "E2" not in outs                                  # home-only never leads out
    out, home = routing._club_route_plan(exits, 85, 3)       # second round: next exit home
    assert out["id"] == "E" and home["id"] == "E2"


def test_short_rides_drop_exits_that_leave_no_loop():
    exits = [_ex("near", 0, km=3), _ex("far", 90, km=12)]
    assert [e["id"] for e in routing.club_exits_for_ride(exits, 30)] == ["near"]
    assert routing.club_exits_for_ride(exits, 5) == []


def test_club_loop_routes_through_the_exit(monkeypatch):
    seen = {}

    def fake_ors(api_key, profile, coordinates, timeout):
        seen["pts"] = coordinates
        co = [(p[1], p[0]) for p in coordinates]
        return co, None, 40.0, 1.0, 0.0, 0.0, 0.0, 0.0, [""] * len(co), 0.0

    monkeypatch.setattr(routing, "_ors_directions", fake_ors)
    ex = _ex("S", 180, km=5)
    ex["waypoints"] = [_destination(*START, 180, k) for k in (1.25, 2.5, 3.75, 5)]
    c = routing._make_club_loop("k", "cycling-regular", *START, 48, 180, 0, [ex], 40)
    pts = [(p[1], p[0]) for p in seen["pts"]]
    assert c.shape == "club loop"
    assert pts[0] == START and pts[-1] == START
    assert pts[1:5] == [tuple(w) for w in ex["waypoints"]]           # out by the exit
    assert pts[-5:-1] == [tuple(w) for w in ex["waypoints"][::-1]]   # and home by it


# -- scoring -------------------------------------------------------------------------- #
def _cand(frac):
    co = _line(START, 90, 5) + _line(START, 90, 5)[-2::-1]
    return Candidate(coords=co, distance_km=10.0, ascent_m=0.0, paved_frac=1.0,
                     unpaved_frac=0.0, shape="loop", club_frac=frac)


def test_club_bonus_only_in_club_mode():
    wind = Wind(270, 10, 15, "2026-10-19T08:00")
    a, b = _cand(0.9), _cand(0.1)
    engine.evaluate([a, b], wind, "road", 10.0)
    assert a.total_score == b.total_score                  # main site: no effect
    from dataclasses import replace
    w = replace(engine.weights_for(None), w_club=0.5)
    engine.evaluate([a, b], wind, "road", 10.0, weights=w)
    assert a.total_score - b.total_score == pytest.approx(0.4)


# -- planner + web ----------------------------------------------------------------------- #
def _test_club():
    exits = [{"id": "S", "name": "S via First St", "bearing": 180.0, "enabled": True,
              "home_only": False, "paths": {"a": [list(p) for p in _line(START, 180, 5)]}}]
    return club.Club(name="Test club", starts=STARTS + [
        {"id": "b", "name": "Other", "lat": 40.104, "lng": -88.2402}],
        exits=exits, roads=club.ClubRoads(club.road_cells([{"coords": _line(START, 180, 8)}])),
        weight=0.5, center=START)


def test_planner_club_mode(monkeypatch):
    seen = {}

    def fake_generate(lat, lng, target_km, ride_type, api_key, **kw):
        seen.update(kw, lat=lat, lng=lng)
        co = _line((lat, lng), 180, 8) + _line((lat, lng), 180, 8)[-2::-1]
        return [Candidate(coords=co, distance_km=16, ascent_m=0, paved_frac=1,
                          unpaved_frac=0, shape="club loop")]

    monkeypatch.setattr(planner.engine, "generate_candidates", fake_generate)
    monkeypatch.setattr(planner.engine, "get_wind",
                        lambda *a, **k: Wind(180, 8, 10, "2026-10-19T08:00"))
    r = planner.plan_routes("ignored", 10, club=_test_club(), club_start="a",
                            api_key="k", fast_roads=False, corrections=False)
    assert (seen["lat"], seen["lng"]) == START
    assert r.location_label == "Shop"
    assert seen["shapes"].count("club loop") == 3          # alternates with the rest
    assert seen["club_exits"] and seen["club_exits"][0]["id"] == "S"
    assert r.ranked[0].club_frac > 0.9


def test_web_club_mode(monkeypatch):
    c = _test_club()
    monkeypatch.setattr(webapp, "CLUB", c)
    monkeypatch.setitem(webapp.app.jinja_env.globals, "club", c)
    monkeypatch.setattr(webapp, "_rate_limited", lambda ip: False)
    page = webapp.app.test_client().get("/").data.decode()
    assert 'name="club_start"' in page and "Plan a club ride" in page
    assert 'id="location"' not in page and 'name="ride_area"' not in page
    seen = {}

    def fake_plan(**kw):
        seen.update(kw)
        raise ValueError("stop here")

    monkeypatch.setattr(webapp.planner, "plan_routes", fake_plan)
    webapp.app.test_client().post("/plan", data={"club_start": "b", "distance": "20"})
    assert seen["club"] is c and seen["club_start"] == "b" and seen["ride_area"] is None
    webapp.app.test_client().post("/plan", data={"club_start": "nope", "distance": "20"})
    assert seen["club_start"] == "a"                       # unknown start -> the first


def test_main_site_has_no_club():
    assert webapp.CLUB is None or webapp.os.environ.get("CLUB_CONFIG")
    page = webapp.app.test_client().get("/").data.decode()
    if webapp.CLUB is None:
        assert 'id="location"' in page and "club_start" not in page
