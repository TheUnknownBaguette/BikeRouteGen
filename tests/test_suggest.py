"""Offline tests for the location type-ahead (/suggest): location bias + cache.

No network: Photon is stubbed. Run:  python tests/test_suggest.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webapp
import windroute.geocode  # noqa: F401  (the package re-exports a geocode() function)

geocode = sys.modules["windroute.geocode"]


def _stub_photon(calls):
    def fake(query, count, bias=None):
        calls.append((query, bias))
        return [{"label": f"{query} place", "lat": 41.5, "lng": -87.8}]
    return fake


def test_bias_is_passed_rounded_and_results_cached():
    calls, saved = [], geocode._suggest_photon
    geocode._suggest_photon = _stub_photon(calls)
    geocode._SUGGEST_CACHE.clear()
    try:
        a = geocode.suggest_places("Mok", near=(41.5261, -87.8892))
        b = geocode.suggest_places("mok", near=(41.5499, -87.8601))   # same ~10 km cell
        geocode.suggest_places("Mok", near=(34.79, 126.38))            # elsewhere: new call
        geocode.suggest_places("Mok")                                   # no bias: new call
    finally:
        geocode._suggest_photon = saved
        geocode._SUGGEST_CACHE.clear()
    assert a == b
    assert calls == [("Mok", (41.5, -87.9)), ("Mok", (34.8, 126.4)), ("Mok", None)]


def test_outage_is_not_cached():
    calls, saved = [], (geocode._suggest_photon, geocode._suggest_openmeteo)
    geocode._suggest_photon = lambda q, c, bias=None: calls.append(q) or []
    geocode._suggest_openmeteo = lambda q, c: []
    geocode._SUGGEST_CACHE.clear()
    try:
        geocode.suggest_places("Mokena")
        geocode.suggest_places("Mokena")
    finally:
        geocode._suggest_photon, geocode._suggest_openmeteo = saved
    assert calls == ["Mokena", "Mokena"]                 # retried, not cached empty


def test_suggest_endpoint_forwards_map_area():
    seen, saved = {}, webapp.engine.suggest_places

    def fake(q, count=6, near=None):
        seen.update(q=q, near=near)
        return [{"label": "Mokena, Illinois, US", "lat": 41.5, "lng": -87.9}]
    webapp.engine.suggest_places = fake
    try:
        client = webapp.app.test_client()
        r = client.get("/suggest?q=Mok&lat=41.5&lng=-87.9")
        assert r.status_code == 200 and r.get_json()[0]["label"].startswith("Mokena")
        assert seen == {"q": "Mok", "near": (41.5, -87.9)}
        assert "max-age" in r.headers["Cache-Control"]
        client.get("/suggest?q=Mok&lat=999&lng=x")      # junk bias is ignored
        assert seen["near"] is None
    finally:
        webapp.engine.suggest_places = saved


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)


# --------------------------------------------------------------- typed addresses
def _with_geocoders(photon, nominatim, openmeteo):
    saved = (geocode._suggest_photon, geocode._geocode_nominatim, geocode._geocode_openmeteo)
    geocode._suggest_photon, geocode._geocode_nominatim, geocode._geocode_openmeteo = (
        photon, nominatim, openmeteo)
    return saved


def _restore(saved):
    geocode._suggest_photon, geocode._geocode_nominatim, geocode._geocode_openmeteo = saved


def _nothing(*a, **k):
    raise ValueError("no match")


def test_address_without_town_uses_map_area_not_a_town_name():
    """'233 S Wacker' must resolve to the address near the map, never the town of
    Wacker, IL (the old fallback)."""
    seen = {}

    def photon(q, count, bias=None):
        seen["bias"] = bias
        return [{"label": "233 South Wacker Drive, Chicago, Illinois, United States",
                 "lat": 41.8787, "lng": -87.6360}]
    saved = _with_geocoders(photon, _nothing, lambda q: (42.06, -90.05, "Wacker, Illinois, US"))
    try:
        lat, lng, label = geocode.geocode("233 S Wacker", near=(41.88, -87.63))
    finally:
        _restore(saved)
    assert label.startswith("233 South Wacker") and abs(lng + 87.636) < 1e-6
    assert seen["bias"] == (41.88, -87.63)


def test_unknown_house_number_falls_back_to_street_and_is_flagged():
    photon = lambda q, c, bias=None: [{"label": "South La Grange Road, Mokena, Illinois, United States",
                                       "lat": 41.538, "lng": -87.850}]
    saved = _with_geocoders(photon, _nothing, _nothing)
    try:
        lat, lng, label = geocode.geocode("18901 S La Grange Rd, Mokena")
    finally:
        _restore(saved)
    assert label.startswith("South La Grange Road")
    assert geocode.missing_house_number("18901 S La Grange Rd, Mokena", label) == "18901"
    assert geocode.missing_house_number("11004 Carpenter St", "11004 Carpenter Street, Mokena") is None


def test_address_not_found_never_guesses_a_town():
    town_calls = []
    saved = _with_geocoders(lambda q, c, bias=None: [], _nothing,
                            lambda q: town_calls.append(q) or (1.0, 2.0, "Somewhere"))
    try:
        try:
            geocode.geocode("19100 S Wolf Rd, Mokena, IL")
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert "drop your start" in str(exc)
    finally:
        _restore(saved)
    assert town_calls == []


def test_digits_without_house_number_can_still_match_a_town():
    saved = _with_geocoders(lambda q, c, bias=None: [], _nothing,
                            lambda q: (41.53, -87.89, "Mokena, Illinois, US"))
    try:
        assert geocode.geocode("Mokena 60448")[2] == "Mokena, Illinois, US"
    finally:
        _restore(saved)
