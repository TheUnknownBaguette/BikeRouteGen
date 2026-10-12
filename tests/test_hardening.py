"""Input hardening from the Oct 2026 code-health pass (offline).

- The web form's choice fields are whitelisted before they reach the planner (they
  end up in file names and GPX/TCX text).
- GPX track names are XML-escaped.
- A failed region lookup isn't cached, so the next plan retries it.
"""
import datetime as dt
import xml.etree.ElementTree as ET

import webapp
from windroute import engine, planner, regions, render


def _result():
    wind = engine.Wind(direction_from_deg=180.0, speed_mph=10.0, gust_mph=15.0,
                       valid_time="2026-06-15T08:00")
    a = engine.Candidate(coords=[(41.50, -87.85), (41.52, -87.85), (41.52, -87.87)],
                         distance_km=40.0, ascent_m=60.0, paved_frac=1.0,
                         unpaved_frac=0.0, shape="loop")
    return planner.PlanResult(
        location_label="Chicago, IL", when=dt.datetime(2026, 6, 15, 8), wind=wind,
        zone=None, ranked=[a], options=[engine.RouteOption(a, "recommended", "Top pick")])


def test_plan_whitelists_choice_fields(monkeypatch):
    seen = {}

    def fake_plan(**kw):
        seen.update(kw)
        return _result()

    monkeypatch.setattr(webapp.planner, "plan_routes", fake_plan)
    monkeypatch.setattr(webapp, "_rate_limited", lambda ip: False)
    r = webapp.app.test_client().post("/plan", data={
        "location": "Chicago, IL", "distance": "25", "unit": "furlongs",
        "ride_type": "<b>road</b>", "surface_source": "evil",
        "shapes": ["loop", "spiral", "../x"]})
    assert r.status_code == 200
    assert seen["ride_type"] == "road"
    assert seen["unit"] == "mi"
    assert seen["surface_source"] == "ors"
    assert seen["shapes"] == ["loop"]
    assert b"<b>road</b>" not in r.data


def test_plan_keeps_valid_choices(monkeypatch):
    seen = {}
    monkeypatch.setattr(webapp.planner, "plan_routes",
                        lambda **kw: (seen.update(kw), _result())[1])
    monkeypatch.setattr(webapp, "_rate_limited", lambda ip: False)
    webapp.app.test_client().post("/plan", data={
        "location": "Chicago, IL", "distance": "20", "unit": "km", "ride_type": "gravel",
        "surface_source": "both", "shapes": ["rectangle", "out-and-back"]})
    assert (seen["unit"], seen["ride_type"], seen["surface_source"]) == ("km", "gravel", "both")
    assert seen["shapes"] == ["rectangle", "out-and-back"]


def test_gpx_name_is_escaped(tmp_path):
    path = tmp_path / "r.gpx"
    render.write_gpx([(41.5, -87.8), (41.6, -87.9)], str(path), name='A & B <x> "q"')
    root = ET.parse(path).getroot()                  # well-formed XML
    name = root.find(".//{*}name")
    assert name.text == 'A & B <x> "q"'


def test_failed_region_lookup_is_not_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(regions, "_query_overpass",
                        lambda *a: calls.append(1) or None)
    regions._CACHE.clear()
    for _ in range(2):
        prof = regions.classify_region((10.123, 20.456))
        assert prof.archetype == "unknown"
    assert len(calls) == 2                           # retried, not served from cache
    assert not regions._CACHE
