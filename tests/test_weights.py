"""Offline tests for archetype-keyed tuning (work-plan Task 2).

The central guarantee is the **regression gate**: the grid-farmland row must equal
today's constants, and the default path (no archetype / 'unknown') must reproduce
current scoring exactly. No network.

Run from the project root:  python tests/test_weights.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from windroute import engine, zones


# --- route weights --------------------------------------------------------- #
def test_grid_farmland_weights_equal_constants():
    """grid-farmland ROAD RouteWeights == the module constants (byte-identical)."""
    w = engine.weights_for("grid-farmland")          # default ride_type="road"
    assert w.wind_scale == 1.0
    assert w.w_wind == 1.0                            # base road wind weight
    assert w.w_dist == 0.5
    assert w.road_gravel_lin == engine.W_ROAD_GRAVEL_LIN
    assert w.road_gravel_quad == engine.W_ROAD_GRAVEL_QUAD
    assert w.gravel_seek == 0.0                       # road never SEEKS gravel
    assert w.w_good_gravel == 0.0
    assert w.w_busy == engine.W_BUSY
    assert w.busy_free_frac == engine.BUSY_FREE_FRAC
    assert w.w_path == engine.W_PATH
    assert w.path_run_free_frac == engine.PATH_RUN_FREE_FRAC
    assert w.w_bikelane == engine.W_BIKELANE
    assert w.w_tidy == engine.W_TIDY
    assert w.tidy_free_per_km == engine.TIDY_FREE_PER_KM


# --- road/gravel asymmetry (Task 3a/3b) ------------------------------------ #
def test_road_profile_penalizes_gravel():
    w = engine.ROAD_WEIGHTS
    assert w.road_gravel_lin > 0 and w.road_gravel_quad > 0
    assert w.gravel_seek == 0.0                       # no seek reward on road


def test_gravel_profile_seeks_gravel():
    w = engine.GRAVEL_WEIGHTS
    assert w.gravel_seek > 0                          # rewards unpaved
    assert w.road_gravel_lin == 0 and w.road_gravel_quad == 0   # no penalty
    assert w.w_wind < engine.ROAD_WEIGHTS.w_wind      # wind matters less on gravel
    assert w.w_good_gravel > 0                         # bonus for good gravel


def test_weights_for_gravel_selects_gravel_profile():
    assert engine.weights_for("grid-farmland", "gravel").gravel_seek > 0
    assert engine.weights_for(None, "gravel").road_gravel_lin == 0
    # archetype tuning still composes onto the gravel base
    assert engine.weights_for("mountain", "gravel").wind_scale < 1.0


def test_unrideable_hard_avoid_both_ride_types():
    assert engine.weights_for("grid-farmland", "road").w_unrideable > 0
    assert engine.weights_for("grid-farmland", "gravel").w_unrideable > 0


def test_seek_curve_band():
    f = engine._gravel_seek_reward
    assert f(0.0, 0.5, 0.75) == 0.0
    assert f(0.3, 0.5, 0.75) == 0.6                   # ramps (0.3/0.5)
    assert f(0.5, 0.5, 0.75) == 1.0                   # full at band start
    assert f(0.7, 0.5, 0.75) == 1.0                   # holds across band
    assert 0.7 <= f(0.95, 0.5, 0.75) < 1.0            # gentle taper above, floored


def test_weights_for_defaults_to_grid_farmland():
    base = engine.weights_for("grid-farmland")
    assert engine.weights_for(None) is base
    assert engine.weights_for("unknown") is base
    assert engine.weights_for("nonsense-archetype") is base


def test_mountain_weights_differ():
    w = engine.weights_for("mountain")
    assert w.wind_scale < 1.0           # wind matters less where terrain dominates


# --- shapes ---------------------------------------------------------------- #
def test_shapes_grid_farmland_unchanged():
    default = ["loop", "lollipop", "rectangle"]
    assert engine.shapes_for(None, list(default)) == default
    assert engine.shapes_for("grid-farmland", list(default)) == default
    assert engine.shapes_for("unknown", list(default)) == default


def test_shapes_mountain_drops_rectangle():
    out = engine.shapes_for("mountain", ["loop", "lollipop", "rectangle"])
    assert "rectangle" not in out
    assert "loop" in out and "lollipop" in out


def test_shapes_always_keeps_optin_forms():
    # opt-in shapes the caller added deliberately survive archetype filtering
    out = engine.shapes_for("mountain", ["loop", "out-and-back", "staging", "roundtrip"])
    for s in ("out-and-back", "staging", "roundtrip"):
        assert s in out


def test_shapes_never_empty():
    assert engine.shapes_for("mountain", []) == ["loop"]


# --- loop geometry --------------------------------------------------------- #
def test_loop_geom_grid_farmland_is_default():
    sides, detour = engine.loop_geom_for(None)
    assert sides == engine._LOOP_SIDES
    assert detour == 1.25


def test_loop_geom_mountain_curvier():
    _sides, detour = engine.loop_geom_for("mountain")
    assert detour > 1.25


# --- zone weights ---------------------------------------------------------- #
def test_zone_weights_grid_farmland_baseline():
    z = zones.zone_weights_for("grid-farmland")
    assert (z.w_grid, z.w_farm, z.w_art) == (zones.W_GRID, zones.W_FARM, zones.W_ART)
    assert z.w_forest == 0.0 and z.w_water == 0.0      # no extra signals -> identical
    assert z.forest_cw == 0.0 and z.water_cw == 0.0


def test_zone_weights_defaults_and_suburban():
    base = zones.zone_weights_for("grid-farmland")
    assert zones.zone_weights_for(None) is base
    assert zones.zone_weights_for("unknown") is base
    # suburban escapes TO open country -> same farmland-seeking signal
    assert zones.zone_weights_for("suburban-sprawl") is base


def test_zone_weights_mountain_uses_forest():
    z = zones.zone_weights_for("mountain")
    assert z.w_forest > 0 and z.forest_cw > 0


# --- evaluate: regression-identical default path --------------------------- #
def _candidates():
    # a couple of small, distinct routes with a real bearing so wind_norm varies
    a = engine.Candidate(coords=[(41.0, -88.0), (41.05, -88.0), (41.05, -88.05)],
                         distance_km=30.0, ascent_m=50.0, paved_frac=1.0,
                         unpaved_frac=0.0, shape="loop", busy_frac=0.0,
                         path_run_frac=0.0, bikelane_frac=0.1)
    b = engine.Candidate(coords=[(41.0, -88.0), (41.0, -88.05), (41.05, -88.05)],
                         distance_km=33.0, ascent_m=80.0, paved_frac=0.8,
                         unpaved_frac=0.2, shape="loop", busy_frac=0.1,
                         path_run_frac=0.4, bikelane_frac=0.0)
    return [a, b]


def test_evaluate_none_equals_grid_farmland():
    wind = engine.Wind(direction_from_deg=200.0, speed_mph=10.0, gust_mph=15.0,
                       valid_time="")
    base = engine.weights_for("grid-farmland")
    r_none = engine.evaluate(_candidates(), wind, "road", 30.0, 3.0, weights=None)
    r_grid = engine.evaluate(_candidates(), wind, "road", 30.0, 3.0, weights=base)
    s_none = sorted(c.total_score for c in r_none)
    s_grid = sorted(c.total_score for c in r_grid)
    assert s_none == s_grid, (s_none, s_grid)


def test_evaluate_mountain_changes_scores():
    wind = engine.Wind(direction_from_deg=200.0, speed_mph=10.0, gust_mph=15.0,
                       valid_time="")
    r_none = engine.evaluate(_candidates(), wind, "road", 30.0, 3.0, weights=None)
    r_mtn = engine.evaluate(_candidates(), wind, "road", 30.0, 3.0,
                            weights=engine.weights_for("mountain"))
    s_none = sorted(c.total_score for c in r_none)
    s_mtn = sorted(c.total_score for c in r_mtn)
    assert s_none != s_mtn      # archetype tuning actually moves the numbers


_COORDS = [(41.0, -88.0), (41.05, -88.0), (41.05, -88.05)]
_WIND = engine.Wind(direction_from_deg=200.0, speed_mph=10.0, gust_mph=15.0,
                    valid_time="")


def _cand(unpaved=0.0, good=0.0, unrideable=0.0):
    return engine.Candidate(coords=list(_COORDS), distance_km=30.0, ascent_m=50.0,
                            paved_frac=1.0 - unpaved, unpaved_frac=unpaved,
                            shape="loop", good_gravel_frac=good,
                            unrideable_frac=unrideable)


def _score(c, ride_type):
    engine.evaluate([c], _WIND, ride_type, 30.0, 3.0,
                    weights=engine.weights_for(None, ride_type))
    return c.total_score


def test_gravel_ride_seeks_gravel_road_avoids():
    hi, lo = _cand(unpaved=0.6), _cand(unpaved=0.05)
    # gravel ride: the gravelly route wins
    assert _score(hi, "gravel") > _score(_cand(unpaved=0.05), "gravel")
    # road ride: the paved route wins (penalty, not reward)
    assert _score(_cand(unpaved=0.05), "road") > _score(_cand(unpaved=0.6), "road")


def test_unrideable_demotes_both_ride_types():
    for rt in ("road", "gravel"):
        clean = _score(_cand(unpaved=0.4), rt)
        muddy = _score(_cand(unpaved=0.4, unrideable=0.3), rt)
        assert muddy < clean, rt           # mud/ground hard-avoided for both


def test_good_gravel_bonus_gravel_only():
    plain = _score(_cand(unpaved=0.4), "gravel")
    good = _score(_cand(unpaved=0.4, good=0.4), "gravel")
    assert good > plain                     # confirmed good gravel is a bonus
    # road ride ignores the good-gravel bonus
    assert _score(_cand(unpaved=0.4), "road") == _score(_cand(unpaved=0.4, good=0.4), "road")


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {t.__name__}: {exc}")
        except Exception as exc:                              # pragma: no cover
            failures += 1
            print(f"  ERROR {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
