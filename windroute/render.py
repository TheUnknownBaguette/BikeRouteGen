"""Output layer: a labelled route image and a GPX file for Ride with GPS."""
from __future__ import annotations

import math
import re

from PIL import Image, ImageDraw, ImageFont

from .engine import compass_label

try:
    from staticmap import StaticMap, Line, CircleMarker
    _HAVE_STATICMAP = True
except ImportError:
    _HAVE_STATICMAP = False


# --------------------------------------------------------------------------- #
# GPX (no dependencies — it's just XML)
# --------------------------------------------------------------------------- #
def write_gpx(coords, path, name="WindRoute"):
    """coords: [(lat, lng), ...]. Writes a single-track GPX importable by RWGPS."""
    pts = "\n".join(f'   <trkpt lat="{lat:.6f}" lon="{lng:.6f}" />' for lat, lng in coords)
    gpx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="windroute" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        f' <trk><name>{name}</name><trkseg>\n{pts}\n </trkseg></trk>\n'
        '</gpx>\n'
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(gpx)
    return path


def route_basename(when, distance_km, unit, shape, wind_from_deg):
    """Descriptive, filesystem-safe base name for a route's files.

    e.g. ``jun14-30mi-loop-Swind`` — ride date, the route's actual rounded
    distance, its shape, and the compass the wind blows FROM. No extension.
    Used as the default output name so files aren't all called "route".
    """
    date = when.strftime("%b%d").lower() if hasattr(when, "strftime") else "ride"
    miles = unit.lower().startswith("mi")
    dist = round(distance_km / 1.609344) if miles else round(distance_km)
    shp = re.sub(r"[^a-z0-9-]+", "", str(shape).lower()) or "route"
    wind = compass_label(wind_from_deg)
    return f"{date}-{dist}{'mi' if miles else 'km'}-{shp}-{wind}wind"


def dedupe_names(names):
    """Make a list of base names unique in order, appending -2, -3, … on clashes."""
    seen, out = {}, []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}-{seen[n]}")
        else:
            seen[n] = 1
            out.append(n)
    return out


# --------------------------------------------------------------------------- #
# Map image
# --------------------------------------------------------------------------- #
def render_map(candidate, wind, meta, path, size=(820, 820), tile_url=None):
    """Render the route on an OSM basemap with an info banner and a wind compass.

    `meta` keys used: title, location, when, ride_type.
    """
    if not _HAVE_STATICMAP:
        raise RuntimeError("staticmap is not installed (pip install staticmap)")

    w, h = size
    smap = StaticMap(
        w, h,
        url_template=tile_url or "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        headers={"User-Agent": "windroute/0.1 (personal cycling tool)"},
    )
    lnglat = [(lng, lat) for lat, lng in candidate.coords]   # staticmap wants (lon, lat)
    smap.add_line(Line(lnglat, "#1f6feb", 5))
    smap.add_marker(CircleMarker(lnglat[0], "#d83933", 13))
    map_img = smap.render()

    banner_h = 118
    canvas = Image.new("RGB", (w, h + banner_h), "white")
    canvas.paste(map_img, (0, banner_h))
    draw = ImageDraw.Draw(canvas)
    font, small = _fonts()

    draw.text((16, 12), meta.get("title", "Wind-smart route"), fill="#111111", font=font)
    draw.text(
        (16, 44),
        f"{meta.get('location', '')}   |   {meta.get('when', '')}   |   "
        f"{meta.get('ride_type', '')} ride",
        fill="#333333", font=small,
    )
    surf = (f"{candidate.unpaved_frac * 100:.0f}% unpaved"
            if meta.get("ride_type") == "gravel"
            else f"{candidate.paved_frac * 100:.0f}% paved")
    draw.text(
        (16, 72),
        f"Wind {compass_label(wind.direction_from_deg)} "
        f"({wind.direction_from_deg:.0f}\u00b0) {wind.speed_mph:.0f} mph "
        f"(gust {wind.gust_mph:.0f})   |   {candidate.distance_km:.1f} km   |   "
        f"+{candidate.ascent_m:.0f} m   |   {surf}",
        fill="#333333", font=small,
    )

    _draw_wind_compass(draw, w - 62, 58, 30, wind.direction_from_deg)

    # Attribution, bottom-right of the map (OSM tile policy requires visible credit;
    # weather sources credited too). Drawn over a small white plate for legibility.
    cred = "© OpenStreetMap contributors  ·  wind: Open-Meteo.com / weather.gov"
    tb = draw.textbbox((0, 0), cred, font=small)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    x0, y0 = w - tw - 12, h + banner_h - th - 10
    draw.rectangle([x0 - 5, y0 - 4, w - 2, h + banner_h - 2], fill="#ffffff")
    draw.text((x0, y0), cred, fill="#444444", font=small)

    canvas.save(path)
    return path


def _draw_wind_compass(draw, cx, cy, r, from_deg):
    """Small dial; arrow points the way the wind is blowing TO."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline="#888888", width=1)
    draw.text((cx - 3, cy - r - 13), "N", fill="#888888")
    blow_to = math.radians((from_deg + 180) % 360)
    dx, dy = math.sin(blow_to), -math.cos(blow_to)        # screen coords: y down
    tip = (cx + dx * (r - 4), cy + dy * (r - 4))
    tail = (cx - dx * (r - 8), cy - dy * (r - 8))
    draw.line([tail, tip], fill="#1f6feb", width=3)
    # arrowhead
    ang = math.atan2(tip[1] - tail[1], tip[0] - tail[0])
    for off in (math.radians(150), math.radians(-150)):
        draw.line([tip, (tip[0] + 8 * math.cos(ang + off),
                         tip[1] + 8 * math.sin(ang + off))], fill="#1f6feb", width=3)


def _fonts():
    for name in ("DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, 22), ImageFont.truetype(name, 15)
        except (OSError, IOError):
            continue
    f = ImageFont.load_default()
    return f, f
