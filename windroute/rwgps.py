"""Ride with GPS API client (v1).

A pure HTTP layer over the Ride with GPS API: exchange a login for a long-lived
auth token, page through your recorded *trips*, fetch a trip's track, and pull
the bits we care about out of the JSON. No printing, no scoring — a front-end
calls these and feeds the results to `learn`.

Auth (personal single-user use → Basic auth):
  1. Create an API client at https://ridewithgps.com/settings/developers to get
     an `api_key`.
  2. POST your email/password once to /auth_tokens.json (with the api_key header)
     to get a long-lived `auth_token`.
  3. Every request thereafter sends both as headers:
       x-rwgps-api-key:   <api_key>
       x-rwgps-auth-token: <auth_token>

The exact request/response envelopes aren't fully nailed down from the public
docs, so the parsers here are deliberately defensive: track points may use the
legacy compact keys (y=lat, x=lng, e=elev) or spelled-out ones, and the list /
summary keys are probed against a few likely names. `RwgpsError` carries a raw
snippet so an unexpected shape is easy to diagnose on the first real call.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

RWGPS_BASE = "https://ridewithgps.com/api/v1"
USER_AGENT = "windroute/0.1 (personal cycling tool)"
DEFAULT_TIMEOUT = 30
METERS_PER_MILE = 1609.344


class RwgpsError(RuntimeError):
    """An API call failed or returned an unexpected shape (carries a raw snippet)."""


# --------------------------------------------------------------------------- #
# Low-level request helpers
# --------------------------------------------------------------------------- #
def _headers(api_key: str, auth_token: str | None = None) -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json",
         "x-rwgps-api-key": api_key}
    if auth_token:
        h["x-rwgps-auth-token"] = auth_token
    return h


def _get(path: str, api_key: str, auth_token: str, params: dict | None = None,
         timeout: int = DEFAULT_TIMEOUT) -> dict:
    url = f"{RWGPS_BASE}/{path.lstrip('/')}"
    r = requests.get(url, headers=_headers(api_key, auth_token),
                     params=params or {}, timeout=timeout)
    return _json_or_raise(r, url)


def _json_or_raise(r: requests.Response, url: str) -> dict:
    if r.status_code >= 400:
        raise RwgpsError(f"{r.request.method} {url} -> HTTP {r.status_code}: "
                         f"{r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        raise RwgpsError(f"{url} returned non-JSON: {r.text[:300]}")


def _first(d: dict, *keys, default=None):
    """Return the first present, non-None value among `keys` (handles API drift)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def get_auth_token(email: str, password: str, api_key: str,
                   timeout: int = DEFAULT_TIMEOUT) -> tuple[str, int | None]:
    """Exchange email/password for a long-lived auth token.

    Returns (auth_token, user_id). Raises RwgpsError on bad credentials or an
    unexpected response shape.
    """
    url = f"{RWGPS_BASE}/auth_tokens.json"
    body = {"user": {"email": email, "password": password}}
    r = requests.post(url, headers=_headers(api_key), json=body, timeout=timeout)
    data = _json_or_raise(r, url)
    # Probe the likely shapes: {auth_token: {auth_token, user_id}} or flat.
    node = _first(data, "auth_token", "auth_tokens", default=data)
    if isinstance(node, list) and node:
        node = node[0]
    token = _first(node, "auth_token", "token") if isinstance(node, dict) else None
    if not token and isinstance(data, dict):
        token = _first(data, "token")
    if not token:
        raise RwgpsError(f"no auth token in response: {json.dumps(data)[:300]}")
    user_id = None
    for src in (node, data):
        if isinstance(src, dict):
            user_id = _first(src, "user_id", "user", "id")
            if isinstance(user_id, dict):
                user_id = _first(user_id, "id")
            if user_id is not None:
                break
    return token, user_id


def get_current_user(api_key: str, auth_token: str,
                     timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Fetch the authenticated user (used to verify a pasted token). Raises
    RwgpsError on a bad api_key/auth_token pair."""
    data = _get("users/current.json", api_key, auth_token, timeout=timeout)
    return _first(data, "user", default=data)


# --------------------------------------------------------------------------- #
# Trips
# --------------------------------------------------------------------------- #
def list_trips(api_key: str, auth_token: str, max_trips: int | None = None,
               page_size: int = 50, timeout: int = DEFAULT_TIMEOUT):
    """Yield trip summary dicts (raw API shape) across all pages, newest first.

    Stops after `max_trips` if given. Pagination follows the documented
    record_count / page_count / next_page_url meta.
    """
    page, yielded = 1, 0
    while True:
        data = _get("trips.json", api_key, auth_token,
                    params={"page": page, "page_size": page_size}, timeout=timeout)
        rows = _first(data, "results", "trips", "items", default=[])
        if isinstance(data, list):                       # bare array fallback
            rows = data
        if not rows:
            break
        for row in rows:
            yield row
            yielded += 1
            if max_trips is not None and yielded >= max_trips:
                return
        # Pagination meta is nested under meta.pagination (with a top-level
        # fallback for older/other shapes): {meta:{pagination:{page_count,
        # next_page_url,...}}}. Loop by page_count when known, else follow
        # next_page_url, else stop.
        meta = _first(data, "meta", default={}) or {}
        pg = (meta.get("pagination") or {}) if isinstance(meta, dict) else {}
        page_count = _first(pg, "page_count", "pages") or _first(data, "page_count", "pages")
        next_url = _first(pg, "next_page_url") or _first(data, "next_page_url")
        if page_count is not None:
            if page >= int(page_count):
                break
        elif not next_url:
            break
        page += 1


def get_trip(api_key: str, auth_token: str, trip_id, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Fetch a single trip's full JSON (including track points)."""
    data = _get(f"trips/{trip_id}.json", api_key, auth_token, timeout=timeout)
    # Detail is usually wrapped: {trip: {...}}.
    return _first(data, "trip", default=data)


# --------------------------------------------------------------------------- #
# Parsing (defensive — key names vary between API generations)
# --------------------------------------------------------------------------- #
def parse_track_points(trip_json: dict) -> list[tuple[float, float]]:
    """Return [(lat, lng), ...] from a trip's track points.

    Accepts the legacy compact keys (y=lat, x=lng) and the spelled-out
    (lat/lng, latitude/longitude) variants. Skips malformed points.
    """
    trip = _first(trip_json, "trip", default=trip_json)
    pts_raw = _first(trip, "track_points", "trackpoints", "points", default=[])
    coords: list[tuple[float, float]] = []
    for p in pts_raw:
        if not isinstance(p, dict):
            continue
        lat = _first(p, "y", "lat", "latitude")
        lng = _first(p, "x", "lng", "lon", "longitude")
        if lat is None or lng is None:
            continue
        try:
            coords.append((float(lat), float(lng)))
        except (TypeError, ValueError):
            continue
    return coords


def _parse_departed(trip: dict) -> dt.datetime | None:
    raw = _first(trip, "departed_at", "started_at", "created_at", "first_lat_lng_at")
    if not raw:
        return None
    try:
        # API timestamps are ISO-8601, often with a Z or offset.
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def trip_summary(trip_json: dict) -> dict:
    """Pull the fields we report on out of a trip (list row or detail)."""
    trip = _first(trip_json, "trip", default=trip_json)
    dist_m = _first(trip, "distance", "distance_meters", default=0.0) or 0.0
    ascent = _first(trip, "elevation_gain", "total_elevation_gain", "ascent", default=0.0)
    departed = _parse_departed(trip)
    return {
        "id": _first(trip, "id", "trip_id"),
        "name": _first(trip, "name", "title", default="") or "",
        "distance_km": float(dist_m) / 1000.0,
        "ascent_m": float(ascent or 0.0),
        "departed_at": departed.isoformat() if departed else None,
        "n_points": len(_first(trip, "track_points", "trackpoints", "points", default=[])),
        # RWGPS metadata used to filter to real rides and to classify shape.
        "activity_type": (_first(trip, "activity_type", default="") or "").lower(),
        "stationary": bool(_first(trip, "stationary", default=False)),
        "track_type": (_first(trip, "track_type", default="") or "").lower(),
        "terrain": (_first(trip, "terrain", default="") or "").lower(),
    }


def is_outdoor_cycling(summary: dict) -> bool:
    """True for a real outdoor bike ride (excludes walks, hikes, indoor trainer)."""
    at = summary.get("activity_type") or ""
    if not at.startswith("cycling"):
        return False
    if summary.get("stationary") or "indoor" in at:
        return False
    return True


# --------------------------------------------------------------------------- #
# Credential storage (~/.windroute/rwgps.json, mirrors the corrections cache)
# --------------------------------------------------------------------------- #
def default_creds_path() -> Path:
    return Path.home() / ".windroute" / "rwgps.json"


@dataclass
class Credentials:
    api_key: str = ""
    auth_token: str = ""
    user_id: int | None = None

    @property
    def ok(self) -> bool:
        return bool(self.api_key and self.auth_token)

    @classmethod
    def load(cls, path=None) -> "Credentials":
        """Load creds, with env vars (RWGPS_API_KEY / RWGPS_AUTH_TOKEN) taking
        precedence over the on-disk file so a terminal can override."""
        p = Path(path) if path else default_creds_path()
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
        return cls(
            api_key=os.environ.get("RWGPS_API_KEY") or data.get("api_key", ""),
            auth_token=os.environ.get("RWGPS_AUTH_TOKEN") or data.get("auth_token", ""),
            user_id=data.get("user_id"),
        )

    def save(self, path=None) -> Path:
        p = Path(path) if path else default_creds_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return p


# --------------------------------------------------------------------------- #
# Local trip cache (~/.windroute/trips/<id>.json) so analysis doesn't re-hit
# the API and stays usable offline.
# --------------------------------------------------------------------------- #
def trips_dir() -> Path:
    return Path.home() / ".windroute" / "trips"


def cached_trip_ids() -> set:
    d = trips_dir()
    return {p.stem for p in d.glob("*.json")} if d.exists() else set()


def save_trip(trip_json: dict) -> Path:
    """Persist one trip's full JSON, keyed by its id."""
    trip = _first(trip_json, "trip", default=trip_json)
    tid = _first(trip, "id", "trip_id")
    if tid is None:
        raise RwgpsError("trip has no id; cannot cache")
    d = trips_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{tid}.json"
    p.write_text(json.dumps(trip_json), encoding="utf-8")
    return p


def load_cached_trips():
    """Yield (summary, coords) for every cached trip, newest departure first."""
    items = []
    for p in trips_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        items.append((trip_summary(data), parse_track_points(data)))
    items.sort(key=lambda it: it[0].get("departed_at") or "", reverse=True)
    return items
