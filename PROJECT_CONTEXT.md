# BikeRouteGen — context handoff

**Point a new chat here:** "Read `PROJECT_CONTEXT.md` for context before we start."
This file is the single source of truth for what the project is, what's built, and
the non-obvious decisions behind it. Keep it updated when you finish a feature.

---

## What it is

`BikeRouteGen` is a **wind-smart cycling route generator** (Python package
`windroute/`). Give it a start point, distance, ride time, and ride type; it pulls
the wind forecast for that hour, generates candidate routes, scores them so you
ride **into the wind while fresh and get the tailwind home**, and writes a
**recommended route plus two alternatives** (each leading on a different benefit) as
labelled maps + GPX files (`route.png/.gpx`, `route-alt1.*`, `route-alt2.*`) you
import into Ride with GPS.

- **Location:** the project folder (`path\to\BikeRouteGen`)
- **APIs:** OpenRouteService (routing, needs free key), Open-Meteo (geocode + wind
  forecast + **archive** for historical wind, no key), OSM Nominatim (address geocode),
  Overpass (OSM surface / bike-lane / busy-path / farmland reads), **Ride with GPS v1**
  (recorded-trip import, needs an api_key + auth_token). `ORS_API_KEY` is saved at the
  Windows **User** level; RWGPS creds live in `~/.windroute/rwgps.json`.
- **Owner's riding preferences (these drive the scoring):** quiet county/township
  roads; avoid busy US-highways; dislikes pure out-and-backs (retracing); uses multiuse
  paths as **connectors to reach good riding, never as the ride itself** (would never
  out-and-back a trail); loves on-road bike lanes; rides *out toward quiet rural roads
  and open farmland* for quiet riding. (All confirmed by trip-history analysis — see findings.)

## How to run (PowerShell)

```powershell
cd path\to\BikeRouteGen
.\.venv\Scripts\Activate.ps1
python -m windroute.cli plan -l "Chicago, IL" -d 30 -s "2026-06-14 08:00" -r road
```

`ORS_API_KEY` is set at User level, so new terminals inherit it. If a terminal was
open *before* it was set, load it manually:
```powershell
$env:ORS_API_KEY = [Environment]::GetEnvironmentVariable("ORS_API_KEY","User")
```

**Learn from your Ride with GPS history** (no password needed — paste an api_key +
auth_token from the API client's edit page at ridewithgps.com/settings/developers):
```powershell
python -m windroute.cli rwgps-login --api-key <KEY> --auth-token <TOKEN>
python -m windroute.cli import --limit 200      # newest 200 trips -> ~/.windroute/trips
python -m windroute.cli learn                   # full report (slow: OSM + wind per trip)
python -m windroute.cli learn --no-surface      # fast: geometry/distance/direction only
```
Full setup + every option is in `README.md`.

**Local web app (no terminal):** `python webapp.py` (or double-click `run.bat`) starts
a Flask server on `127.0.0.1:5000` and opens a browser; a form runs `plan_routes` and
shows the recommendation + 2 alternatives with inline maps + GPX downloads. Maps/GPX
are written to `static/out/` (gitignored, swept after 1 h). `webapp.py` reads `HOST`/
`PORT` from the env (default local), so the same file serves locally and on a server.

**Hosting / future self-host (so friends need no key):** `Procfile` runs `waitress`
(cross-platform prod server, in requirements) via `waitress-serve --listen=*:$PORT
webapp:app`. CURRENT: hosted on a free service (e.g. Render) — connect the repo, that
start command, and set `ORS_API_KEY` as a server SECRET (never in the public repo).
FUTURE self-host (owner wants their own mini server eventually, doesn't have one yet,
2026-06): the SAME `waitress-serve` command runs on any box (Windows/Linux/Pi) — set
`ORS_API_KEY` in that machine's env, open the port, done. No code changes; the env-
driven HOST/PORT + waitress are the provisions for it. Watch the ORS free-tier limit
(~2000 calls/day, ~12-15 per plan) — a paid key or self-hosted ORS if it grows.

## Architecture / file map

```
windroute/
  engine.py       core: geocode, wind (+ get_wind_historical), geometric route gen + shapes, scoring, route-option selection (NO I/O — pure fns)
  planner.py      SHARED pipeline: plan_routes() -> PlanResult (geocode->wind->staging->generate->surface->corrections->evaluate->options). No printing/files. CLI + web both call it.
  zones.py        auto-detect nearest quiet riding zone (for --ride-area staging)
  surface.py      OSM/Overpass surface + bike-lane + busy/path waytype source (OverpassSurface)
  corrections.py  personal correction cache (~/.windroute/corrections.json) + road-notes parser
  rwgps.py        Ride with GPS v1 API client (auth, list/fetch trips, trip cache, creds)
  learn.py        analyse imported trips -> rider profile + suggested weight changes (pure)
  render.py       map image + GPX output
  cli.py          CLI front-end: plan / mark / roads-import / corrections / forget / rwgps-login / import / learn
webapp.py         local web front-end (Flask); templates/ has the HTML; run.bat launches it
discord_bot.py    optional Discord front-end (thin over planner.plan_routes; needs discord.py; not wired in)
README.md         user-facing setup + usage
requirements.txt  deps
```
**Design rule:** every front-end (CLI, web, Discord) is a thin layer over
`planner` (orchestration) + `engine`/`render` (logic + output). Never reimplement the
pipeline in a front-end — `plan_routes` is the one place it lives.

---

## Features built (all DONE + verified)

- **Wind scoring:** `wind_score` rewards headwind on first half / tailwind home.
- **Distance tolerance:** `-t/--tolerance` free buffer band; only excess is penalized.
- **Elevation fix:** `engine._smoothed_ascent` (interpolate SRTM nodata, median +
  moving-average filter, hysteresis) — raw ORS ascent was wildly inflated on flat IL.
- **Route shapes** (`--shapes`, default `loop,lollipop,rectangle`): loop, lollipop,
  **rectangle** (long leg into wind / short crosswind jog / long parallel leg home —
  owner loves these in grid country), out-and-back (opt-in), `roundtrip` (opt-in; the
  old ORS round_trip algorithm). Directional shapes aim into the wind first. **All
  automatic shapes are now built from explicit geometric waypoints** (see "Clean
  geometric routing" below) — no ORS `round_trip` unless you ask for `roundtrip`.
- **Three route options** (`engine.select_route_options` -> `RouteOption`): the plan
  returns **1 recommendation + 2 alternatives**, each leading on a *different* benefit
  (stronger wind line / quieter roads / more bike lane / closer distance) and a
  genuinely different RIDE — different shape, a couple miles longer/shorter, or mostly
  different roads (measured by `_route_overlap`, a ~100 m grid-cell overlap). NOT just
  the 2nd/3rd-best scores (near-clones of the winner), and NOT a different *direction*
  (same good country is fine). An axis is only used if a candidate beats the pick on it
  by a margin; leftover slots fall back to "most different roads/shape/length". CLI
  writes the pick to `<out>.*` and alternatives to `<out>-alt1/-alt2`. Pure, no refetch.
- **Clean geometric routing** (replaced ORS `round_trip`, which scattered via-points
  that tangled and spurred onto perpendicular roads): `loop` is now a polygon routed
  point-to-point through corners around the start (`_polygon_loop_waypoints` /
  `_make_polygon_loop`); `lollipop` and `staging` use the same polygon for their candy/
  destination loop, anchored at the stem's real routed endpoint. Clean by construction.
  Backstops still applied: `_strip_backtracks` removes ORS's little A->B->A stub spurs
  per leg (tol 5 m, before any out-and-back concatenation so a deliberate retrace is
  preserved), and a **tidiness penalty** (`W_TIDY=0.4`, free band 0.10 self-crossings/km
  via `_self_intersections`) demotes any tangled candidate so the cleanest seed wins
  (`Cross` column in the table). `--candidates` default 12 (more seeds to pick from).
- **Surface sources** (`--surface-source ors|osm|both`): ORS buckets (baseline, free),
  OSM/Overpass tags (finer for gravel), or `both` (cross-check, flags disagreements
  >10%). Road rides show "Gravel %" (= confirmed unpaved); gravel rides show "Unpaved %".
- **Road gravel penalty:** linear + convex on confirmed `unpaved_frac` — a half-gravel
  "road" route can't be saved by a great wind line. Only penalizes KNOWN gravel.
- **Busy-highway avoidance:** penalizes time on ORS waytype 1 ("State Road" = US-highways)
  beyond a 5% free band. `busy_frac`, "Hwy %" column.
- **Bike paths** (separated multiuse trails, ORS waytype 4/6/7): penalty is on the
  **longest *contiguous* path run** (`path_run_frac`) beyond `PATH_RUN_FREE_FRAC` (0.25),
  NOT total path mileage — the owner uses trails as connectors to reach good riding, not
  as the ride. So short trail segments stitching roads ride free; one long stretch (e.g.
  an out-and-back on a trail) gets hit. `W_PATH=0.6`. Run measured by `_waytype_run_km`
  (ORS positional `extras.waytype.values`) / `OverpassSurface.path_run_frac` (OSM).
- **Bike lanes** (on-road `cycleway=*` tags — invisible to ORS, OSM-only): a bonus
  (`W_BIKELANE=0.6`, bumped from 0.4 — trip history shows the owner actively seeks lanes).
  `bikelane_frac`, "Lane %" column (only under `--surface-source osm|both`).
- **Learn from Ride with GPS history** (`rwgps-login` / `import` / `learn`): import recorded
  trips, measure the scorer's features on them + geometry it doesn't model (distance, outbound
  direction, shape via RWGPS `track_type`, real surface/path-run/lane fractions, and actual
  into-the-wind behaviour via historical-wind backfill), and print a profile + suggested
  weight changes. Walks/hikes/indoor auto-excluded (RWGPS `activity_type`/`stationary`);
  `--all-activities` to include. Read-only: never edits weights. See findings below.
- **Corrections cache** (`mark` / `corrections` / `forget`): record personal ground
  truth (a road is really gravel / really busy) via GPX, points, or `--between`/`--to`;
  applied on top of surface data on every plan.
- **Road-notes bulk import** (`roads-import <file>`): edit a plain-text file of roads
  (`<tags>: A -> B`, tags = gravel|paved|busy|quiet, combinable) and import them all into
  the correction cache at once — the ergonomic front door to `mark`. `corrections.parse_road_notes`
  is the pure parser (returns entries + per-line errors); the CLI geocodes each endpoint pair,
  routes A->B (same path as `mark --between/--to`), and adds it. Missing file -> writes a
  commented template (`ROAD_NOTES_TEMPLATE`). Imported records carry `source="roads-import"` +
  `origin=<file>` so re-running RE-SYNCS that file (replaces its prior entries; `--append` keeps
  them). Key gotcha: a line gives two ENDPOINTS, not a road name — the trace follows whatever
  ORS routes between them, so pick close endpoints on the road (addresses / `lat,lng` pins;
  bare road names geocode unreliably). Penalty scales with distance ridden ALONG a marked
  road, so merely CROSSING a marked gravel road costs ~nothing (~84 m flagged at the 40 m
  match radius = ~0.18% of a 48 km ride).
- **Exact start point:** `geocode()` dispatches over (1) `lat,lng` decimal, (2) DMS
  from Google Maps (`41°31'36.3"N 87°52'18.0"W`), (3) street address (Nominatim),
  (4) town / "City, ST" (Open-Meteo).
- **Profile choice:** road rides use `cycling-regular` (NOT `cycling-road`, which
  hard-avoids paths/lanes and kept routes off the paved Hickory Creek trail). Balance
  lives in scoring, not the profile.
- **Auto-detect quiet ride zone + staging** (`--ride-area auto`, or a place/`lat,lng`):
  `zones.find_ride_zone` does ONE Overpass call, buckets quiet grid roads + arterials +
  farmland into 12 directional sectors, scores them (farmland dominant), and returns the
  best sector's centroid — or None. The `staging` shape transits there, loops on the
  wind, and rides home; only the destination loop is wind-scored. `-d` = TOTAL miles.

---

## Key decisions & gotchas (the non-obvious stuff — read before changing these)

- **ORS `profile_params.weightings.quiet` is a NO-OP on the public API** (accepted,
  silently ignored — verified byte-identical routes). All "quiet" steering is done via
  the scoring penalty, never at the routing level.
- **`cycling-road` hard-avoids paths AND on-road bike lanes.** That's why road rides use
  `cycling-regular`. Don't switch it back.
- **ORS `paved_frac + unpaved_frac` does NOT sum to 1** — untagged surface is counted as
  neither (defaults to paved upstream). So we display confirmed-gravel %, not paved %.
- **ORS often reports ~0% gravel where gravel really exists** (e.g. Champaign township
  roads). `--surface-source osm` + the correction cache are how real gravel gets in.
- **Zone detector calibration:** farmland is the DOMINANT signal (`W_FARM=1.0`); quiet
  road-km is only a minor tiebreaker (`W_GRID=0.15`) because a dense road grid correlates
  with SUBURBIA (more km) — rewarding it heavily picks the wrong direction. `W_ART=0.4`.
  Two None-gates: relative standout test (`min_advantage`) + "already in good country"
  (home inner-ring farmland density ≥ 70% of best sector → no staging). Verified:
  a suburban start → a quiet zone ~12km out; an already-rural start → None.
- **Geometric loop sizing:** `_polygon_loop_waypoints` builds a regular n-gon whose
  circumscribing circle is offset one radius along `bearing` so the START sits on the
  circle and the loop bulges toward `bearing` (aim into wind). Radius solved so crow
  perimeter × `detour` (1.25) ≈ target. `_LOOP_SIDES` cycles 5/4/6 and orientation
  flips by seed for variety. Convex ⇒ no self-crossings by construction. The vertices
  are crow points ORS snaps to the grid (works in practice, like the rectangle); a bad
  seed that 404s is just skipped in `generate_candidates` (it catches `HTTPError`).
- **Staging avoids the off-road centroid:** the zone center is a farmland CENTROID that
  often sits mid-field — routing a leg TO it 404s (ORS code 2010 "no routable point").
  So `_make_staging` NEVER targets it: the stem aims at a crow point a loop-radius SHORT
  of the centroid, and the geometric loop (anchored at the stem's real routed end,
  bulging toward the zone) centers itself ON the centroid using only routable ring
  waypoints. (Was previously: build a `round_trip` loop first and aim the stem at
  `l_coords[0]`; round_trip is retired from staging.)
- **`_strip_backtracks` runs PER ORS LEG, never on assembled geometry:** a deliberate
  out-and-back / lollipop-stem retrace (out leg + reversed out leg) looks exactly like
  one giant backtrack, so cleaning the *assembled* route would collapse the whole
  return. It lives inside `_ors_directions` (after `_waytype_run_km`, which needs the raw
  positional indices), and keeps `eles` aligned.
- **Staging distance cap:** `_resolve_ride_area` rejects a zone whose crow transit >
  0.3·target (so 2·transit ≤ 0.6·target) — a 20mi ride won't try to stage 12km away;
  staging needs a bigger `-d`.
- **`Candidate.score_coords`:** wind is scored on `score_coords or coords`. Staging sets
  it to the loop only, so fixed transit legs don't pollute the wind line. Default None.
- **`generate_candidates(... zone=...)`:** only emits the `staging` shape when a zone is
  passed; strips it otherwise.
- **Nominatim can't reliably geocode named TRAILS or "&" intersections** — tell the user
  to use a street address or drop a pin for `lat,lng` at a trailhead.
- **RWGPS API quirks** (`rwgps.py`): auth is api_key + auth_token **headers**
  (`x-rwgps-api-key` / `x-rwgps-auth-token`), no password — the token is minted from the
  API client's edit page. Trips are `/trips.json` (the `users/{id}/trips.json` form 404s).
  **Pagination meta is nested under `meta.pagination`** (`page_count`, `next_page_url`) —
  reading it at top level silently stops after page 1. Track points use legacy compact
  keys (`y`=lat, `x`=lng, `e`=elev) — parsed defensively. `departed_at` is **tz-aware**;
  `get_wind_historical` strips it to naive local (Open-Meteo `timezone=auto` is naive) or
  subtraction with `datetime.now()` raises.
- **`learn` is slow at scale:** one Overpass + one historical-wind call per trip (~0.5s+
  paced). Use `learn --no-surface` for a fast geometry/distance/direction pass; full `learn`
  for surface/path-run/lane/wind. Shape comes from RWGPS `track_type` (not the computed
  `_self_overlap`, which is a fallback). Trips cache in `~/.windroute/trips/`.
- **Windows console is cp1252:** `cli.py` reconfigures stdout to UTF-8 at import or rich's
  box-drawing/maths glyphs (`█ ≤ ⚠`) raise `UnicodeEncodeError`. Don't remove that.

## What the trip history revealed (Jun 2026, newest 200 trips → 108 outdoor rides)

The owner's *better-era* rides (he asked for the newest 200, not all 604 — older ones predate
his improved route-making). Every tuning decision below is backed by this:
- **Typical ride ~22 mi** (middle half 10–33). **82% loops** (89/108), few out-and-backs →
  loop-first default validated.
- **Avoids gravel** (mean 3%, median 0%) and **busy highways** (mean 0%) → gravel penalty and
  `W_BUSY=1.5` justified, unchanged.
- **Paths are connectors:** median 38% total path but only 15% longest contiguous run → the
  run-based `W_PATH` (free band 0.25) leaves his normal rides untouched; only the minority with
  long stretches (run p90 49%) get hit. This is *why* the path model was redesigned.
- **Seeks bike lanes** (mean 24%, p90 53%) → `W_BIKELANE` 0.4→0.6 (could go higher).
- **Wind premise holds:** 64% of rides go into the wind first (mean score +0.52, ~61° off the
  headwind line) → `w_wind=1.0` supported.
- **Direction:** a strong, consistent directional lean (one dominant sector, then two
  adjacent ones) — confirms the "ride out toward the open farmland" preference and
  motivates the preferred-direction bias (next steps).

## Possible next steps (discussed, NOT built)

- **Preferred-direction / force-a-path vs wind** (now strongly data-backed — SSE is the
  owner's dominant heading): owner "gets on the Hickory Creek Trail going south" but the
  optimizer aims into the wind first. Ideas: (a) a "prefer/force this direction or path"
  bias; (b) a "best-day finder" scanning the 7-day forecast for the day that best rewards a
  chosen-direction ride. The `learn` direction histogram could seed a default bias.
- **Auto-tune weights from `learn`:** the analysis already emits suggested weight changes;
  a future pass could fit the weights to the trip history instead of hand-tuning. (Deliberately
  deferred — owner chose "analysis + review" over auto-retune.)
- **Strava-heatmap screenshot parsing** (owner idea, explicitly "later"): no Strava API
  access, but could screenshot a local heatmap to parse as a popularity/scenery signal.
- **More surface sources:** Indiana DOT `LRSE_Surface_Type` ArcGIS (clean template);
  county road-commission GIS for local gravel data (Michigan's open layer lacks surface).
- **Watch the staging value-add on a cross-wind (E/W) day** — logically sound but never
  yet seen winning live (test days had SW wind, which already favors riding south).
