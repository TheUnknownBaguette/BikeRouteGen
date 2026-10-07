"""Wind forecast + historical wind (Open-Meteo primary, US NWS fallback)."""
from __future__ import annotations

import datetime as dt
import math
import re

import requests

from .geocode import USER_AGENT
from .geometry import COMPASS_16, _destination
from .models import Wind, WindField


FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


# Wind-field sampling: the start plus a ring of points around it, so a route's
# far side can see a different wind than the start (lake breezes, fronts). The
# ring sits at ~1/3 of the ride distance (about where a loop's far side is).
FIELD_RING_POINTS = 6
FIELD_RING_FRAC = 1.0 / 3.0
FIELD_RING_MIN_KM = 3.0
FIELD_HOURS_BEFORE = 1        # hours of forecast kept before the start ...
FIELD_HOURS_AFTER = 12        # ... and after it (covers a 100 km ride at any pace)


def get_wind(lat: float, lng: float, when: dt.datetime,
             radius_km: float = None) -> Wind:
    """Wind forecast for the hour nearest `when` (naive local time).

    With `radius_km` (the ride's rough reach), the returned Wind also carries a
    `field`: hourly wind at the start and a ring of points around it, so scoring
    can use the wind you'll meet along the route as the ride goes on. Without it
    (or from the NWS fallback, which is single-point) `field` is the start-point
    hourly series or None.

    Open-Meteo is the primary source (free, no key, worldwide). If it fails — most
    notably HTTP 429 when running from a shared cloud IP that Open-Meteo throttles
    (e.g. a free hosting tier) — fall back to the US National Weather Service
    (`api.weather.gov`, keyless, US-only). Locally Open-Meteo just works and NWS is
    never touched.

    If BOTH sources fail (e.g. a non-US start when Open-Meteo is throttled — NWS
    404s outside the US), return a calm `Wind` flagged `known=False` rather than
    letting the exception kill the whole plan. `evaluate` neutralizes the wind term
    for an unknown wind and the planner surfaces a note, so a route still comes back.
    """
    fetch_errors = (requests.RequestException, ValueError, KeyError, IndexError)
    try:
        return _wind_from_open_meteo(lat, lng, when, radius_km)
    except fetch_errors:
        pass
    try:
        return _wind_from_nws(lat, lng, when)
    except fetch_errors:
        return Wind(direction_from_deg=0.0, speed_mph=0.0, gust_mph=0.0,
                    valid_time="", known=False)


def _wind_from_open_meteo(lat: float, lng: float, when: dt.datetime,
                         radius_km: float = None) -> Wind:
    points = [(lat, lng)]
    if radius_km:
        ring = max(FIELD_RING_MIN_KM, radius_km * FIELD_RING_FRAC)
        points += [_destination(lat, lng, i * 360.0 / FIELD_RING_POINTS, ring)
                   for i in range(FIELD_RING_POINTS)]
    r = requests.get(
        FORECAST_URL,
        params={
            # Open-Meteo takes comma-separated coordinates for several locations
            # in one request and then returns a list (one object per location).
            "latitude": ",".join(f"{p[0]:.4f}" for p in points),
            "longitude": ",".join(f"{p[1]:.4f}" for p in points),
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": 7,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    hourlies = [d["hourly"] for d in (data if isinstance(data, list) else [data])]
    w = _wind_from_hourly(hourlies[0], when)
    w.field = _field_from_hourlies(points, hourlies, when)
    return w


def _field_from_hourlies(points, hourlies, when: dt.datetime):
    """Build a WindField from per-point Open-Meteo `hourly` blocks (same times),
    trimmed to the hours around the ride. None if the window has no data."""
    t0 = when.replace(minute=0, second=0, microsecond=0)
    times = [dt.datetime.fromisoformat(t) for t in hourlies[0]["time"]]
    keep = [i for i, t in enumerate(times)
            if -FIELD_HOURS_BEFORE <= (t - t0).total_seconds() / 3600 <= FIELD_HOURS_AFTER]
    keep = [i for i in keep
            if all(h["wind_speed_10m"][i] is not None
                   and h["wind_direction_10m"][i] is not None for h in hourlies)]
    if not keep:
        return None
    hours = [(times[i] - when).total_seconds() / 3600 for i in keep]
    us, vs = [], []
    for h in hourlies:
        u_row, v_row = [], []
        for i in keep:
            spd = float(h["wind_speed_10m"][i])
            rad = math.radians(float(h["wind_direction_10m"][i]))
            u_row.append(spd * math.sin(rad))
            v_row.append(spd * math.cos(rad))
        us.append(u_row)
        vs.append(v_row)
    return WindField(points=list(points), hours=hours, u=us, v=vs)


def _wind_from_nws(lat: float, lng: float, when: dt.datetime) -> Wind:
    """US National Weather Service hourly wind (keyless, US-only).

    Two calls: /points/{lat},{lng} gives the hourly-forecast URL, then that URL
    returns hourly periods with windSpeed ('10 mph' / '5 to 10 mph') and
    windDirection (a compass label). NWS requires a descriptive User-Agent and
    only covers US locations (a point outside the US 404s).
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    pt = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lng:.4f}",
                      headers=headers, timeout=20)
    pt.raise_for_status()
    hourly_url = pt.json()["properties"]["forecastHourly"]
    fc = requests.get(hourly_url, headers=headers, timeout=20)
    fc.raise_for_status()
    periods = fc.json().get("properties", {}).get("periods") or []
    if not periods:
        raise ValueError("NWS returned no forecast periods for this location")

    target = when.replace(minute=0, second=0, microsecond=0)
    best, best_diff = None, None
    for per in periods:
        t = dt.datetime.fromisoformat(per["startTime"]).replace(tzinfo=None)
        diff = abs((t - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff, best = diff, per
    # The same periods give a start-point-only wind field (time, no space), so a
    # changing wind is still accounted for when Open-Meteo is unavailable.
    hourly = {"time": [], "wind_speed_10m": [], "wind_direction_10m": []}
    for per in periods:
        hourly["time"].append(str(per["startTime"])[:16])
        hourly["wind_speed_10m"].append(_parse_mph(per.get("windSpeed")))
        hourly["wind_direction_10m"].append(_compass_to_deg(per.get("windDirection")))
    return Wind(
        direction_from_deg=_compass_to_deg(best.get("windDirection")),
        speed_mph=_parse_mph(best.get("windSpeed")),
        gust_mph=_parse_mph(best.get("windGust")),
        valid_time=str(best.get("startTime", ""))[:16],   # 'YYYY-MM-DDTHH:MM'
        field=_field_from_hourlies([(lat, lng)], [hourly], when),
    )


def _compass_to_deg(label) -> float:
    """A 16-point compass label ('SSW') -> degrees the wind comes FROM (0=N)."""
    if not label:
        return 0.0
    try:
        return COMPASS_16.index(str(label).strip().upper()) * 22.5
    except ValueError:
        return 0.0


def _parse_mph(text) -> float:
    """Pull a speed out of an NWS string like '10 mph' or '5 to 10 mph' (-> 10)."""
    if not text:
        return 0.0
    nums = re.findall(r"\d+(?:\.\d+)?", str(text))
    return max(float(n) for n in nums) if nums else 0.0


def get_wind_historical(lat: float, lng: float, when: dt.datetime) -> Wind:
    """Wind that actually blew at the hour nearest `when` (a past ride time).

    `get_wind` only covers the 7-day forecast, so backfilling the wind for a
    recorded trip needs the Open-Meteo archive. The archive lags real time by a
    few days, so for very recent dates we fall back to the forecast endpoint's
    `past_days` window (which reaches back up to ~92 days). `when` is naive local.
    """
    if when.tzinfo is not None:
        # RWGPS timestamps carry the ride's local offset; drop it to a naive
        # local wall-clock time, which is what Open-Meteo (timezone=auto) returns
        # and what _nearest_time_index compares against. Mixing the two raises
        # "can't subtract offset-naive and offset-aware datetimes".
        when = when.replace(tzinfo=None)
    days_ago = (dt.datetime.now() - when).days
    if days_ago <= 7:
        return _wind_from_forecast_past(lat, lng, when, past_days=min(92, days_ago + 2))
    return _wind_from_archive(lat, lng, when)


def _wind_from_archive(lat: float, lng: float, when: dt.datetime) -> Wind:
    day = when.strftime("%Y-%m-%d")
    r = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": lat,
            "longitude": lng,
            "start_date": day,
            "end_date": day,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "wind_speed_unit": "mph",
            "timezone": "auto",
        },
        timeout=30,
    )
    r.raise_for_status()
    h = r.json().get("hourly") or {}
    if not h.get("time"):
        # Archive has no data yet for this (recent) date — fall back to forecast.
        return _wind_from_forecast_past(lat, lng, when, past_days=92)
    return _wind_from_hourly(h, when)


def _wind_from_forecast_past(lat: float, lng: float, when: dt.datetime,
                             past_days: int) -> Wind:
    r = requests.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lng,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "past_days": max(1, past_days),
            "forecast_days": 1,
        },
        timeout=30,
    )
    r.raise_for_status()
    return _wind_from_hourly(r.json()["hourly"], when)


def _wind_from_hourly(h: dict, when: dt.datetime) -> Wind:
    """Build a Wind from an Open-Meteo `hourly` block at the hour nearest `when`."""
    idx = _nearest_time_index(h["time"], when)
    gusts = h.get("wind_gusts_10m") or []
    gust = gusts[idx] if idx < len(gusts) and gusts[idx] is not None else 0.0
    return Wind(
        direction_from_deg=float(h["wind_direction_10m"][idx]),
        speed_mph=float(h["wind_speed_10m"][idx]),
        gust_mph=float(gust),
        valid_time=h["time"][idx],
    )


def _nearest_time_index(times, when: dt.datetime) -> int:
    target = when.replace(minute=0, second=0, microsecond=0)
    best_diff, best_i = None, 0
    for i, t in enumerate(times):
        ti = dt.datetime.fromisoformat(t)
        diff = abs((ti - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff, best_i = diff, i
    return best_i
