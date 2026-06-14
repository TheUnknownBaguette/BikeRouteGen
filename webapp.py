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

import os
import time
import uuid
from pathlib import Path

from flask import Flask, render_template, request, send_from_directory

from windroute import engine, render, planner

app = Flask(__name__)

OUT_DIR = Path(__file__).parent / "static" / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_AGE_S = 3600                      # delete generated maps/gpx older than this

# Defaults shown in the form (match the CLI's).
FORM_DEFAULTS = {
    "location": "Mokena, IL", "distance": "30", "unit": "mi", "start": "now",
    "ride_type": "road", "shapes": ["loop", "lollipop", "rectangle"],
    "surface_source": "ors", "ride_area": "", "tolerance": "3",
    "candidates": "12", "corrections": True,
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


@app.route("/plan", methods=["POST"])
def plan():
    f = request.form
    shapes = f.getlist("shapes") or FORM_DEFAULTS["shapes"]
    try:
        result = planner.plan_routes(
            location=f.get("location", "").strip(),
            distance=float(f.get("distance", 0) or 0),
            unit=f.get("unit", "mi"),
            start=f.get("start", "now").strip() or "now",
            ride_type=f.get("ride_type", "road"),
            shapes=shapes,
            surface_source=f.get("surface_source", "ors"),
            ride_area=(f.get("ride_area", "").strip() or None),
            tolerance=float(f.get("tolerance", 3) or 3),
            candidates=int(f.get("candidates", 12) or 12),
            corrections=("corrections" in f),
            api_key=os.environ.get("ORS_API_KEY"),
            n_alternatives=2,
        )
    except Exception as exc:                       # bad location, no routes, no key…
        # Re-show the form with the submitted values and a friendly message.
        submitted = {**FORM_DEFAULTS, **{k: f.get(k, "") for k in FORM_DEFAULTS
                                         if k not in ("shapes", "corrections")}}
        submitted["shapes"] = shapes
        submitted["corrections"] = "corrections" in f
        return render_template("index.html", d=submitted, all_shapes=ALL_SHAPES,
                               error=str(exc)), 400

    _sweep_old_files()
    token = uuid.uuid4().hex[:8]
    to_mi = 1.0 / 1.609344
    ride_type = f.get("ride_type", "road")
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
            "cross": c.self_intersections,
            "png": f"out/{base.name}.png", "gpx": f"{base.name}.gpx",
        })

    ranked_rows = [{
        "shape": c.shape, "dist_mi": c.distance_km * to_mi, "dist_km": c.distance_km,
        "ascent_m": c.ascent_m, "gravel_pct": c.unpaved_frac * 100,
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
    return render_template("results.html", label=result.location_label,
                           wind=wind_ctx, notes=result.notes, cards=cards,
                           ranked=ranked_rows)


@app.route("/download/<path:name>")
def download(name):
    """Serve a generated GPX as a file download."""
    return send_from_directory(OUT_DIR, name, as_attachment=True)


def _open_browser():
    import webbrowser
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    import threading
    # Open the browser a beat after the server starts (debug/reloader OFF so this
    # fires exactly once).
    threading.Timer(1.0, _open_browser).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
