"""Geocoding + type-ahead place suggestions (Open-Meteo / Nominatim / Photon)."""
from __future__ import annotations

import re

import requests


GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL = "https://photon.komoot.io/api"   # OSM geocoder built for type-ahead
USER_AGENT = "windroute/0.1 (personal cycling tool)"


# --------------------------------------------------------------------------- #
# Geocoding + wind (Open-Meteo, free, no key)
# --------------------------------------------------------------------------- #
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}


def geocode(place: str):
    """Return (lat, lng, label) for a location string.

    Accepts three forms, picked automatically so you can start a ride from an
    exact spot (e.g. the corner near your house that reaches the bike path),
    not just a town centroid:
      1. Raw coordinates ``"41.5358,-87.8890"`` -> used directly (most precise).
      2. A street address ``"233 S Wacker Dr, Chicago, IL"`` -> OSM Nominatim, which
         resolves house numbers / streets (Open-Meteo only knows town centroids).
      3. A town / "City, ST" name -> Open-Meteo (fast), falling back to Nominatim.
    """
    coords = _parse_coords(place)
    if coords:
        lat, lng = coords
        return lat, lng, f"{lat:.5f}, {lng:.5f}"

    # Anything with a digit (house number, route number, ZIP) is address-like and
    # belongs to Nominatim first; plain town names go to the faster Open-Meteo.
    if any(ch.isdigit() for ch in place):
        try:
            return _geocode_nominatim(place)
        except (ValueError, requests.RequestException):
            pass                                   # fall through to town geocoder

    try:
        return _geocode_openmeteo(place)
    except (ValueError, requests.RequestException):
        return _geocode_nominatim(place)           # odd names, or Open-Meteo rate-limited


def _parse_coords(place: str):
    """Parse a coordinate pair into (lat, lng), or None if it isn't one.

    Handles decimal ``"41.5267,-87.8717"`` and the degrees-minutes-seconds form
    Google Maps copies, ``"41°31'36.3\"N 87°52'18.0\"W"``.
    """
    dms = _parse_dms(place)
    if dms:
        return dms
    parts = place.replace(" ", "").split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
        return lat, lng
    return None


# DMS like 41°31'36.3"N — three numbers (deg/min/sec) then a hemisphere letter.
# Non-digit runs (\D+) absorb whatever symbols were used for °, ', ".
_DMS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)\D*([NSEW])",
    re.IGNORECASE)


def _parse_dms(place: str):
    """Parse a 'D°M\\'S\"H D°M\\'S\"H' degrees-minutes-seconds pair, else None."""
    matches = _DMS_RE.findall(place.strip())
    if len(matches) != 2:
        return None
    lat = lng = None
    for deg, minu, sec, hemi in matches:
        val = float(deg) + float(minu) / 60.0 + float(sec) / 3600.0
        hemi = hemi.upper()
        if hemi in ("S", "W"):
            val = -val
        if hemi in ("N", "S"):
            lat = val
        else:
            lng = val
    if lat is None or lng is None:
        return None
    if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
        return lat, lng
    return None


def _geocode_openmeteo(place: str):
    """Town / "City, ST" geocoding via Open-Meteo (no key). Raises if not found."""
    params: dict = {"count": 1}
    # "City, ST" / "City, State" notation: strip the suffix and add country filter
    if "," in place:
        city_part, region_part = place.split(",", 1)
        region_part = region_part.strip()
        if region_part.upper() in _US_STATES or len(region_part) > 2:
            params["name"] = city_part.strip()
            params["country"] = "US"
        else:
            params["name"] = place
    else:
        params["name"] = place

    r = requests.get(GEOCODE_URL, params=params, timeout=20)
    r.raise_for_status()
    results = r.json().get("results")
    if not results:
        raise ValueError(f"Could not find a location matching {place!r}")
    top = results[0]
    label = ", ".join(
        p for p in (top.get("name"), top.get("admin1"), top.get("country_code")) if p
    )
    return top["latitude"], top["longitude"], label


def suggest_places(query: str, count: int = 6):
    """Type-ahead suggestions for a partial query — street addresses AND towns.

    Backs the web form's location autocomplete. Primary source is Photon
    (photon.komoot.io), an OSM geocoder purpose-built for autocomplete, so house
    numbers and streets resolve as you type — unlike Nominatim, whose usage policy
    forbids per-keystroke queries, or Open-Meteo, which only knows town centroids.
    Falls back to Open-Meteo town search if Photon is unavailable. Returns a list of
    ``{"label", "lat", "lng"}``; never raises (returns [] on any problem).
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    items = _suggest_photon(q, count)
    return items if items else _suggest_openmeteo(q, count)


def _suggest_photon(query: str, count: int):
    """Address + place suggestions from Photon (GeoJSON). [] on failure."""
    try:
        r = requests.get(PHOTON_URL, params={"q": query, "limit": count, "lang": "en"},
                         headers={"User-Agent": USER_AGENT}, timeout=8)
        r.raise_for_status()
        features = r.json().get("features") or []
    except (requests.RequestException, ValueError):
        return []
    out, seen = [], set()
    for feat in features:
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates")        # [lng, lat]
        if not coords or len(coords) < 2:
            continue
        label = _photon_label(props)
        if label and label.lower() not in seen:
            seen.add(label.lower())
            out.append({"label": label, "lat": coords[1], "lng": coords[0]})
    return out


def _photon_label(props: dict) -> str:
    """Build a concise, geocodable label from a Photon feature's properties."""
    house, street, name = props.get("housenumber"), props.get("street"), props.get("name")
    if street:
        primary = f"{house} {street}" if house else street
    else:
        primary = name
    locality = (props.get("city") or props.get("town") or props.get("village")
                or props.get("district") or props.get("county"))
    parts = [primary, locality, props.get("state"),
             props.get("country") or props.get("countrycode")]
    label = []
    for p in parts:                       # keep order, drop blanks + adjacent dupes
        if p and (not label or label[-1].lower() != p.lower()):
            label.append(p)
    return ", ".join(label)


def _suggest_openmeteo(query: str, count: int):
    """Town/city fallback suggestions from Open-Meteo, ranked by population."""
    name = query.split(",", 1)[0].strip() if "," in query else query
    if len(name) < 2:
        return []
    try:
        r = requests.get(GEOCODE_URL, params={
            "name": name, "count": 20, "language": "en", "format": "json"}, timeout=8)
        r.raise_for_status()
        results = r.json().get("results") or []
    except (requests.RequestException, ValueError):
        return []
    nlow = name.lower()
    results.sort(key=lambda t: (
        not str(t.get("name", "")).lower().startswith(nlow),   # prefix matches first
        -(t.get("population") or 0),                            # then most populous
    ))
    out = []
    for top in results:
        label = ", ".join(
            p for p in (top.get("name"), top.get("admin1"), top.get("country_code")) if p
        )
        if label and "latitude" in top and "longitude" in top:
            out.append({"label": label, "lat": top["latitude"], "lng": top["longitude"]})
        if len(out) >= count:
            break
    return out


def _geocode_nominatim(place: str):
    """Street-address geocoding via OSM Nominatim (no key; handles house numbers).

    Nominatim's usage policy asks for a real User-Agent and modest volume — one
    call per plan is well within that. Builds a short label from the address parts
    instead of Nominatim's very long display_name.
    """
    r = requests.get(
        NOMINATIM_URL,
        params={"q": place, "format": "jsonv2", "limit": 1, "addressdetails": 1},
        headers={"User-Agent": USER_AGENT}, timeout=20,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        raise ValueError(f"Could not find a location matching {place!r}")
    top = results[0]
    addr = top.get("address", {})
    town = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("hamlet") or addr.get("suburb"))
    house = " ".join(x for x in (addr.get("house_number"), addr.get("road")) if x)
    label = ", ".join(p for p in (
        house or None, town, addr.get("state"),
        (addr.get("country_code") or "").upper() or None) if p)
    return float(top["lat"]), float(top["lon"]), label or top.get("display_name", place)
