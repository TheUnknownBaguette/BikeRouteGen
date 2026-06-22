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
  forecast + **archive** for historical wind, no key) with **US NWS** (`api.weather.gov`)
  as a wind fallback, OSM Nominatim (address geocode), **Photon** (komoot; web-form
  location autocomplete — built for type-ahead, which Nominatim's policy forbids),
  Overpass (OSM surface / bike-lane / busy-path / farmland reads), **Ride with GPS v1**
  (recorded-trip import, needs an api_key + auth_token). `ORS_API_KEY` is saved at the
  Windows **User** level; RWGPS creds live in `~/.windroute/rwgps.json`.
- **Owner's riding preferences (these drive the scoring):** quiet county/township
  roads; avoid busy US-highways; dislikes pure out-and-backs (retracing); uses multiuse
  paths as **connectors to reach good riding, never as the ride itself** (would never
  out-and-back a trail); loves on-road bike lanes; rides *out toward quiet rural roads
  and open farmland* for quiet riding. (All confirmed by trip-history analysis — see findings.)
  - **Will spend real effort to reach good roads — road quality > proximity/wind line.** The
    home area (Mokena, IL) is suburban but rural country surrounds it; the owner happily rides a
    **crosswind transit** to get off suburban roads onto quiet farm roads. So reaching good country
    can outweigh a pure wind line. The default generated routes **don't escape suburbia enough yet**
    (they optimize wind from the start); `--ride-area auto` does it on request. See work-plan
    "Captured ideas" + next steps below.
  - **Sequences wind by EXPOSURE, not just bearing:** routes headwind legs through sheltered
    paths/urban and saves open rural/farmland for the tailwind leg. `wind_score` doesn't model this
    yet (it's bearing-only). Validated by recovery.gpx (Mokena, WSW wind, 2026-06-15). See "Captured
    ideas".

## How to run (PowerShell)

```powershell
cd path\to\BikeRouteGen
.\.venv\Scripts\Activate.ps1
python -m windroute.cli plan -l "Chicago, IL" -d 30 -s "2026-06-14 08:00" -r road
python -m windroute.cli plan -l "Aspen, CO" -d 25 --classify   # adapt tuning to terrain
python -m windroute.cli classify -l "Aspen, CO"                # terrain archetype only (no ORS key)
```

`--classify` (and the `classify` command) read the terrain archetype (Tasks 1-2) and adapt
weights/shapes/zones + normalize the busy penalty (Task 4a); off by default so the validated
grid-farmland road path stays byte-identical. `classify` needs no ORS key (Overpass + Open-Meteo only).

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
Form niceties: the **start point autocompletes** addresses + towns as you type
(`/suggest` → `engine.suggest_places`, Photon), **start time** is a `datetime-local`
picker (prefilled client-side to the local hour; empty → "now"), each advanced option has
an **ⓘ hover tooltip**, and the prefilled location is **Chicago, IL** (not the owner's
town). `run.bat` is **self-healing**: it builds the venv on first run and rebuilds it if
one was synced from another machine (a venv isn't relocatable — see gotchas).

**Hosting / future self-host (so friends need no key):** `Procfile` runs `waitress`
(cross-platform prod server, in requirements) via `waitress-serve --listen=*:$PORT
webapp:app`. CURRENT: hosted on a free service (e.g. Render) — connect the repo, that
start command, and set `ORS_API_KEY` as a server SECRET (never in the public repo).
FUTURE self-host (owner wants their own mini server eventually, doesn't have one yet,
2026-06): the SAME `waitress-serve` command runs on any box (Windows/Linux/Pi) — set
`ORS_API_KEY` in that machine's env, open the port, done. No code changes; the env-
driven HOST/PORT + waitress are the provisions for it. Watch the ORS free-tier limit
(~2000 calls/day, ~12-15 per plan) — a paid key or self-hosted ORS if it grows.

**Public-instance hardening (all default-on, no config):** security headers (CSP
`default-src 'self'`, X-Frame-Options, Referrer-Policy, HSTS over HTTPS), server-side
input clamps (candidates 1-20, distance/tolerance bounds), a per-IP in-memory rate limit
on `/plan` (12 / 5 min — each plan is ~12-15 ORS calls), a 32 KB body cap, friendly error
messages (raw exceptions logged not shown), and a visible **`/about`** page (privacy
policy + ride-safety / "vibecoded, no warranty" disclaimer) linked from a heads-up banner
and footer.

## Architecture / file map

```
windroute/
  engine.py       core: geocode + suggest_places (Photon autocomplete) + parse_compass, wind (+ get_wind_historical), geometric route gen + shapes, scoring, route-option selection (NO I/O — pure fns)
  planner.py      SHARED pipeline: plan_routes() -> PlanResult (geocode->wind->staging->generate->surface->corrections->evaluate->options); optional location_label override. No printing/files. CLI + web both call it.
  zones.py        find_ride_zone: best quiet riding zone, nearest OR a forced compass direction (prefer_bearing) — for --ride-area staging
  regions.py      classify_region -> RegionProfile (terrain archetype): one Overpass read (roads + land-use) + Open-Meteo elevation (relief), cached per ~0.1° cell. classify_archetype() is pure. Diagnostic only so far (work-plan Task 1) — does NOT yet drive weights
  surface.py      OSM/Overpass surface + bike-lane + busy/path + gravel-quality source (OverpassSurface); overpass_json mirror-fallback; SurfaceProvider registry (Task 5)
  valhalla.py     EXPERIMENTAL gated wind-biased router seam (Task 7) — off unless WINDROUTE_VALHALLA_URL set; untested against a live server
  corrections.py  personal correction cache (~/.windroute/corrections.json) + road-notes parser
  rwgps.py        Ride with GPS v1 API client (auth, list/fetch trips, trip cache, creds)
  learn.py        analyse imported trips -> rider profile + suggested weight changes (pure)
  render.py       map image + GPX output
  cli.py          CLI front-end: plan / classify / mark / roads-import / corrections / forget / rwgps-login / import / learn
webapp.py         local/hosted web front-end (Flask): routes / /plan /suggest /about; headers + rate limit
discord_bot.py    optional Discord front-end (thin over planner.plan_routes; needs discord.py; not wired in)
templates/        web HTML: base / index (form) / results / about (privacy + disclaimer)
static/           app.js (datetime + autocomplete JS); out/ generated maps+GPX (gitignored, swept hourly)
run.bat           double-click launcher; self-builds/repairs the venv (see gotchas)
Procfile          prod start command for a host (waitress-serve webapp:app)
README.md         user-facing setup + usage
requirements.txt  deps
```
**Design rule:** every front-end (CLI, web, Discord) is a thin layer over
`planner` (orchestration) + `engine`/`render` (logic + output). Never reimplement the
pipeline in a front-end — `plan_routes` is the one place it lives.

---

## Features built (all DONE + verified)

- **Region archetype classifier** (`regions.py`, work-plan Task 1 — DONE): `classify_region((lat,lng))`
  labels the country around a start as `grid-farmland`, `forested-rolling`, `mountain`,
  `suburban-sprawl`, `coastal`, `arid-open`, or `unknown`, returning a `RegionProfile`
  (archetype + confidence + raw feature vector). Features come from ONE Overpass read
  (road density + class mix from highways; farmland/forest/built-up/water AREA fractions via
  shoelace on land-use/natural polygons; coastline length) plus ONE keyless Open-Meteo
  *elevation* read for coarse relief (range + std over a grid). `classify_archetype(features)`
  is a **pure** prioritized decision list (offline unit tests in `tests/test_regions.py`).
  Cached per ~0.1° cell so repeat plans in an area don't re-fetch. Verified live: Champaign IL
  → grid-farmland, Aspen CO → mountain, Naperville IL → suburban-sprawl, Outer Banks → coastal,
  rural NV → arid-open. **Diagnostic only:** surfaced via the CLI `classify` command + `plan
  --classify` + a web checkbox, but it does NOT yet change scoring/zone weights (that is Task 2,
  gated so grid-farmland stays byte-identical). Degrades to `unknown` (low confidence) on any
  fetch failure rather than aborting a plan.
- **Archetype-keyed weight + shape tables** (work-plan Task 2 — DONE): the route scorer,
  loop geometry, default shapes, and quiet-zone scoring now adapt to the region archetype.
  `engine.RouteWeights` + `WEIGHTS_BY_ARCHETYPE` (route-score tunables), `LOOP_GEOM_BY_ARCHETYPE`
  (n-gon side counts + detour — curvier loops for mountain/forest), `SHAPES_BY_ARCHETYPE`
  (mountain/forested drop the grid-only `rectangle`), and `zones.ZoneWeights` +
  `ZONE_WEIGHTS_BY_ARCHETYPE` (farmland in the grid, **forest/water** signals added for
  hills/coast). Helpers: `engine.weights_for / loop_geom_for / shapes_for`, `zones.zone_weights_for`.
  Threaded through `plan_routes` (it derives them from `region.archetype` and passes them to
  `generate_candidates(loop_geom=)`, `evaluate(weights=)`, `find_ride_zone(archetype=)`).
  **Regression-safe:** the `grid-farmland` row is built FROM the existing constants (single
  source of truth) and `unknown`/`None` fall back to it, so the default path (classify off) is
  byte-identical — `weights_for(None) is weights_for("grid-farmland")`, and grid-farmland's zone
  Overpass query is unchanged (forest/water only fetched when an archetype uses them). Adaptivity
  is still **opt-in behind `--classify`** for now (regression-trivial + keeps the flaky Overpass
  read off every plan's critical path); flipping to default-on is a one-line change once the
  Overpass mirror-fallback hardening lands. Offline invariant tests in `tests/test_weights.py`
  (grid-farmland == constants, default path identical, mountain drops rectangle, etc.); live-verified
  that per-archetype zone scoring shifts the chosen direction (forest/coast vs farmland). **Non-grid
  rows are a deliberately conservative FIRST PASS — calibrate against real rides later (Task 8).**
- **Road vs. gravel asymmetry** (work-plan Task 3 — DONE): ride type is now a first-class weight
  profile, and `evaluate` has **no ride-type `if` in its formula** — it's fully weights-driven.
  - **3a Two profiles:** `engine.ROAD_WEIGHTS` / `GRAVEL_WEIGHTS`; `engine.as_gravel(rw)` derives the
    gravel profile from any road one (so archetype tuning still composes). `w_wind` is now a
    RouteWeights field (road 1.0 / gravel 0.55 — wind matters less on gravel). `weights_for(archetype,
    ride_type)` picks + transforms.
  - **3b Gravel-seek reward:** road keeps the convex gravel **penalty**; gravel swaps it for a
    **seek reward** (`engine._gravel_seek_reward`): ramps to full by `gravel_seek_lo` (0.5), holds
    across the band to `gravel_seek_hi` (0.75), then tapers (floored at 0.7) — so a ~30% gravel area
    still returns a sane route. No-op on road rides (`gravel_seek=0`).
  - **3c Quality grading:** `surface.classify_quality_tags` + `OverpassSurface.classify_quality`
    bucket OSM `surface`/`tracktype`/`smoothness` into **good** gravel (fine_gravel/compacted/grade2-3,
    a gravel-ride bonus) and **bad/unrideable** (mud/ground/sand/grade5/awful smoothness), the latter
    **hard-avoided for BOTH ride types** (`w_unrideable=2.5`). New `Candidate.good_gravel_frac` /
    `unrideable_frac`, set by the planner's OSM surface steps. Live-verified: a route on a good-tagged
    way reads (1.0, 0.0); on a bad-tagged way (0.0, 1.0).
  - **Regression-safe:** the grid-farmland ROAD profile is unchanged (gravel-seek/good/unrideable
    terms are 0 there, and `unrideable_frac` is 0 without OSM quality data), so the default road path
    stays byte-identical. Quality grading only bites under `--surface-source osm|both` (like bike
    lanes). Gravel scoring DID change (it was never validated — only road rides were). Display: CLI
    table flags good gravel `+Ng` / unrideable `!N`, option reasons + web cards/table show them.
- **Volume-first busy reframe** (work-plan Task 4a — DONE): the arterial penalty is now
  **corridor-normalized**. `evaluate(..., busy_baseline=)` charges busy only on arterial mileage
  ABOVE the corridor's *unavoidable* level (the quietest candidate's `busy_frac`), plus the free
  band — so a region whose quietest available roads are still somewhat busy doesn't tank every
  route; the relatively-quietest still wins. `plan_routes` sets `busy_baseline = min(busy_frac)`
  across the candidates **only when `--classify` is on** (the adaptivity switch), and adds a note
  when arterials look unavoidable. Default (classify off) `busy_baseline=0.0` -> the absolute
  penalty, **byte-identical**. NOTE: this means a grid-farmland plan WITH `--classify` can differ
  very slightly from without when there's an unavoidable arterial (intended — more correct); the
  regression guarantee is specifically the classify-OFF default path. **Task 4b (real AADT) is
  deferred to Task 5** — the work plan defines it as an optional surface-provider, so it lands with
  the provider registry. The class proxy (ORS waytype-1 / OSM busy classes) stays the universal
  baseline.
- **Surface-provider registry + graceful degradation + Overpass mirror-fallback** (work-plan
  Task 5 — DONE):
  - **Registry:** OSM `surface=*` (`OverpassSurface`) is the UNIVERSAL baseline; optional
    region-specific sources (state DOT layers, county GIS, AADT — Task 4b) plug in as
    `surface.SurfaceProvider` subclasses gated by `applies_to(lat,lng)` (admin boundary).
    `surface.REGIONAL_SURFACE_PROVIDERS` (empty by default) + `regional_providers_for(lat,lng)`;
    `plan_routes` runs the applicable ones after the OSM baseline, so adding/removing one changes
    only that region's reads. **No concrete regional providers shipped** — the mechanism is the
    deliverable; this is the home for Task 4b AADT and future state providers.
  - **Graceful degradation:** `OverpassSurface.coverage()` = share of the route within range of an
    OSM-surface-tagged way. `PlanResult.data_confidence` ∈ {`ok`, `low`, `ors-baseline`} via
    `planner._surface_confidence` (LOW_COVERAGE_FRAC=0.25). Thin OSM coverage (or a failed lookup)
    under `osm`/`both` -> `low` + a user-facing note ("treat gravel figures as a rough hint");
    `ors` mode -> `ors-baseline` (no nag). CLI renders the low note in bold yellow; web shows it in
    notes. A foreign/low-coverage start now returns a route PLUS an honest low-confidence flag.
  - **Overpass mirror-fallback:** `surface.overpass_json(query, timeout, url)` tries
    `OVERPASS_MIRRORS` in order (overpass-api.de -> maps.mail.ru -> kumi.systems). ALL Overpass
    reads (surface, regions, zones) go through it, so the frequent overpass-api.de 504s no longer
    sink a read. Live-verified: a default `classify_region` succeeds via a mirror when the primary
    is down.
- **Local-search candidate refinement** (work-plan Task 6 — DONE, opt-in `--refine`):
  `engine.refine_candidate` hill-climbs a waypoint-built candidate (loop/rectangle, which now
  carry their `Candidate.waypoints`): nudge each interior corner a small step, re-route that loop
  via ORS (`_candidate_from_waypoints`), and KEEP a move only if it raises the **full** objective
  (a `score_fn` supplied by the caller, so the non-additive surface/wind/quiet score is honored per
  move) AND length stays within tolerance. `planner._refine_candidates` builds that `score_fn` —
  reusing the prebuilt `OverpassSurface` index + correction cache (no extra network) so refined
  routes are scored on the SAME basis as the seeds — and refines the top `REFINE_TOP` (2) candidates
  at `REFINE_CALLS_EACH` (5) ORS calls each. Runs after `evaluate`, before `select_route_options`;
  then re-evaluates. **Not** textbook edge-2-opt: public ORS exposes no per-edge control (that's
  Task 7), so the achievable local move is corner-nudge + re-route. **Regression-safe:** off by
  default (`refine=False`) → none of it runs, behaviour identical; the seed's own `total_score` is
  the baseline so its one-time corrections aren't double-applied. Budget: adds ≤ `TOP*CALLS_EACH`
  (~10) ORS calls only when `--refine` is set. Offline tests (`test_weights.py`: improves within
  budget, length cap, skips non-refinable).
- **Wind-biased routing** (work-plan Task 7):
  - **`wind` shape (stopgap, DONE, opt-in `--shapes wind`):** rides headwind-OUT to a turnaround
    (`engine._make_wind_loop`), then routes home AVOIDING the outbound corridor via ORS
    `avoid_polygons`, so the tailwind return takes **different roads** — the strategy the owner
    rides by hand (recovery.gpx). The corridor is `engine._corridor_multipolygon` — a MultiPolygon
    of small squares sampled along the outbound, **excluding** boxes within a clearance of the start
    / turnaround so the return's endpoints aren't trapped (ORS 2010). Falls back to a plain return
    if the avoided route is unroutable. `_ors_directions` gained an `avoid_polygons` arg. Opt-in
    (in `SHAPES` + `shapes_for`'s always-set, NOT a default), so default plans are unchanged; costs
    2-3 ORS calls per `wind` seed. Verified offline (corridor excludes endpoints; return leg gets
    the avoid; fallback path); the live "different roads" check needs an ORS key.
  - **Valhalla full version (`windroute/valhalla.py`) — gated + EXPERIMENTAL, off by default.**
    `enabled()` is True only when `WINDROUTE_VALHALLA_URL` is set; otherwise the app routes entirely
    on ORS and none of it runs. When enabled, the `wind` shape's outbound corridor comes from
    Valhalla (re-traced through ORS for surface/waytype extras); any failure falls back to ORS.
    **Honest caveat:** untested against a live Valhalla (none available), and TRUE per-edge wind
    biasing needs a **custom Valhalla costing model** — stock `bicycle` costing has no bearing-vs-
    wind term. So this is the ready-to-wire seam, not a verified feature; the wind line still comes
    from the turnaround geometry. (ORS `weightings.quiet` is a verified public-API no-op — why a
    self-hosted router is needed at all.)
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
  hard-avoids paths/lanes and kept routes off a paved local rail-trail). Balance
  lives in scoring, not the profile.
- **Auto-detect quiet ride zone + staging** (`--ride-area auto`, a compass direction, or a
  place/`lat,lng`): `zones.find_ride_zone` does ONE Overpass call, buckets quiet grid roads
  + arterials + farmland into 12 directional sectors, scores them (farmland dominant), and
  returns the best sector's centroid — or None. The `staging` shape transits there, loops
  on the wind, and rides home; only the destination loop is wind-scored. `-d` = TOTAL miles.
- **Directional ride-area staging** (`--ride-area south` / `SSE` / `NW` …): stages to the
  best quiet zone *in that direction* instead of geocoding the word as a place.
  `engine.parse_compass` turns a direction word/abbrev into a bearing (returns None for
  non-directions, so real place names still geocode); `find_ride_zone(prefer_bearing=…)`
  picks the best-scoring sector within ±45° and **skips** the standout / already-in-good-
  country gates (the user explicitly chose the way). `auto` and place/`lat,lng` unchanged.
- **Web form UX:** start-point **autocomplete** (addresses + towns via Photon through the
  `/suggest` same-origin proxy; picking a suggestion routes from its **exact coords** and
  shows the address as the label via `plan_routes(location_label=…)`), a **datetime-local**
  start-time picker, and an **ⓘ tooltip** on each advanced option (ride type / surface /
  ride-area / tolerance / candidates). Default location Chicago (privacy).
- **Descriptive output names:** files auto-name from the ride —
  `render.route_basename(when, dist_km, unit, shape, wind_from_deg)` →
  `jun14-30mi-loop-Swind`, deduped via `render.dedupe_names`. CLI uses them when `-o` is
  omitted (pass `-o` for the old `<out>`/`-alt1/-alt2`); the web app stores files under a
  token but the GPX download link carries this as the browser `download=` name. The GPX
  internal `<trk><name>` is already descriptive.
- **Public-instance hardening + privacy page:** security headers, server-side input clamps,
  per-IP `/plan` rate limit, body-size cap, friendly error messages, and a `/about` page
  (privacy policy + ride-at-your-own-risk / "vibecoded, no warranty" disclaimer). All
  default-on. See "Key decisions" for the CSP posture and the autocomplete-proxy reason.
- **Self-healing `run.bat`:** builds the venv on first run and detects + rebuilds one that
  was synced from another machine (a venv bakes in the creating Python's absolute path, so
  OneDrive-synced copies can't run). Web-only users need only Python + an ORS key.

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
- **Web autocomplete uses Photon, NOT Nominatim:** Nominatim's policy forbids per-keystroke
  autocomplete; Photon (komoot) is built for it (Open-Meteo is a town-only fallback).
  `/suggest` is a **same-origin proxy** because the page CSP is `default-src 'self'` — a
  browser fetch straight to a geocoder would be blocked, and the proxy also lets us send a
  proper User-Agent. `suggest_places` over-fetches and re-ranks **prefix-then-population**
  so a populous prefix match outranks a tiny exact-name village the user didn't mean.
- **CSP posture:** `script-src 'self'` (the one inline `<script>` was moved to
  `static/app.js`), but `style-src` keeps `'unsafe-inline'` because the templates use inline
  `style="…"` attributes throughout — tightening it would mean stripping all of those.
- **Picked-suggestion precision:** selecting an autocomplete item posts hidden
  `picked_lat/lng/label`; if the visible text still equals the label, webapp routes from the
  exact coords and passes `location_label` so results show the address (not raw coords).
  Editing the text after picking clears the coords. The JS coordinate-skip matches a lat,lng
  *pair* so house-number addresses ("123 Main St") still autocomplete. `datetime-local`
  posts `YYYY-MM-DDTHH:MM` (parsed by `dateutil`); empty/absent → webapp uses `"now"`.
- **A virtualenv is NOT relocatable:** `pyvenv.cfg` + `Scripts/` hardcode the creating
  Python's absolute path. `.venv/` is gitignored, but the project lives in OneDrive, which
  synced machine-A's venv onto machine B where it couldn't run. `run.bat` self-heals (runs
  `python --version`, rebuilds on failure). Never commit or sync a venv.
- **Privacy:** all in-repo examples use Chicago / a landmark address / generic IL towns — the
  owner's home town + street address were scrubbed from README, CLI help, docstrings,
  comments, and this file. **Current files only**; git history still contains them and the
  owner is OK with that ("just don't want it obvious").
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
- **Region classifier — relief is range/std, NOT `_smoothed_ascent`:** the work plan suggested
  reusing `_smoothed_ascent`, but that measures ascent *along an ordered route*. Region-level
  relief is elevation **spread over a sampled grid** (range + std), fetched from Open-Meteo's
  keyless `/v1/elevation` endpoint (up to 100 pts/call; we send a ≤25-pt disc grid). Mountain
  detection keys off `relief_std_m` / `relief_range_m`; if the elevation fetch fails those are
  `None` and the classifier simply can't pick `mountain` (no crash).
- **Archetype tuning is regression-safe by construction:** the `grid-farmland` `RouteWeights`/
  `ZoneWeights` rows are built FROM the existing module constants (not re-typed numbers), so they
  can't drift, and `weights_for(None) is weights_for("grid-farmland")` (identity). `evaluate` and
  `find_ride_zone` reduce to the exact prior arithmetic when archetype is None/grid-farmland —
  e.g. zone `land`/`land_in` = `w_farm*farm` only (forest/water weights 0), so the "already in good
  country" density ratio is unchanged. **Zone centroid weights are kept SEPARATE from score weights**
  (`FARM_CENTROID_W=1.0` ≠ `W_FARM=1.0` conceptually; grid centroid `0.1` ≠ score `0.15`) — reusing
  score weights for the centroid would silently move the grid-farmland zone center and break
  byte-identity. New forest/water centroid weights are 0 for grid-farmland.
- **Region classifier Overpass query gotcha:** the union MUST be `(...);out geom;` — a missing
  `;` after the `)` is a silent **400 Bad Request** (cost me a debug cycle). The query is heavy
  (roads + all land-use/natural at 10 km), so `overpass-api.de` frequently **504s** under load;
  the classifier then returns `unknown` (low confidence) by design. **Mitigated in Task 5:** all
  Overpass reads now go through `surface.overpass_json`, which falls back across `OVERPASS_MIRRORS`
  (overpass-api.de -> maps.mail.ru -> kumi.systems), so a 504 on the primary no longer kills a read.

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

- **Wind-exposure weighting (shelter vs. open)** — rider-validated (recovery.gpx, 2026-06-15):
  scale the headwind penalty DOWN on sheltered land cover (urban/forest/path) and the tailwind
  reward UP on open land cover (farmland), since `wind_score` is bearing-only today. Uses the
  land-cover signals already pulled by `regions`/Overpass. See work-plan "Captured ideas".
- **Transit-to-good-roads (suburban escape) by default** — the owner will ride a crosswind to
  reach quiet farm roads; default routes don't escape suburbia enough (only `--ride-area auto`
  does). Bias default generation toward the nearest good-riding zone when the start archetype is
  `suburban-sprawl`. See work-plan "Captured ideas".
- **Preferred-direction / force-a-path vs wind** (data-backed — the owner has one dominant
  heading): **directional ride-area *staging* is now built** (`--ride-area <direction>`), but
  it only biases staging. Still open: (a) a direction/path bias for **normal (non-staging)
  rides**, where the optimizer still aims into the wind first (owner often wants to ride a
  particular local trail/road out regardless); (b) a "best-day finder" scanning the 7-day
  forecast for the day that best rewards a chosen-direction ride. The `learn` direction
  histogram could seed a default bias.
- **Auto-tune weights from `learn`:** the analysis already emits suggested weight changes;
  a future pass could fit the weights to the trip history instead of hand-tuning. (Deliberately
  deferred — owner chose "analysis + review" over auto-retune.)
- **Strava-heatmap screenshot parsing** (owner idea, explicitly "later"): no Strava API
  access, but could screenshot a local heatmap to parse as a popularity/scenery signal.
- **More surface sources:** Indiana DOT `LRSE_Surface_Type` ArcGIS (clean template);
  county road-commission GIS for local gravel data (Michigan's open layer lacks surface).
- **Watch the staging value-add on a cross-wind (E/W) day** — logically sound but never
  yet seen winning live (test days had SW wind, which already favors riding south).
- **Smarter route optimization (algorithm)** — prompted by a "use weighted graph
  optimization / AI to make the map" suggestion. Assessment: ORS *already* does weighted
  graph optimization (A*/CH on the OSM graph) per leg, and "AI to create the map" is a
  non-starter (OSM is ground truth; a generative map would hallucinate roads). The real
  reason it's generate-and-score, not one clever Dijkstra: the objective is **non-additive /
  global** — "headwind while fresh, tailwind home" depends on which half of the loop an edge
  is in, plus target-length + longest-contiguous-path + self-crossings are whole-route
  properties. That makes it an **orienteering-class (NP-hard) problem**; komoot/ORS
  `round_trip` are all heuristics too. Realistic improvements, best bang-for-buck first:
  (1) **deterministic local-search/2-opt refinement** on existing candidates — edge swaps on
  the real graph to raise wind score while holding length; fits the current architecture, no
  new infra (offered to prototype, deferred). (2) **wind-biased edge weights** for the
  outbound leg (cost lowered for edges pointing into the headwind), route out to a turnaround
  then home on different roads — needs a router we control (self-hosted ORS/Valhalla/
  GraphHopper; public ORS exposes no per-edge custom weights, and its `quiet` weighting is a
  no-op). (3) full metaheuristic (SA/genetic/ant-colony) — most principled, biggest build,
  still heuristic on the length constraint, uncertain payoff.
