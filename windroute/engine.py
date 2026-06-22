"""Compatibility facade — the historical flat `engine` namespace.

The logic now lives in focused modules (CODE_HEALTH Task C1):
  models.py    Wind / Candidate / RouteOption
  geometry.py  bearings, distances, self-intersection, compass
  geocode.py   geocoding + autocomplete
  wind.py      wind forecast + historical
  routing.py   ORS directions, geometric shapes, generation, refinement
  scoring.py   weights, evaluate, route-option selection, explanations

Everything that used to be `engine.NAME` still resolves here, so call sites and
imports are unchanged. IMPORTANT: to MONKEYPATCH an internal, patch it on its HOME
module (e.g. routing._ors_directions, wind._wind_from_open_meteo) — rebinding the
re-exported name here does not affect the home module's own lookups.
"""
from __future__ import annotations

from . import models, geometry, geocode, wind, routing, scoring

# Re-export every public + private name from each module so the flat namespace is
# reproduced exactly (names were globally unique in the original single file).
for _mod in (models, geometry, geocode, wind, routing, scoring):
    for _name, _val in vars(_mod).items():
        if not _name.startswith("__"):
            globals()[_name] = _val
del _mod, _name, _val
