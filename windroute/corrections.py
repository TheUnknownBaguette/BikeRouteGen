"""Personal "I rode this" correction cache.

A local, hand-curated source of ground truth: roads you've actually ridden and
know the truth about — "ORS calls this paved but it's loose gravel", or "this
county road is quiet, not the busy arterial the data thinks". Corrections are
stored on disk and applied *on top of* whichever surface baseline you ran
(ORS / OSM / both), overriding only the segments you've marked and leaving the
rest of each route untouched. Your own knowledge always wins.

A correction is a polyline (from a GPX you rode, an A->B routed road, or a few
typed points) plus optional `surface` ('paved'|'unpaved') and `traffic`
('quiet'|'busy') labels. Matching reuses the same point-to-segment geometry as
the OSM source, so a route segment running along a marked road picks up the
correction.
"""
from __future__ import annotations

import datetime as dt
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .surface import _haversine_km, _pt_seg_dist_m

DEFAULT_RADIUS_M = 40.0
SURFACE_VALUES = ("paved", "unpaved")
TRAFFIC_VALUES = ("quiet", "busy")


def default_path() -> Path:
    """Where corrections live unless overridden (~/.windroute/corrections.json)."""
    return Path.home() / ".windroute" / "corrections.json"


def parse_gpx(path) -> list:
    """Return [(lat, lng), ...] from the track/route points of a GPX file."""
    text = Path(path).read_text(encoding="utf-8")
    root = ET.fromstring(text)
    pts = []
    # findall honors the "{*}" namespace wildcard (matches the GPX default
    # namespace our own write_gpx emits); Element.iter() does a literal tag-string
    # match and silently misses namespaced points, so it must not be used here.
    for tag in ("trkpt", "rtept", "wpt"):
        for el in root.findall(".//{*}" + tag):
            try:
                pts.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
            except (KeyError, ValueError):
                continue
        if pts:
            break
    return pts


def downsample(coords, min_gap_m=25.0, cap=400) -> list:
    """Thin a dense track: drop points closer than `min_gap_m`, keep <= `cap`."""
    if not coords:
        return []
    kept = [coords[0]]
    for p in coords[1:]:
        if _haversine_km(kept[-1], p) * 1000.0 >= min_gap_m:
            kept.append(p)
    if kept[-1] != coords[-1]:
        kept.append(coords[-1])
    if len(kept) > cap:                                # uniform stride if still huge
        step = len(kept) / cap
        kept = [kept[int(i * step)] for i in range(cap)]
    return kept


class CorrectionCache:
    """Load/save personal corrections and apply them to candidate routes."""

    def __init__(self, path=None, cell_deg=0.003):
        self.path = Path(path) if path else default_path()
        self.cell_deg = cell_deg
        self.records: list = []
        self._grid: dict | None = None

    # -- persistence --------------------------------------------------------- #
    @classmethod
    def load(cls, path=None):
        inst = cls(path)
        if inst.path.exists():
            data = json.loads(inst.path.read_text(encoding="utf-8"))
            inst.records = data.get("corrections", [])
        return inst

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"corrections": self.records}, indent=2), encoding="utf-8")
        return self.path

    # -- editing ------------------------------------------------------------- #
    def add(self, coords, surface=None, traffic=None, radius_m=DEFAULT_RADIUS_M,
            label=None, note=""):
        if surface and surface not in SURFACE_VALUES:
            raise ValueError(f"surface must be one of {SURFACE_VALUES}")
        if traffic and traffic not in TRAFFIC_VALUES:
            raise ValueError(f"traffic must be one of {TRAFFIC_VALUES}")
        if not surface and not traffic:
            raise ValueError("a correction needs at least a surface or traffic label")
        if not coords:
            raise ValueError("a correction needs at least one point")
        rec = {
            "label": label or self._auto_label(surface, traffic),
            "coords": [[round(la, 6), round(ln, 6)] for la, ln in coords],
            "surface": surface,
            "traffic": traffic,
            "radius_m": float(radius_m),
            "note": note,
            "added": dt.datetime.now().isoformat(timespec="seconds"),
        }
        self.records.append(rec)
        self._grid = None
        return rec

    def remove(self, key) -> bool:
        """Remove by label or 1-based index; return True if something was removed."""
        for i, rec in enumerate(self.records):
            if rec.get("label") == key:
                del self.records[i]
                self._grid = None
                return True
        try:
            idx = int(key) - 1
        except (TypeError, ValueError):
            return False
        if 0 <= idx < len(self.records):
            del self.records[idx]
            self._grid = None
            return True
        return False

    def _auto_label(self, surface, traffic):
        base = "-".join(x for x in (surface, traffic) if x) or "corr"
        n = 1
        existing = {r.get("label") for r in self.records}
        while f"{base}-{n}" in existing:
            n += 1
        return f"{base}-{n}"

    # -- spatial index ------------------------------------------------------- #
    def build(self):
        grid: dict = {}
        for i, rec in enumerate(self.records):
            pts = [tuple(c) for c in rec["coords"]]
            if len(pts) == 1:
                self._add_segment(grid, i, pts[0], pts[0])
            else:
                for a, b in zip(pts, pts[1:]):
                    self._add_segment(grid, i, a, b)
        self._grid = grid
        return self

    def _cell(self, lat, lng):
        return int(lat / self.cell_deg), int(lng / self.cell_deg)

    def _add_segment(self, grid, rec_i, a, b):
        steps = int(max(abs(a[0] - b[0]), abs(a[1] - b[1])) / self.cell_deg) + 1
        seen = set()
        for k in range(steps + 1):
            t = k / steps
            cell = self._cell(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            if cell not in seen:
                seen.add(cell)
                grid.setdefault(cell, []).append((rec_i, a, b))

    def _nearest_record(self, p):
        if not self._grid:
            return None
        ci, cj = self._cell(*p)
        best_d, best_rec = float("inf"), None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for rec_i, a, b in self._grid.get((ci + di, cj + dj), ()):
                    dist = _pt_seg_dist_m(p, a, b)
                    rec = self.records[rec_i]
                    if dist <= rec.get("radius_m", DEFAULT_RADIUS_M) and dist < best_d:
                        best_d, best_rec = dist, rec
        return best_rec

    # -- application --------------------------------------------------------- #
    def apply(self, candidate):
        """Override a candidate's surface/busy fractions for marked segments.

        Corrected segments are forced to their marked class; the rest of the
        route keeps its baseline rate, blended back by distance. Returns the
        corrected distance (km) for surface and traffic as a (surf, traffic)
        tuple — 0/0 means nothing of this route is in the cache.
        """
        if self._grid is None:
            self.build()
        coords = candidate.coords
        total = 0.0
        surf_d = surf_unpaved = 0.0
        traf_d = traf_busy = 0.0
        for a, b in zip(coords, coords[1:]):
            d = _haversine_km(a, b)
            if d <= 0:
                continue
            total += d
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            rec = self._nearest_record(mid)
            if rec is None:
                continue
            if rec.get("surface") in SURFACE_VALUES:
                surf_d += d
                if rec["surface"] == "unpaved":
                    surf_unpaved += d
            if rec.get("traffic") in TRAFFIC_VALUES:
                traf_d += d
                if rec["traffic"] == "busy":
                    traf_busy += d
        if total <= 0:
            return 0.0, 0.0

        if surf_d > 0:
            uncorrected = total - surf_d
            new_unpaved = (surf_unpaved + candidate.unpaved_frac * uncorrected) / total
            candidate.unpaved_frac = new_unpaved
            candidate.paved_frac = 1.0 - new_unpaved
            candidate.surface_by_source["cache"] = new_unpaved
        if traf_d > 0:
            uncorrected = total - traf_d
            candidate.busy_frac = (traf_busy + candidate.busy_frac * uncorrected) / total
        return surf_d, traf_d
