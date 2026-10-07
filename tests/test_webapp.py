"""Offline smoke test for the web /plan render path.

Catches webapp <-> template drift (e.g. a card/ranked field the template references
but the view never sets) WITHOUT hitting the network: plan_routes and the
map/GPX writers are stubbed, so only the glue + Jinja templates run. This is the
class of bug that a 500-on-every-plan came from (a ranked-row key the template used
but the view omitted).

Run:  python tests/test_webapp.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webapp
from windroute import engine, planner, render


def _fake_result():
    wind = engine.Wind(direction_from_deg=180.0, speed_mph=10.0, gust_mph=15.0,
                       valid_time="2026-06-15T08:00")
    a = engine.Candidate(coords=[(41.50, -87.85), (41.52, -87.85), (41.52, -87.87)],
                         distance_km=40.0, ascent_m=60.0, paved_frac=1.0,
                         unpaved_frac=0.0, shape="loop")
    b = engine.Candidate(coords=[(41.50, -87.85), (41.50, -87.88), (41.52, -87.88)],
                         distance_km=42.0, ascent_m=80.0, paved_frac=0.7,
                         unpaved_frac=0.3, shape="lollipop", unrideable_frac=0.12)
    opts = [engine.RouteOption(a, "recommended", "Top pick", ["into the wind first"]),
            engine.RouteOption(b, "alternative", "Quieter roads", ["fewer arterials"])]
    return planner.PlanResult(
        location_label="Mokena, IL", when=dt.datetime(2026, 6, 15, 8),
        wind=wind, zone=None, ranked=[a, b], options=opts,
        notes=["region: grid-farmland (95%)"], surface_mode="ors",
        data_confidence="ok")


def test_plan_endpoint_renders_results():
    orig = (webapp.planner.plan_routes, render.render_map, render.write_gpx)
    webapp.planner.plan_routes = lambda **kw: _fake_result()
    render.render_map = lambda *a, **k: None
    render.write_gpx = lambda *a, **k: None
    try:
        client = webapp.app.test_client()
        r = client.post("/plan", data={"location": "Mokena, IL", "distance": "25",
                                       "unit": "mi", "ride_type": "road"})
        assert r.status_code == 200, f"status {r.status_code}"
        assert b"Mokena" in r.data
        assert b"Top pick" in r.data            # cards rendered
    finally:
        (webapp.planner.plan_routes, render.render_map, render.write_gpx) = orig


def test_results_show_every_candidate_and_round_trip_edit():
    """Every ranked route gets a card + map entry, and 'Edit plan' links back to a
    form pre-filled with the same inputs."""
    import json, re
    orig = (webapp.planner.plan_routes, render.write_gpx)
    res = _fake_result()
    extra = engine.Candidate(coords=[(41.50, -87.85), (41.49, -87.85), (41.49, -87.83)],
                             distance_km=38.0, ascent_m=40.0, paved_frac=1.0,
                             unpaved_frac=0.0, shape="rectangle")
    res.ranked.append(extra)
    webapp.planner.plan_routes = lambda **kw: res
    render.write_gpx = lambda *a, **k: None
    try:
        client = webapp.app.test_client()
        r = client.post("/plan", data={"location": "Mokena, IL", "distance": "42",
                                       "unit": "mi", "ride_type": "road", "speed": "18"})
        html = r.data.decode()
        assert r.status_code == 200
        assert html.count('class="rcard') == 3            # 2 options + 1 more candidate
        assert "More candidates" in html
        payload = json.loads(re.search(r'id="route-data">(.*?)</script>', html, re.S).group(1))
        assert payload["unit"] == "mi" and len(payload["routes"]) == 3
        assert [r["pick"] for r in payload["routes"]] == [True, True, False]
        edit = re.search(r'href="(/\?edit=1[^"]*)"', html).group(1).replace("&amp;", "&")
        form = client.get(edit).data.decode()
        assert 'value="42"' in form and 'value="18"' in form
    finally:
        (webapp.planner.plan_routes, render.write_gpx) = orig


def test_payload_carries_wind_field_and_share_fields():
    """The page payload has what the route wind arrows + share links need: the
    hourly field, the plan's start time and pace, and the card fields."""
    import json, re
    from windroute.models import WindField
    orig = (webapp.planner.plan_routes, render.write_gpx)
    res = _fake_result()
    res.wind.field = WindField(points=[(41.5, -87.85)], hours=[-1.0, 0.0, 1.0, 2.0],
                               u=[[1.0, 2.0, 3.0, 4.0]], v=[[5.0, 6.0, 7.0, 8.0]])
    for c in res.ranked:
        c.ride_hours = 1.5
    webapp.planner.plan_routes = lambda **kw: res
    render.write_gpx = lambda *a, **k: None
    try:
        r = webapp.app.test_client().post("/plan", data={"location": "Mokena, IL",
                                                          "distance": "25", "unit": "mi"})
        html = r.data.decode()
        payload = json.loads(re.search(r'id="route-data">(.*?)</script>', html, re.S).group(1))
        assert payload["field"]["hours"] == [-1.0, 0.0, 1.0, 2.0]
        assert payload["plan"]["start"] == "2026-06-15T08:00"
        assert [t["h"] for t in payload["plan"]["timeline"]] == [0.0, 1.0, 2.0]
        assert html.count('class="tl"') == 3
        assert payload["plan"]["pace_mph"] == 17.0                # default pace, in mph
        card = payload["routes"][0]["card"]
        assert card["headline"] == "Top pick" and card["ride_time"] == "1:30"
        assert set(webapp.SHARED_CARD_FIELDS) <= set(card)
        assert 'id="share-btn"' in html and "data-wind-toggle" in html
    finally:
        (webapp.planner.plan_routes, render.write_gpx) = orig


def test_share_page_and_tile_hosts_allowed():
    client = webapp.app.test_client()
    r = client.get("/share")
    assert r.status_code == 200 and b'id="share-root"' in r.data
    csp = r.headers["Content-Security-Policy"]
    for host in ("tile.openstreetmap.org", "tile-cyclosm.openstreetmap.fr",
                 "tile.opentopomap.org"):
        assert host in csp


def test_card_reasons_use_plan_unit():
    out = webapp._card_reasons(["a different option - a loop, 45.1 km",
                                "45.1 km, +108 m, ~1:40 at your pace",
                                "17% path (connectors)"], "mi")
    assert out == ["a different option - a loop, 28.0 mi", "17% path (connectors)"]


def test_index_and_about_render():
    client = webapp.app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/about").status_code == 200


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
