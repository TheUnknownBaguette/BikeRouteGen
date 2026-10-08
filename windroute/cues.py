"""Turn-by-turn cues for a finished route, and a TCX course file that carries them.

A GPX track has no cues, so Ride with GPS (and a head unit fed from it) shows none.
We work the cues out from the route itself: `Candidate.road_names` gives the road
for each stretch (from ORS's steps), and a cue goes wherever the road changes, with
left/right from the bend there. Doing it on the *final* line, rather than reusing
ORS's own instructions, keeps cues right on reversed stems (an out-and-back's return,
a lollipop's stem home), where ORS's lefts would be rights.

TCX is used for upload because it has a standard place for cues (CoursePoint), which
Ride with GPS reads into the route's cue sheet.
"""
from __future__ import annotations

import datetime as dt
from xml.sax.saxutils import escape

from .geometry import _bearing, _haversine_km

LOOK_M = 40.0          # bend measured from ~this far before the turn to ~this far after
MIN_GAP_M = 60.0       # cues closer than this to the previous one are dropped
BLIP_M = 40.0          # a name that lasts less than this (and returns) is noise
TURNAROUND_DEG = 150.0 # a bend this sharp gets a cue even without a road change
REVERSAL_M = 200.0     # ... and is a real turnaround only if the route comes back:
REVERSAL_GAP_M = 60.0  # points this far either side end up this close together
END_M = 100.0          # no turnaround cues this close to the start / finish (a start
                       # point mid-block makes a meaningless one)
EDGE_M = 30.0          # and no cues at all this close: the route is just getting going


def _point_back(coords, cum, i, metres):
    """Index of the point about `metres` before point i along the route."""
    j = i
    while j > 0 and (cum[i] - cum[j]) * 1000.0 < metres:
        j -= 1
    return j


def _point_ahead(coords, cum, i, metres):
    j = i
    while j < len(coords) - 1 and (cum[j] - cum[i]) * 1000.0 < metres:
        j += 1
    return j


def _turn(delta, reverses=True):
    """(direction word, TCX PointType) for a heading change in degrees (+ = right).
    A very sharp bend is a u-turn only when the route `reverses`, else a sharp turn."""
    a = abs(delta)
    side = "right" if delta > 0 else "left"
    if a < 25:
        return "continue", "Straight"
    if a >= TURNAROUND_DEG:
        if reverses:
            return "u-turn", "Generic"
        return "sharp " + side, side.capitalize()
    kind = "slight " if a < 50 else "sharp " if a > 130 else ""
    return kind + side, side.capitalize()


def make_cues(coords, names):
    """Cues for a route: [{i, lat, lng, km, type, text, road}], in riding order.

    `names[i]` is the road for the stretch leaving point i ("" = unnamed). A cue is
    placed where the name changes, plus at any turnaround; brief name blips and cues
    crowding the previous one are dropped. The start and finish are not cues.
    """
    n = len(coords)
    if n < 3 or not names or len(names) != n:
        return []
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + _haversine_km(a, b))

    cues, last_km = [], -1.0
    road = names[0]                           # the road we're on (blips don't count)
    for i in range(1, n - 1):
        new = names[i]
        b0 = _bearing(coords[_point_back(coords, cum, i, LOOK_M)], coords[i])
        b1 = _bearing(coords[i], coords[_point_ahead(coords, cum, i, LOOK_M)])
        delta = (b1 - b0 + 540.0) % 360.0 - 180.0
        if new == road:
            if abs(delta) < TURNAROUND_DEG:
                continue
        else:
            # a name that flips and comes back within a few metres is map noise
            j = i
            while j < n - 1 and names[j] == new:
                j += 1
            if (cum[j] - cum[i]) * 1000.0 < BLIP_M and names[j] == road:
                continue
            road = new
        if min(cum[i], cum[-1] - cum[i]) * 1000.0 < EDGE_M:
            continue
        if last_km >= 0 and (cum[i] - last_km) * 1000.0 < MIN_GAP_M:
            continue
        reverses = True
        if abs(delta) >= TURNAROUND_DEG:
            back = coords[_point_back(coords, cum, i, REVERSAL_M)]
            ahead = coords[_point_ahead(coords, cum, i, REVERSAL_M)]
            reverses = _haversine_km(back, ahead) * 1000.0 <= REVERSAL_GAP_M
            near_end = min(cum[i], cum[-1] - cum[i]) * 1000.0 < END_M
            if reverses and near_end:
                continue
        word, ptype = _turn(delta, reverses)
        if word == "u-turn":
            text = "Turn around" + (f" on {new}" if new else "")
        elif word == "continue":
            if not new:
                continue                      # straight on and nameless: nothing to say
            text = f"Continue onto {new}"
        else:
            text = f"Turn {word}" + (f" onto {new}" if new else "")
        lat, lng = coords[i]
        cues.append({"i": i, "lat": lat, "lng": lng, "km": cum[i], "type": ptype,
                     "text": text, "road": new})
        last_km = cum[i]
    return cues


def tcx_bytes(coords, eles, cues, name="windroute", start=None, speed_kmh=25.0):
    """A TCX course (track + CoursePoint cues) as UTF-8 bytes.

    Times are synthetic (start + distance at `speed_kmh`); TCX requires them but
    Ride with GPS only uses the line and the cues.
    """
    start = start or dt.datetime(2026, 1, 1, 8, 0)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    speed = max(1.0, speed_kmh) / 3600.0                    # km per second

    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + _haversine_km(a, b))

    def when(km):
        return (start + dt.timedelta(seconds=km / speed)).strftime("%Y-%m-%dT%H:%M:%SZ")

    has_ele = bool(eles) and len(eles) == len(coords)
    pts = []
    for k, (lat, lng) in enumerate(coords):
        alt = f"<AltitudeMeters>{eles[k]:.1f}</AltitudeMeters>" if has_ele else ""
        pts.append(f"<Trackpoint><Time>{when(cum[k])}</Time><Position>"
                   f"<LatitudeDegrees>{lat:.6f}</LatitudeDegrees>"
                   f"<LongitudeDegrees>{lng:.6f}</LongitudeDegrees></Position>{alt}"
                   f"<DistanceMeters>{cum[k] * 1000:.1f}</DistanceMeters></Trackpoint>")
    cps = []
    for c in cues:
        short = escape((c["road"] or c["type"])[:10])        # TCX caps Name at 10 chars
        cps.append(f"<CoursePoint><Name>{short}</Name><Time>{when(c['km'])}</Time>"
                   f"<Position><LatitudeDegrees>{c['lat']:.6f}</LatitudeDegrees>"
                   f"<LongitudeDegrees>{c['lng']:.6f}</LongitudeDegrees></Position>"
                   f"<PointType>{c['type']}</PointType>"
                   f"<Notes>{escape(c['text'])}</Notes></CoursePoint>")
    total = cum[-1] if coords else 0.0
    first = coords[0] if coords else (0.0, 0.0)
    last = coords[-1] if coords else (0.0, 0.0)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<TrainingCenterDatabase '
        'xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
        f'<Courses><Course><Name>{escape(name[:15])}</Name>'
        f'<Lap><TotalTimeSeconds>{total / speed:.0f}</TotalTimeSeconds>'
        f'<DistanceMeters>{total * 1000:.1f}</DistanceMeters>'
        f'<BeginPosition><LatitudeDegrees>{first[0]:.6f}</LatitudeDegrees>'
        f'<LongitudeDegrees>{first[1]:.6f}</LongitudeDegrees></BeginPosition>'
        f'<EndPosition><LatitudeDegrees>{last[0]:.6f}</LatitudeDegrees>'
        f'<LongitudeDegrees>{last[1]:.6f}</LongitudeDegrees></EndPosition>'
        '<Intensity>Active</Intensity></Lap>'
        f'<Track>{"".join(pts)}</Track>{"".join(cps)}'
        '</Course></Courses></TrainingCenterDatabase>\n'
    )
    return xml.encode("utf-8")
