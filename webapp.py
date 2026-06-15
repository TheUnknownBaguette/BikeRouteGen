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

import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from windroute import engine, render, planner

app = Flask(__name__)
# Reject oversized request bodies outright — the form is tiny.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

OUT_DIR = Path(__file__).parent / "static" / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_AGE_S = 3600                      # delete generated maps/gpx older than this

# Each plan fans out to ~12-15 OpenRouteService calls, so a public instance needs a
# throttle to protect the shared free-tier quota from casual abuse. Simple in-memory
# sliding window per client IP (per worker process — good enough for a hobby host).
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
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
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
    "ride_type": "road", "shapes": ["loop", "lollipop", "rectangle"],
    "surface_source": "ors", "ride_area": "", "tolerance": "3",
    "candidates": "12", "corrections": True, "classify": False,
}
ALL_SHAPES = ["loop", "lollipop", "rectangle", "out-and-back", "roundtrip"]


def _sweep_old_files():
    """Drop maps/GPX from earlier sessions so static/out doesn't grow forever."""
    cutoff = time.time() - MAX_AGE_S
    for f in OUT_DIR.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


@app.route("/")
def index():
    return render_template("index.html", d=FORM_DEFAULTS, all_shapes=ALL_SHAPES,
                           error=None)


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
    submitted = {**FORM_DEFAULTS, **{k: f.get(k, "") for k in FORM_DEFAULTS
                                     if k not in ("shapes", "corrections", "classify")}}
    submitted["shapes"] = shapes
    submitted["corrections"] = "corrections" in f
    submitted["classify"] = "classify" in f
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
            candidates=int(_clamp(f.get("candidates", 12), 1, 20, 12)),
            corrections=("corrections" in f),
            classify=("classify" in f),
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

    _sweep_old_files()
    token = uuid.uuid4().hex[:8]
    to_mi = 1.0 / 1.609344
    ride_type = f.get("ride_type", "road")
    unit = f.get("unit", "mi")
    # Descriptive download names (files are stored under an unguessable token to avoid
    # collisions; the browser saves each GPX as e.g. jun14-30mi-loop-Swind.gpx).
    dlnames = render.dedupe_names([
        render.route_basename(result.when, o.candidate.distance_km, unit,
                              o.candidate.shape, result.wind.direction_from_deg)
        for o in result.options])
    cards = []
    for i, opt in enumerate(result.options):
        c = opt.candidate
        base = OUT_DIR / f"{token}-{i}"
        meta = {
            "title": f"{c.distance_km * to_mi:.0f} mi {ride_type} {c.shape} "
                     f"- {opt.headline}",
            "location": result.location_label,
            "when": result.wind.valid_time.replace("T", " "),
            "ride_type": ride_type,
        }
        render.render_map(c, result.wind, meta, str(base.with_suffix(".png")))
        render.write_gpx(c.coords, str(base.with_suffix(".gpx")), name=meta["title"])
        verdict = ("into wind first" if c.wind_score > 0.2
                   else "wind against" if c.wind_score < -0.2 else "neutral")
        cards.append({
            "role": opt.role, "headline": opt.headline, "reasons": opt.reasons,
            "shape": c.shape, "dist_km": c.distance_km, "dist_mi": c.distance_km * to_mi,
            "ascent_m": c.ascent_m, "verdict": verdict,
            "gravel_pct": c.unpaved_frac * 100, "hwy_pct": c.busy_frac * 100,
            "path_pct": c.path_frac * 100, "lane_pct": c.bikelane_frac * 100,
            "good_gravel_pct": c.good_gravel_frac * 100,
            "unrideable_pct": c.unrideable_frac * 100,
            "cross": c.self_intersections,
            "png": f"out/{base.name}.png", "gpx": f"{base.name}.gpx",
            "dlname": f"{dlnames[i]}.gpx",
        })

    ranked_rows = [{
        "shape": c.shape, "dist_mi": c.distance_km * to_mi, "dist_km": c.distance_km,
        "ascent_m": c.ascent_m, "gravel_pct": c.unpaved_frac * 100,
        "unrideable_pct": c.unrideable_frac * 100,
        "hwy_pct": c.busy_frac * 100, "path_pct": c.path_frac * 100,
        "cross": c.self_intersections, "score": c.total_score,
        "verdict": ("into wind" if c.wind_score > 0.2
                    else "against" if c.wind_score < -0.2 else "neutral"),
    } for c in result.ranked]

    wind = result.wind
    wind_ctx = {
        "from": engine.compass_label(wind.direction_from_deg),
        "deg": wind.direction_from_deg, "mph": wind.speed_mph, "gust": wind.gust_mph,
        "when": wind.valid_time.replace("T", " "),
    }
    # Surface the terrain archetype as a debug line above the other notes.
    notes = result.notes
    if result.region is not None:
        notes = [result.region.note] + notes
    return render_template("results.html", label=result.location_label,
                           wind=wind_ctx, notes=notes, cards=cards,
                           ranked=ranked_rows)


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
