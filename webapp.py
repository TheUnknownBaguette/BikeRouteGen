"""Local web front-end for windroute — run it, open a browser, plan a ride.

A thin Flask layer over `windroute.planner` + `windroute.render` (the same pipeline
the CLI uses). Run it and a browser opens to a form; submit and you get the
recommended route plus two alternatives, each with its map and a GPX download.

    pip install -r requirements.txt
    python webapp.py            # or double-click run.bat

It binds to 127.0.0.1 only (local machine, not exposed to your network). Reads the
OpenRouteService key from ORS_API_KEY, exactly like the CLI.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, jsonify, render_template, request, send_from_directory

from windroute import engine, render, planner

app = Flask(__name__)
# Cache-buster for our own CSS/JS: changes on every (re)start, i.e. every deploy, so
# browsers never run a new page against a stale stylesheet or script.
app.jinja_env.globals["asset_v"] = str(int(time.time()))
# Reject oversized request bodies outright — the form is tiny.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

OUT_DIR = Path(__file__).parent / "static" / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_AGE_S = 3600                      # delete generated maps/gpx older than this

# Each plan fans out to ~12-15 OpenRouteService calls, so a public instance needs a
# throttle to protect the shared free-tier quota from casual abuse. Simple in-memory
# sliding window per client IP (per worker process — good enough for a hobby host).
# SCALING CAVEAT: this state is per-process, so running multiple waitress threads/
# workers or multiple instances multiplies the effective limit by that count. If you
# ever raise the worker count, move this to a shared store (e.g. Redis) or the limit
# silently weakens. See CODE_HEALTH_WORKPLAN Task D3.
RL_MAX = 12                          # max plans ...
RL_WINDOW_S = 300                    # ... per IP per this many seconds
_rl_lock = threading.Lock()
_rl_hits: dict[str, list[float]] = defaultdict(list)


def _client_ip() -> str:
    """Best-effort client IP, honoring the host's proxy header."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def _rate_limited(ip: str) -> bool:
    """Record a hit for ip; return True if it has exceeded the window allowance."""
    now = time.time()
    with _rl_lock:
        recent = [t for t in _rl_hits[ip] if t > now - RL_WINDOW_S]
        if len(recent) >= RL_MAX:
            _rl_hits[ip] = recent
            return True
        recent.append(now)
        _rl_hits[ip] = recent
        # Opportunistically drop IPs that have aged out so the map can't grow forever.
        if len(_rl_hits) > 2048:
            for k in [k for k, v in _rl_hits.items()
                      if not v or v[-1] < now - RL_WINDOW_S]:
                _rl_hits.pop(k, None)
        return False


def _clamp(value, lo: float, hi: float, default: float) -> float:
    """Parse value as a number and clamp it into [lo, hi]; default if unparseable.

    The HTML form's min/max are client-side only, so all numeric inputs are
    re-bounded here (a high `candidates`, especially, multiplies routing-API calls).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:                       # NaN
        return default
    return max(lo, min(hi, v))


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    # OSM tile policy requires a Referer on browser tile requests; send only the
    # origin cross-site (no path/query, so route params never leak).
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https://tile.openstreetmap.org "
        "https://*.tile-cyclosm.openstreetmap.fr https://*.tile.opentopomap.org; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    # Only assert HSTS when actually reached over HTTPS (Render terminates TLS and
    # forwards the original scheme), so a plain-HTTP local run isn't pinned to https.
    if request.headers.get("X-Forwarded-Proto", request.scheme) == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

# Defaults shown in the form (match the CLI's).
FORM_DEFAULTS = {
    "location": "Chicago, IL", "distance": "30", "unit": "mi", "start": "now",
    "ride_type": "road", "speed": "17", "shapes": ["loop", "lollipop", "rectangle"],
    "surface_source": "ors", "ride_area": "", "tolerance": "3",
    "candidates": "12", "corrections": True, "classify": False, "refine": False,
}
ALL_SHAPES = ["loop", "lollipop", "rectangle", "out-and-back"]


def _sweep_old_files():
    """Drop maps/GPX from earlier sessions so static/out doesn't grow forever."""
    cutoff = time.time() - MAX_AGE_S
    for f in OUT_DIR.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# Hidden fields that carry an exact picked start point through an "Edit plan" trip.
PICKED_FIELDS = ("picked_lat", "picked_lng", "picked_label")
CHECKBOXES = ("corrections", "classify", "refine")


def _form_values(f):
    """Form values from a submitted form or query string, over the defaults."""
    vals = {**FORM_DEFAULTS, **{k: f.get(k, "") for k in FORM_DEFAULTS
                                if k not in ("shapes",) + CHECKBOXES}}
    vals["shapes"] = f.getlist("shapes") or FORM_DEFAULTS["shapes"]
    for k in CHECKBOXES:
        vals[k] = k in f
    for k in PICKED_FIELDS:
        vals[k] = f.get(k, "")
    return vals


@app.route("/")
def index():
    # "Edit plan" from a results page comes back here with the plan's inputs in
    # the query string (?edit=1&...), so the form opens as you left it.
    d = _form_values(request.args) if request.args.get("edit") else FORM_DEFAULTS
    return render_template("index.html", d=d, all_shapes=ALL_SHAPES, error=None)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/suggest")
def suggest():
    """Type-ahead place suggestions for the location field (JSON). Same-origin
    proxy to the geocoder so the page's strict CSP can stay default-src 'self'."""
    items = engine.suggest_places(request.args.get("q", "")[:80], count=6)
    return jsonify(items)


def _reshow(f, shapes, error, status):
    """Re-render the form with the submitted values and a message."""
    submitted = _form_values(f)
    submitted["shapes"] = shapes
    return render_template("index.html", d=submitted, all_shapes=ALL_SHAPES,
                           error=error), status


@app.route("/plan", methods=["POST"])
def plan():
    f = request.form
    shapes = f.getlist("shapes") or FORM_DEFAULTS["shapes"]

    if _rate_limited(_client_ip()):
        return _reshow(f, shapes, "Too many plans in a short time — this is a small "
                       "shared instance. Please wait a few minutes and try again.", 429)

    # If the user picked an autocomplete suggestion (and didn't then edit the text),
    # route from its exact coordinates but keep the readable label for display.
    location_arg = f.get("location", "").strip()
    label_override = None
    plat, plng = f.get("picked_lat", "").strip(), f.get("picked_lng", "").strip()
    plabel = f.get("picked_label", "").strip()
    if plat and plng and plabel and plabel == location_arg:
        try:
            location_arg = f"{float(plat)},{float(plng)}"
            label_override = plabel
        except ValueError:
            location_arg = f.get("location", "").strip()

    try:
        result = planner.plan_routes(
            location=location_arg,
            location_label=label_override,
            distance=_clamp(f.get("distance", 0), 1, 200, 30),
            unit=f.get("unit", "mi"),
            start=f.get("start", "now").strip() or "now",
            ride_type=f.get("ride_type", "road"),
            shapes=shapes,
            surface_source=f.get("surface_source", "ors"),
            ride_area=(f.get("ride_area", "").strip() or None),
            tolerance=_clamp(f.get("tolerance", 3), 0, 50, 3),
            speed=_clamp(f.get("speed", 17), 3, 40, 17),
            candidates=int(_clamp(f.get("candidates", 12), 1, 20, 12)),
            corrections=("corrections" in f),
            classify=("classify" in f),
            refine=("refine" in f),
            api_key=os.environ.get("ORS_API_KEY"),
            n_alternatives=2,
        )
    except (ValueError, RuntimeError) as exc:      # expected: bad location, no key, no routes…
        # These carry user-friendly text from the planner; safe to show.
        return _reshow(f, shapes, str(exc)[:300], 400)
    except Exception:                              # unexpected: log it, stay generic
        app.logger.exception("plan_routes failed")
        return _reshow(f, shapes, "Something went wrong building that plan. Check your "
                       "inputs and try again; if it persists the routing or weather "
                       "service may be temporarily unavailable.", 500)

    # Observability (Task C2): per-plan ORS usage + process total, so a hosted
    # instance can see how fast it's burning the ~2000/day free-tier quota.
    app.logger.info("plan ok: %d ORS calls (process total %d)",
                    result.ors_calls, engine.ors_call_total())

    _sweep_old_files()
    token = uuid.uuid4().hex[:8]
    ride_type = f.get("ride_type", "road")
    unit = "km" if f.get("unit", "mi") == "km" else "mi"
    per_unit = 1.0 if unit == "km" else 1.0 / 1.609344
    wind = result.wind

    # Every ranked candidate gets a card, a map line and a GPX: the recommended +
    # alternatives first (each with its headline), then the rest in rank order.
    option_of = {id(o.candidate): o for o in result.options}
    order = ([o.candidate for o in result.options]
             + [c for c in result.ranked if id(c) not in option_of])
    rank_of = {id(c): i + 1 for i, c in enumerate(result.ranked)}
    dlnames = render.dedupe_names([
        render.route_basename(result.when, c.distance_km, unit, c.shape,
                              wind.direction_from_deg) for c in order])
    routes, map_routes = [], []
    for i, c in enumerate(order):
        opt = option_of.get(id(c))
        role = opt.role if opt else "candidate"
        headline = opt.headline if opt else c.shape.capitalize()
        color = ROUTE_COLORS[i] if opt and i < len(ROUTE_COLORS) else CANDIDATE_COLOR
        base = OUT_DIR / f"{token}-{i}"
        dist_num = f"{c.distance_km * per_unit:.1f}"
        title = f"{dist_num} {unit} {ride_type} {c.shape} - {headline}"
        render.write_gpx(c.coords, str(base.with_suffix(".gpx")), name=title)
        ride_time = engine.scoring._hhmm(c.ride_hours) if c.ride_hours else ""
        wind_line = engine.wind_summary(c) if wind.known else "no wind forecast"
        routes.append({
            "id": i, "role": role, "headline": headline, "rank": rank_of.get(id(c), i + 1),
            "color": color, "shape": c.shape, "dist": f"{dist_num} {unit}",
            "dist_num": dist_num, "climb": f"{c.ascent_m:.0f} m", "ride_time": ride_time,
            "verdict": engine.wind_verdict(c), "wind_score": c.wind_score,
            "wind_line": wind_line, "reasons": _card_reasons(opt.reasons if opt else [], unit),
            "gravel_pct": c.unpaved_frac * 100, "hwy_pct": c.busy_frac * 100,
            "path_pct": c.path_frac * 100, "lane_pct": c.bikelane_frac * 100,
            "unrideable_pct": c.unrideable_frac * 100, "score": c.total_score,
            "gpx": f"{base.name}.gpx", "dlname": f"{dlnames[i]}.gpx",
        })
        meta = [f"{dist_num} {unit}", ride_time, f"+{c.ascent_m:.0f} m", wind_line]
        card = routes[-1]
        map_routes.append({
            "id": i, "color": color, "pick": role != "candidate", "title": headline,
            "meta": " · ".join(x for x in meta if x),
            "coords": [[round(lat, 5), round(lng, 5)] for lat, lng in c.coords],
            "eles": [round(e) for e in c.eles] if c.eles else [],
            # the card's own fields, so a shared link can redraw it with no server
            "card": {k: card[k] for k in SHARED_CARD_FIELDS},
        })

    # "Edit plan" goes back to the form with exactly these inputs.
    edit_pairs = [("edit", "1")] + list(f.items(multi=True))
    pace = _clamp(f.get("speed", 17), 3, 40, 17)
    dist = _clamp(f.get("distance", 0), 1, 200, 30)
    meta = (f"{dist:g} {unit} {ride_type} · "
            f"{pace:g} {'km/h' if unit == 'km' else 'mph'} pace")
    notes = result.notes
    if result.region is not None:              # terrain archetype as the first note
        notes = [result.region.note] + notes
    valid = _parse_iso(wind.valid_time)
    wind_ctx = {"from": engine.compass_label(wind.direction_from_deg),
                "deg": round(wind.direction_from_deg), "mph": round(wind.speed_mph, 1),
                "gust": round(wind.gust_mph, 1), "known": wind.known,
                "when": _clock(valid) if valid else wind.valid_time}
    when_str = f"{result.when:%a %b} {result.when.day}, {_clock(result.when)}"
    timeline = _wind_timeline(result, order)
    # Everything the map (and a share link) needs, as one JSON blob in the page.
    payload = {
        "unit": unit, "ride_type": ride_type,
        "plan": {"label": result.location_label, "when": when_str, "meta": meta,
                 "start": result.when.isoformat(timespec="minutes"),
                 "pace_mph": round(pace if unit == "mi" else pace / 1.609344, 2),
                 "wind": wind_ctx, "timeline": timeline},
        "field": _field_json(wind),
        "routes": map_routes,
    }
    return render_template(
        "results.html", label=result.location_label, when_str=when_str,
        meta=meta, unit=unit, ride_type=ride_type, wind=wind_ctx,
        timeline=timeline, notes=notes, routes=routes,
        payload=payload, edit_url="/?" + urlencode(edit_pairs))


@app.route("/share")
def share():
    """A shared plan. The routes live in the link's #fragment (never sent to the
    server); the page decodes and draws them client-side."""
    return render_template("share.html")


# Route colors: the recommended route + alternatives (distinct and readable on the
# muted map in light and dark), then one shared color for the other candidates.
ROUTE_COLORS = ["#2563eb", "#ea580c", "#0d9488", "#c026d3"]
CANDIDATE_COLOR = "#7c3aed"
# Card fields carried in the page payload (and so in share links).
SHARED_CARD_FIELDS = ("role", "headline", "rank", "shape", "dist", "climb", "ride_time",
                      "verdict", "wind_score", "wind_line", "reasons", "gravel_pct",
                      "hwy_pct", "lane_pct", "unrideable_pct", "dlname")


def _field_json(wind):
    """The hourly wind field for the map's wind animation (small: ~7 points x ~14
    hours), or None without a forecast."""
    f = wind.field
    if not (wind.known and f is not None):
        return None
    return {"points": [[round(a, 4), round(b, 4)] for a, b in f.points],
            "hours": [round(h, 3) for h in f.hours],
            "u": [[round(x, 2) for x in row] for row in f.u],
            "v": [[round(x, 2) for x in row] for row in f.v]}


_KM_RE = re.compile(r"(\d+(?:\.\d+)?) km\b")


def _card_reasons(reasons, unit):
    """Option bullets for a card: drop the 'N km, +M m, ~T' line (the card's stat
    chips already show distance/climb/time) and put distances in the plan's unit."""
    out = [r for r in reasons if not re.match(r"\d+(?:\.\d+)? km, \+", r)]
    if unit == "mi":
        out = [_KM_RE.sub(lambda m: f"{float(m.group(1)) / 1.609344:.1f} mi", r) for r in out]
    return out


def _clock(t):
    """'6:00 AM' (no leading zero, portable across platforms)."""
    return f"{t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


def _parse_iso(text):
    try:
        return dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _wind_timeline(result, order):
    """Hourly wind at the start across the top pick's ride time:
    [{time, deg, mph, from}, ...]. Empty without a forecast field."""
    wind = result.wind
    if not (wind.known and wind.field is not None and order):
        return []
    top = order[0]
    hours = max(1, math.ceil(top.ride_hours or 1.0))
    lat, lng = top.coords[0]
    base = result.when.replace(minute=0, second=0, microsecond=0)
    out = []
    for h in range(hours + 1):
        t = base + dt.timedelta(hours=h)
        deg, mph = wind.field.at(lat, lng, (t - result.when).total_seconds() / 3600)
        out.append({"time": _clock(t).replace(":00", ""), "deg": round(deg),
                    "mph": round(mph, 1), "from": engine.compass_label(deg),
                    "h": round((t - result.when).total_seconds() / 3600, 3)})
    return out


@app.route("/download/<path:name>")
def download(name):
    """Serve a generated GPX as a file download."""
    return send_from_directory(OUT_DIR, name, as_attachment=True)


def _open_browser(port):
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    import threading
    # HOST/PORT come from the environment so this one file runs both ways:
    #   - locally (defaults to 127.0.0.1:5000 and pops your browser), and
    #   - on your own box later (set HOST=0.0.0.0 to expose it on your network).
    # A hosted free service instead runs a production server (see Procfile:
    # `waitress-serve webapp:app`), which imports `app` and never reaches this block.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    if host in ("127.0.0.1", "localhost"):           # local dev convenience
        threading.Timer(1.0, lambda: _open_browser(port)).start()
    app.run(host=host, port=port, debug=False)
