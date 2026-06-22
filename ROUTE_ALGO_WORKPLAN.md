# BikeRouteGen — Route Algorithm Work Plan

**Purpose:** this is a work order for a coding session that has the full `BikeRouteGen`
codebase. It turns a set of agreed algorithm improvements into ordered, verifiable tasks.
Read `PROJECT_CONTEXT.md` first — that file is the source of truth for what currently
exists; this file is the source of truth for what to change and in what order.

This supersedes any earlier free-form "improvement notes." If both are present, follow this one.

---

## 0. Read this before touching code

### Prime directives (hold for EVERY task)

- **Keep the architecture.** `engine` functions stay pure (no I/O). Orchestration stays in
  `planner.plan_routes` — it remains the single pipeline; never reimplement it in a front-end.
  CLI / web / Discord stay thin pass-throughs. New tunables are **named constants beside the
  existing weights**, documented inline.
- **Stay in the generate-and-score frame.** This problem is non-additive / orienteering-class
  (see context file). Do **not** try to "AI the map" or replace ORS's per-leg graph routing.
  Improvements happen in *signals, weights, candidate generation, and refinement* — not by
  swapping the paradigm.
- **Do not regress the home region.** The grid-farmland (flat IL) tuning is validated against
  108 real rides. Every new weight must either (a) default to a **no-op** in grid-farmland, or
  (b) be **gated behind the region archetype** so the grid-farmland row keeps today's exact
  numbers. A golden-route regression check (§Regression gate) guards this.
- **Verify names against the real code.** Function/constant names below come from the context
  file and may have drifted. Confirm signatures before editing; adapt names as needed.

### Decisions already made this round (don't relitigate)

- **Safety is volume-first, not stress-model.** We rejected Level of Traffic Stress and the
  speed/shoulder/lanes/sightline/lighting penalties: they're calibrated for a casual rider, not
  a confident road cyclist. For this rider, overtaking *volume* is the dominant risk and the
  annoyance signal both. See Task 4 and the Deferred list.
- **Road vs. gravel is asymmetric and explicit.** Road = minimize gravel, wind weighted high.
  Gravel = *seek* gravel (positive reward, not just absence of penalty), tolerate worse wind.
  See Task 3.
- **Adaptivity is the headline.** The current tuning encodes one place; the classifier (Task 1)
  is what lets the rest generalize.

---

## Task sequence

| # | Task | Why now | Primary files |
|---|------|---------|---------------|
| 1 | Region archetype classifier | Foundation — everything else keys off it | `zones.py` (or new `regions.py`), `planner.py` |
| 2 | Archetype-keyed weight + shape tables | Makes weights location-aware; regression-safe | `engine.py`, `zones.py` |
| 3 | Road/gravel asymmetry (profiles + seek + quality) | Direct ask; small | `engine.py`, `surface.py` |
| 4 | Volume-first safety/quiet (reframe) | Generalizes the busy penalty | `engine.py`, `surface.py` |
| 5 | Surface-provider registry + graceful degradation | Portability + honesty on thin data | `surface.py`, `planner.py` |
| 6 | 2-opt candidate refinement (optional) | Squeeze existing candidates | `engine.py`, `planner.py` |
| 7 | Wind-biased edges / self-hosted router | Biggest build, do last | new router adapter |
| 8 | Region-aware tuning validation | Trust weights across regions | `learn.py`, `planner.py` |

Tasks 1–4 are the core. 5 hardens portability. 6–8 are upside, gated behind flags, do after 1–4 land.

---

## Task 1 — Region archetype classifier (foundation)

> **STATUS: DONE (2026-06-15).** Built `windroute/regions.py` (`classify_region` + pure
> `classify_archetype`, `RegionProfile`), per-cell cache, CLI `classify` command + `plan
> --classify`, web checkbox, offline tests (`tests/test_regions.py`). Wired as step 0 of
> `plan_routes` (param `classify`, default off → regression-safe). Deliberately **does not
> change weights yet** (that's Task 2). Relief uses Open-Meteo elevation range/std, not
> `_smoothed_ascent` (see PROJECT_CONTEXT "Key decisions"). Live-verified on the acceptance
> locations. **Next: Task 2.**

**Goal:** before scoring, classify the start's surroundings into a terrain archetype so weights
and shapes can adapt to it.

**Build:** `classify_region(center, radius_km) -> RegionProfile` — one Overpass read (cached per
region cell), pure aside from that fetch. Put it in `zones.py` or a new `regions.py`. Sample:
land-use composition (farmland / forest / residential / water % of area), road-class mix and
road density, and coarse relief (reuse `_smoothed_ascent` on a sparse sample). Return a named
`Archetype` plus the **raw feature vector** (keep it inspectable/loggable).

**Archetypes (starting set — calibrate later against real rides):**
`grid-farmland`, `forested-rolling`, `mountain`, `suburban-sprawl`, `coastal`, `arid-open`,
`unknown` (low tag coverage → falls back to `grid-farmland` weights but flags low confidence).

**Wire-in:** add as step 0 of `plan_routes`; thread the archetype into `find_ride_zone` and the
scorer. Add a CLI flag (e.g. `--classify`) and a web debug line that prints the archetype +
feature vector.

**Acceptance:**
- IL grid start → `grid-farmland`; a mountain start → `mountain`; a dense suburb → `suburban-sprawl`.
- Feature vector logged. Sparse-tag start → `unknown` + low-confidence flag (no crash).
- Overpass cached per region cell (verify it's not re-fetched within a session/area).

---

## Task 2 — Archetype-keyed weight + shape tables

> **STATUS: DONE (2026-06-15).** `engine.RouteWeights` + `WEIGHTS_BY_ARCHETYPE`,
> `LOOP_GEOM_BY_ARCHETYPE`, `SHAPES_BY_ARCHETYPE` (mountain/forested drop `rectangle`),
> `zones.ZoneWeights` + `ZONE_WEIGHTS_BY_ARCHETYPE` (forest/water signals added to the zone
> query/scoring). Helpers `weights_for`/`loop_geom_for`/`shapes_for`/`zone_weights_for`, threaded
> through `plan_routes` → `generate_candidates(loop_geom=)`, `evaluate(weights=)`,
> `find_ride_zone(archetype=)`. grid-farmland rows are built FROM the existing constants so the
> default path is byte-identical (`weights_for(None) is weights_for("grid-farmland")`), and
> grid-farmland's zone query is unchanged. Offline regression/invariant tests in
> `tests/test_weights.py`; live-verified that per-archetype zone scoring shifts the chosen
> direction. Adaptivity remains opt-in behind `--classify` (see PROJECT_CONTEXT). Non-grid rows
> are a conservative first pass (calibrate in Task 8). **Next: Task 3.**

**Goal:** replace constants currently used as universal with per-archetype tables. The
`grid-farmland` row must equal **today's exact values** (regression-safe).

**Touches:** the zone-scoring weights (`W_FARM=1.0`, `W_GRID=0.15`, `W_ART=0.4` in `zones.py`)
and the route-scoring weights (`engine.py`). Introduce `WEIGHTS_BY_ARCHETYPE` (and a shapes
table); `unknown`/missing → `grid-farmland`.

**Zone scoring per archetype** (what "good quiet riding" means):
`grid-farmland` → farmland density (current). `forested-rolling` → forest cover + low arterial
density + elevation variety. `mountain` → reward relief/scenery, low traffic, away from the
valley arterial. `coastal` → water proximity + low-stress shore roads. `suburban-sprawl` →
distance to nearest non-suburban sector (you want to *escape*). `find_ride_zone` takes the
archetype and uses its zone-scoring vector instead of the farmland-fixed one. The two None-gates
(standout test / "already in good country") generalize — just feed them the archetype score.

**Shapes per archetype:** `grid-farmland` → `loop,lollipop,rectangle` (unchanged).
Organic/mountain → drop `rectangle`, raise `_LOOP_SIDES` and `detour` so polygon-loop vertices
snap to the curvy real graph instead of cutting across nothing.

**Acceptance:**
- Grid-farmland start → **byte-identical** routes/scores to pre-change (golden-route check).
- Mountain start → rectangle absent from defaults; zone scoring visibly differs from farmland.

---

## Task 3 — Road vs. gravel asymmetry

> **STATUS: DONE (2026-06-15).** 3a: `engine.ROAD_WEIGHTS`/`GRAVEL_WEIGHTS` + `as_gravel()`;
> `w_wind` is now a RouteWeights field (road 1.0 / gravel 0.55); `weights_for(archetype, ride_type)`.
> 3b: `_gravel_seek_reward` (target band 0.5–0.75 + taper), road keeps the convex penalty, gravel
> swaps to the reward. 3c: `surface.classify_quality_tags` + `OverpassSurface.classify_quality`
> (good vs unrideable), new `Candidate.good_gravel_frac`/`unrideable_frac`, hard-avoid unrideable
> (`w_unrideable=2.5`) for BOTH ride types. `evaluate` is now fully weights-driven (no ride-type if).
> Regression-safe: grid-farmland ROAD byte-identical (gravel/quality terms 0 without OSM). Tests:
> `tests/test_weights.py` (gravel seeks / road avoids / unrideable demotes both / seek curve) +
> `tests/test_surface_quality.py`; live-verified quality grading (good→(1,0), bad→(0,1)). Display
> updated (CLI table flags, reasons, web). **Next: Task 4.**

### 3a. Two real weight profiles
Make `ROAD_WEIGHTS` and `GRAVEL_WEIGHTS` first-class vectors; ride type selects one (you already
branch on ride type for display). Key delta: `w_wind` road `1.0` / gravel `~0.5–0.6`. Road keeps
the gravel **penalty**; gravel swaps it for a **reward** (3b). `W_BUSY` high for both.

### 3b. Gravel-seeking reward (gravel rides only)
Removing the penalty isn't enough — add `W_GRAVEL_SEEK` rewarding confirmed `unpaved_frac`, with
a **target band / diminishing returns**, not linear-to-100%. Reward rising toward ~50–75% and
taper above (logistic or piecewise peaking in-band) so it still returns a sane route where the
area only offers ~30% gravel. Must be a **no-op for road rides**.

### 3c. Gravel quality grading
Extend `OverpassSurface` to expose `surface` subtype, `smoothness`, `tracktype`. Use them to
(a) reward *good* gravel (`fine_gravel`/`compacted`/`tracktype` grade2–3) for gravel rides, and
(b) **hard-avoid unrideable** (`ground`/`mud`/grade5) for **both** ride types. This also sharpens
the road penalty — a `compacted` road costs far less than a `ground` one, which the current flat
convex penalty can't distinguish.

**Acceptance:**
- Gravel ride in a gravel-rich area now routes onto majority gravel; road ride unchanged.
- A grade5/mud segment is avoided by both ride types.
- Display columns (Gravel %/Unpaved %) reflect quality buckets; road-ride scores unaffected by 3b.

---

## Task 4 — Volume-first safety/quiet (reframe, not rebuild)

> **STATUS: 4a DONE (2026-06-15); 4b deferred to Task 5.** 4a: `evaluate(busy_baseline=)` charges
> the arterial penalty only ABOVE the corridor's unavoidable level (min `busy_frac` across
> candidates), so where the quietest roads are unavoidably busy the relatively-quietest still wins.
> `plan_routes` sets the baseline only under `--classify`; default 0.0 -> absolute penalty,
> byte-identical. Note added when arterials look unavoidable. Tests in `tests/test_weights.py`
> (default==absolute, unavoidable-unpenalized, still-picks-quietest). 4b (real AADT) is an optional
> per-region provider — built on Task 5's registry, so deferred to land there. **Next: Task 5.**

**Goal:** generalize the busy penalty around traffic **volume** (the dominant risk + annoyance
signal for this rider), without importing a casual-rider stress model.

### 4a. Road class as region-normalized volume proxy
Most of this is a reframe of the existing waytype-1 / `W_BUSY=1.5` penalty: define "busy" by
**local percentile of the corridor's road-class distribution** (ties into the archetype + local
network from Tasks 1–2), not a fixed US waytype. So the quietest *available* roads still win in a
region where they aren't waytype-1.

### 4b. Real AADT where a DOT publishes it (optional upgrade)
Where a state DOT publishes Annual Average Daily Traffic, use the real count instead of the class
proxy. Implement it as an **optional provider** in the surface-provider registry (Task 5),
discovered by admin boundary. Class proxy stays the universal baseline; AADT is the per-region
upgrade and lets you separate two same-class roads with very different traffic.

**Explicitly NOT in this task** (see Deferred): speed, shoulder, lanes, width, curvature/crest
sightlines, lighting.

**Acceptance:**
- In a region whose quietest roads aren't waytype-1, the penalty still selects them.
- AADT provider (if built) overrides the proxy where data exists; baseline elsewhere.
- Home-region routes preserved.

---

## Task 5 — Surface-provider registry + graceful degradation

> **STATUS: DONE (2026-06-15).** Registry: `surface.SurfaceProvider` + `REGIONAL_SURFACE_PROVIDERS`
> (empty default) + `regional_providers_for(lat,lng)`, dispatched by `applies_to` admin boundary;
> `plan_routes` runs them after the OSM baseline (Task 4b AADT will register here). Degradation:
> `OverpassSurface.coverage()`, `PlanResult.data_confidence` (ok/low/ors-baseline) via
> `_surface_confidence` + user note; CLI bold-yellow, web in notes. Bonus hardening:
> `surface.overpass_json` mirror-fallback (overpass-api.de -> maps.mail.ru -> kumi.systems) wraps
> ALL Overpass reads (surface/regions/zones) — live-verified a default classify succeeds when the
> primary 504s. Tests in `tests/test_providers.py` (dispatch, fallback, coverage, confidence). No
> concrete regional providers shipped (mechanism is the deliverable). **Core (1–5) complete; 6–8
> are flagged upside.**

**Goal:** portability across data environments + honesty when data is thin.

**Registry:** make **OSM `surface=*` the universal baseline**; treat US-specific GIS (Indiana DOT
`LRSE_Surface_Type`, county road-commission layers) and AADT (Task 4b) as **optional providers
discovered by admin boundary**. A small dispatch — "if start ∈ Indiana, also query X" — keeps the
US wins without making them load-bearing everywhere.

**Degradation:** add `data_confidence` to `PlanResult`. Detect low tag coverage (e.g. < threshold
of corridor length has a `surface` tag) and fall back explicitly: trust ORS buckets, widen
tolerance, and **tell the user** ("limited surface data here — gravel estimates low-confidence").
Surface the flag in the CLI table and web UI.

**Acceptance:**
- A low-coverage / foreign start returns a route **plus** an explicit low-confidence flag —
  never a confidently-wrong route.
- Adding/removing a regional provider changes only that region's surface reads.

---

## Task 6 — 2-opt candidate refinement (optional, after 1–4)

> **STATUS: DONE (2026-06-15), opt-in `--refine`.** `engine.refine_candidate` hill-climbs a
> waypoint-built candidate's corners (loop/rectangle now carry `Candidate.waypoints`), re-routing
> each move via `_candidate_from_waypoints` and keeping it only if the FULL objective improves
> within tolerance. `planner._refine_candidates` supplies the `score_fn` (reuses the prebuilt
> OverpassSurface index + correction cache — no extra network) and refines the top `REFINE_TOP`(2)
> at `REFINE_CALLS_EACH`(5) ORS calls each, after `evaluate`/before `select_route_options`, then
> re-evaluates. NOT textbook edge-2-opt — public ORS has no per-edge control (that's Task 7), so the
> move is corner-nudge + re-route. Off by default → identical behaviour + zero extra calls; seed's
> own score is the refine baseline (no double-counted corrections). Tests in `tests/test_weights.py`.
> CLI `--refine`, web checkbox. **Next: Task 7 or 8 (both upside).**

**Goal:** squeeze more wind score out of the candidates you already generate, without new infra.

**Build:** a **pure** local-search refine step in `engine`, called by `planner` after
`generate_candidates` and before `select_route_options`, **off by default behind a flag** until
proven. Run on the top-K seeds. Critical subtlety: the objective is **non-additive**, so you
**cannot** accept a swap on local edge cost — **re-score the whole candidate with the full
objective after each swap**. Keep it cheap by operating on snapped geometry and only re-routing
the *changed segments* through ORS (not the whole loop). Hold target length within `--tolerance`
as a **hard constraint**; keep swaps that raise total score.

**Acceptance:**
- Refined candidate total score ≥ its seed; length stays in tolerance.
- ORS call count per plan stays bounded (don't blow the free-tier budget).
- Flag off → behavior identical to today.

---

## Task 7 — Wind-biased edges via self-hosted router (largest, last)

> **STATUS: stopgap DONE (2026-06-15); Valhalla = gated experimental seam.** Stopgap: opt-in
> `wind` shape (`engine._make_wind_loop`) rides headwind-out to a turnaround then routes home with
> ORS `avoid_polygons` over the outbound corridor (`engine._corridor_multipolygon`, a MultiPolygon
> excluding the endpoints), so the tailwind return is on different roads; `_ors_directions` gained
> `avoid_polygons`; falls back to a plain return if blocked; opt-in via `--shapes wind` (in `SHAPES`
> + `shapes_for` always-set). Full version: `windroute/valhalla.py` — `enabled()` gated on
> `WINDROUTE_VALHALLA_URL` (off by default → ORS only), wired into the `wind` outbound when enabled,
> any failure falls back to ORS. UNTESTED against a live Valhalla, and true per-edge wind biasing
> still needs a custom costing model (documented). Tests in `tests/test_wind.py`. CLI `--shapes`
> help + web shape list updated. **Remaining work-plan item: Task 8 (region-aware tuning validation).**

**Stopgap first (no new infra):** approximate wind-biased outbound on **public ORS** — route to a
wind-optimal turnaround, then force the return onto different roads with **`avoid_polygons`** over
the outbound corridor. Heuristic, but gets "headwind out + different roads home" today; slots into
the staging machinery.

**Full version:** stand up a router you control. Prefer **Valhalla** (dynamic per-request costing
+ bicycle surface/use prefs) so you can bias edge cost by the angle between edge bearing and the
headwind without recompiling. (GraphHopper custom models also work but are more static.) Note: ORS
`profile_params.weightings.quiet` is a verified no-op on the public API and exposes no per-edge
custom weights — that's why this needs a self-hosted router.

**Acceptance:**
- Stopgap produces headwind-out + different-roads-home on public ORS.
- Full Valhalla path is gated behind config and **not required** for the app to run.

---

## Task 8 — Region-aware tuning validation

**Goal:** trust the weights when the rider leaves the home region. Keep **analysis + review**
(owner deliberately deferred auto-retune — do not auto-apply weight changes).

**Build:** in `learn.py`, cluster trip history geographically and report **per-cluster** feature
profiles (so the rider profile can carry per-archetype adjustments instead of one blended set).
At plan time, if the start's archetype (Task 1) differs from the archetype most training rides
came from, surface a one-line note: *"weights tuned for grid-farmland; this looks like mountain —
results may be off."*

**Acceptance:**
- Multi-region history → per-cluster profile in the `learn` report.
- A plan in a new archetype prints the mismatch note; no weights change automatically.

---

## Captured ideas (rider-validated, not yet scheduled — WANTED, unlike Deferred below)

Surfaced from real rides the owner makes by hand; the generator under-does both today. Build when
there's room; both fit the generate-and-score frame.

- **Wind-exposure weighting (shelter vs. open).** `wind_score` weights every segment purely by
  *bearing* — blind to whether a headwind stretch is sheltered or a tailwind stretch is exposed.
  The owner deliberately routes **headwind legs through paths/urban (trees + buildings break the
  wind)** and saves **open rural/farmland for the tailwind leg (full push)**. Idea: scale the
  headwind *penalty* DOWN on sheltered land cover (urban/forest/path) and the tailwind *reward* UP
  on open land cover (farmland/exposed), using the land-cover signals we already pull (regions +
  Overpass landuse). Validated by a real Mokena WSW-wind loop (recovery.gpx, 2026-06-15):
  ~18.7 km headwind-out (sheltered), then a ~10.7 km open E tailwind run on Steger/Delaney;
  tool `wind_score` +1.47. This is the signal that ride was hand-optimizing.
- **Transit-to-good-roads (suburban escape) in DEFAULT routes.** The owner will spend real effort —
  **including riding a crosswind** — to get off suburban roads and onto good quiet farm roads.
  Mokena itself is suburban but rural country surrounds it. The `--ride-area` staging path does this
  on request, but the **default generated routes don't escape suburbia enough** — they optimize wind
  from the start even when the start is suburban. Idea: when the start archetype is `suburban-sprawl`
  (Task 1), bias default generation to reach the nearest good-riding zone (Task 2 zones already find
  it) — treat a crosswind transit to quiet roads as worth it, i.e. road-quality reachability can
  outweigh a pure wind line. Effectively: make a lightweight "escape to good roads" the default in
  suburban archetypes, not just under `--ride-area auto`. See PROJECT_CONTEXT "Owner's riding
  preferences" + "Possible next steps".

## Deferred / do-NOT-build (recorded so the session doesn't wander)

Each is plausible at the margin but **speculative for this rider/use until real riding surfaces a
concrete case**. Revisit per-case, don't pre-build.

- **Level of Traffic Stress** and any casual-rider stress model — wrong target rider.
- **Speed / shoulder / lanes / width** penalties — volume dominates for a confident road cyclist.
- **Curvature / crest sightline** penalties — left out by choice; add only if a specific road
  proves bad on the bike (then it's a tuning fix, gated to non-flat archetypes).
- **Lighting / time-of-day** stress.
- **Auto-retune** of weights from `learn` — owner chose analysis + review.
- **"AI generates the map"** — non-starter; OSM is ground truth, generative maps hallucinate roads.
- **Strava-heatmap parsing** — owner idea, explicitly "later."

---

## Regression gate (run before merging any task)

- **Golden route:** pick a fixed grid-farmland plan (e.g. the Chicago example in the context file
  with a fixed date/time/seed) and snapshot its recommendation + 2 alternatives (shape, length,
  score, key roads). Any task that isn't *explicitly* meant to change home-region behavior must
  reproduce it. Tasks 1–2 in particular must be byte-identical here.
- **Purity:** new `engine` functions take inputs and return values, no I/O. `planner.plan_routes`
  stays the only pipeline. CLI / web / Discord change only to pass through new flags.
- **Budget:** confirm ORS calls per plan stay within the free-tier envelope (~12–15 today; Task 6
  must not balloon it).

---

## Suggested order of attack for the session

Land **Task 1**, then **Task 2** (and confirm the golden route is byte-identical — this proves the
adaptivity scaffolding is regression-safe before anything else moves). Then **Task 3** (road/gravel
asymmetry — small, high-value) and **Task 4** (volume reframe — mostly a generalization of existing
code). **Task 5** whenever you next plan to ride somewhere with thin data. **Tasks 6–8** are upside,
each behind a flag, after the core feels right on real rides.
