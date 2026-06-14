"""windroute — wind-smart cycling route generator.

The package is split so the interface (CLI, Discord bot, web app) is always a
thin layer over `engine` + `render`. To add a new front-end, import these and
call them; never reimplement the logic.
"""
from .engine import (
    Wind,
    Candidate,
    geocode,
    get_wind,
    get_wind_historical,
    generate_candidates,
    evaluate,
    compass_label,
    wind_score,
)
from .render import render_map, write_gpx
from .surface import OverpassSurface, classify_tags
from .corrections import CorrectionCache, parse_gpx
from .rwgps import (
    Credentials,
    get_auth_token,
    list_trips,
    get_trip,
    parse_track_points,
    trip_summary,
)
from .learn import trip_features, analyze_trips, suggest_weight_changes

__all__ = [
    "Wind",
    "Candidate",
    "geocode",
    "get_wind",
    "get_wind_historical",
    "generate_candidates",
    "evaluate",
    "compass_label",
    "wind_score",
    "render_map",
    "write_gpx",
    "OverpassSurface",
    "classify_tags",
    "CorrectionCache",
    "parse_gpx",
    "Credentials",
    "get_auth_token",
    "list_trips",
    "get_trip",
    "parse_track_points",
    "trip_summary",
    "trip_features",
    "analyze_trips",
    "suggest_weight_changes",
]
__version__ = "0.1.0"
