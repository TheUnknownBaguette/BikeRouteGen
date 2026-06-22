"""EXPERIMENTAL, OFF BY DEFAULT — wind-biased routing via a self-hosted Valhalla.

Work-plan Task 7 "full version". This is the gated SEAM, not a verified feature:

  * It does NOTHING unless the env var ``WINDROUTE_VALHALLA_URL`` points at a Valhalla
    instance. With it unset (the default) the whole app routes on OpenRouteService
    exactly as before — `enabled()` is False and nothing here is called.
  * It is UNTESTED against a live Valhalla (none was available during development).
  * True per-edge wind biasing (cost an edge by the angle between its bearing and the
    headwind) needs a CUSTOM Valhalla costing model — stock `bicycle` costing has no
    such option. What's wired here routes the outbound leg through your own router
    (off the ORS quota) toward a wind-optimal turnaround; the wind line still comes
    from that turnaround geometry, not from per-edge costing. Treat this as a
    ready-to-extend starting point for when you stand up a server + custom costing.

Kept deliberately self-contained (only os + requests) so it can't affect the ORS path.
"""
from __future__ import annotations

import os

import requests

URL_ENV = "WINDROUTE_VALHALLA_URL"
USER_AGENT = "windroute/0.1 (personal cycling tool)"


def enabled() -> bool:
    """True only when a Valhalla endpoint is configured (default: False)."""
    return bool(os.environ.get(URL_ENV))


def _route_body(lat, lng, dlat, dlng) -> dict:
    """Valhalla /route request body for a bicycle leg (pure; unit-testable)."""
    return {
        "locations": [{"lat": lat, "lon": lng}, {"lat": dlat, "lon": dlng}],
        "costing": "bicycle",
        "costing_options": {"bicycle": {"bicycle_type": "Road",
                                        "use_roads": 0.4, "use_hills": 0.3}},
        "directions_options": {"units": "kilometers"},
    }


def wind_biased_leg(lat, lng, dlat, dlng, into_wind_bearing=None, timeout=40):
    """Route (lat,lng) -> (dlat,dlng) via the configured Valhalla. Returns [(lat,lng)].

    EXPERIMENTAL / untested-against-live (see module docstring). Raises on any error;
    the caller (`engine._make_wind_loop`) falls back to ORS, so a misconfigured or
    down Valhalla never breaks a plan. `into_wind_bearing` is accepted for the future
    custom-costing extension; stock bicycle costing ignores it.
    """
    base = os.environ[URL_ENV].rstrip("/")
    resp = requests.post(base + "/route", json=_route_body(lat, lng, dlat, dlng),
                         headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    shape = resp.json()["trip"]["legs"][0]["shape"]
    return _decode_polyline(shape, precision=6)          # Valhalla encodes at 1e-6


def _decode_polyline(encoded, precision=6):
    """Decode an encoded polyline (Google/Valhalla algorithm) -> [(lat, lng)]."""
    coords, index, lat, lng = [], 0, 0, 0
    factor = float(10 ** precision)
    length = len(encoded)
    while index < length:
        for is_lng in (False, True):
            shift, result = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += delta
            else:
                lat += delta
        coords.append((lat / factor, lng / factor))
    return coords
