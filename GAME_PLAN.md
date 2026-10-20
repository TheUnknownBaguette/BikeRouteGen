# BikeRouteGen — Game Plan (Oct 2026)

**What this is:** the next batch of ideas the owner and Claude brainstormed on 2026-10-08,
written so any later session can pick them up cold. Go through it together, one item at a
time: confirm the open questions with the owner, build, test, then mark the item done here.

**Read first:** `PROJECT_CONTEXT.md` (what exists, gotchas). The older
`docs/archive/ROUTE_ALGO_WORKPLAN.md` / `docs/archive/CODE_HEALTH_WORKPLAN.md` are finished and only historical.

---

## 0. Ground rules (carry these into every session)

- **No password logins, no new accounts.** Claude can't create accounts or type passwords.
  Integrations use API keys/tokens the owner creates and can revoke. (The owner offered
  shared Ride with GPS and Strava logins; that's declined for this reason. It isn't needed.)
- **No Strava scraping.** Strava's terms ban automated access outside its API, and heatmap
  data is licensed only for tracing into OpenStreetMap. Use the real API (item 6) or other
  data instead.
- **The repo is public.** Keys go in env vars / Render secrets, never in files. The owner's
  ride history (item 3) is personal location data: never commit it.
- **Front-ends stay thin.** New logic goes in `windroute/` and is reached through
  `planner.plan_routes`; the CLI and web app only pass inputs through and present results.
- **Don't regress the defaults.** With a new signal switched off (or its data missing), the
  ranking must be what it is today. Keep the steady-wind equivalence test passing.
- **Workflow the owner likes:**
  - They test on the **hosted Render site** (deployed from `main`), not locally.
  - Ask before committing; they say "commit and push" when ready. Commits end with the
    `Co-Authored-By` line.
  - Explain choices in plain language and give a recommendation.

### Environment cheat sheet (this PC)

- Project: `C:\Users\gcook\OneDrive\Gus' School Folder\Code\claude\BikeRouteGen`; the venv is
  `.venv` (rebuilt on this PC in Oct 2026). `run.bat` rebuilds it on other machines.
- Tests: `.venv/Scripts/python.exe -m pytest -q` (184 passing as of 2026-10-19, all offline).
- Local preview from Claude Code: the `webapp-c` entry in the parent folder's
  `.claude/launch.json` (waitress on port 5057). Restart it after Python/template changes.
- `ORS_API_KEY` is set in the Windows user environment. ORS now lives at
  `api.heigit.org/openrouteservice` (see PROJECT_CONTEXT gotchas). The free plan is 2,000
  directions requests/day and 40/minute; one plan uses about 8-15.
- Tooling quirk: long `python - <<'EOF'` heredocs in the Bash tool sometimes fail to parse.
  Use the Edit tool, or write a patch script to the scratchpad and run it.

---

## Suggested order

| # | Item | Size | Why this order |
|---|------|------|----------------|
| 1 | ORS bike-suitability score | small | Free data on the call we already make; quick win |
| 2 | Send routes to Ride with GPS | small-medium | Removes the manual GPX import step |
| 3 | Personal heatmap from ride history | medium | Biggest routing win for this rider |
| 4 | Real traffic counts (IDOT/INDOT AADT) | medium | Turns "busy" from a guess into car counts |
| 5 | "Fully routable" checks | small-medium | Needs an example bad route from the owner first |
| 6 | Strava segment popularity (optional) | medium | Thin signal, tight limits; only if 3-4 fall short |
| 7 | Ride with GPS OAuth for friends (later) | large | Only if other people start using the site |
| 8 | Club version (start points + known exits) | medium | Owner's next priority (2026-10-19) |

Smaller leftovers already listed in PROJECT_CONTEXT "Possible next steps" (do any time):
default the start box to the most recent start; fit `WIND_SPEED_EFFECT` (0.25) to ride
history; wind-exposure weighting (shelter vs. open); suburban-escape by default.

**Code-health ideas saved for later** (from the Oct 12 review; owner said "later"). None
change behavior:
- `routing._ors_directions` returns a bare 10-value tuple; a NamedTuple would make the
  call sites (builders, `cli mark`, `roads-import`) readable.
- `_make_lollipop` and `_make_staging` repeat the same distance-weighted blending; share
  one helper.
- `OverpassSurface` has four near-identical `_nearest_*` lookups; fold into one.
- 10 older test files still carry `_run()` runners and `sys.path` inserts pytest doesn't
  need (the newer files don't).
- CI tests only Python 3.11 and Render's Python isn't pinned: add a `.python-version` and
  test that version too.
- `discord_bot.py` is untested and unused: keep as an example or drop it.

---

## 1. ORS bike-suitability score

**Goal:** penalize roads OpenRouteService itself rates as poor for bikes.

**What we know (tested 2026-10-08):**
- Adding `"suitability"` to `extra_info` in the directions body returns a per-segment
  0-10 rating (10 = best), on the same request. No extra calls or quota.
- On a Mokena test route it rated 97.6% of the distance an 8, 2.2% a 7, 0.2% a 6.
- `"roadaccessrestrictions"` was also requested but didn't come back. Either the route had
  none or the cycling profile doesn't support it; check before relying on it (see item 5).

**Plan:**
1. `routing._ors_directions`: add `"suitability"` to `extra_info`; compute a
   distance-weighted mean and a "poor share" (fraction with suitability ≤ some threshold,
   e.g. ≤ 5). Return them alongside busy/path like the other extras.
2. `models.Candidate`: new fields (e.g. `suitability_mean`, `poor_suit_frac`), default so
   nothing changes when absent.
3. `scoring.RouteWeights`: new weight (start small) on `poor_suit_frac` beyond a free band.
   Show it in the candidate table / cards only if it matters.
4. Calibrate: run a few real plans around Mokena and see what the values look like on the
   roads the owner likes vs. avoids before choosing the threshold and weight.

**Questions for the owner:** none needed to start. Show them a before/after on a real plan.

**Done when:** tests cover the parsing and the scoring term; a real plan shows the field;
default-weight ranking only changes where suitability is clearly poor.

---

## 2. Send routes to Ride with GPS

**Goal:** one click puts a planned route into the owner's Ride with GPS account (they use
Ride with GPS for sharing routes).

**API facts (from the official OpenAPI spec, https://ridewithgps.com/api/v1/openapi.yaml):**
- `POST /api/v1/routes.json` creates routes from an uploaded file. **multipart/form-data
  only**, fields: `file` (GPX/TCX/KML/FIT, required), `name`, `description`.
- Auth: the same `api_key` + `auth_token` the existing `windroute/rwgps.py` client uses
  (creds in `~/.windroute/rwgps.json`, set via `cli rwgps-login`). No OAuth needed for the
  owner's own account.
- It's async. It returns **202** with a `task` (`status: "pending"`) and a `Location` header.
  Poll `GET /api/v1/tasks/{id}.json` until `status == "completed"`, then read `items`
  (`item_type`, `item_id`, `item_url`) and `errors` (machine-readable `code`).
- **Visibility can't be set on upload**; it follows the account's default route privacy.
  `PUT /api/v1/routes/{id}.json` can change `name`, `description`, `visibility`
  (`public`, `followers_only`, `mutual_followers_only`, `private`), `activity_types`,
  `archived` afterwards (added 2026-10-06). The track can't be edited.
- **Unknown:** whether an uploaded route gets a turn-by-turn cue sheet. Route exports
  (TCX/FIT) carry cues, so routes *can* have them; test one upload and look.
- Ride with GPS added API Terms of Service on 2026-09-11; skim them before shipping.

**Options discussed:**
- **A (recommended): "Send to Ride with GPS" for the owner.** Store the owner's
  api_key/auth_token as Render secrets (e.g. `RWGPS_API_KEY`, `RWGPS_AUTH_TOKEN`). Add a
  button per route card. Because the hosted site is public, gate it with a passphrase only
  the owner knows (e.g. a `RWGPS_SEND_PASSPHRASE` secret, entered once and remembered in the
  browser), or anyone could fill the account.
- **C (almost free with A): CLI `plan --to-rwgps`** using the local creds file.
- **B (later, item 7): OAuth** so each visitor connects their own account.

**Plan (A + C):**
1. `rwgps.py`: `upload_route(api_key, auth_token, gpx_bytes, name, description)` → task;
   `wait_for_task(task_id, timeout)` → created route id/url or `RwgpsError` with the codes;
   optional `update_route(id, visibility=...)`. Multipart via `requests` `files=`.
2. Naming: reuse `render.route_basename`-style names, e.g.
   "Oct 8 · 30 mi loop · NW wind · into wind first", and put the wind summary + ride time
   in the description.
3. Web: a server endpoint (POST, passphrase-checked, rate-limited) that takes a route's
   GPX token (the GPX is already written to `static/out/`) and uploads it; the card shows a
   link to the new Ride with GPS route when it's done. Shared-link pages (`/share`) don't
   get the button (no server-side GPX there).
4. CLI: `--to-rwgps` flag on `plan` that uploads the recommended route (or all three).
5. Tests: stub `requests` like `tests/test_ors_access.py` does (202 → poll → completed).

**Questions for the owner:**
- Visibility for these routes: private, followers-only, or public?
- Upload just the route they pick, or all three options?
- Passphrase approach OK for the hosted site?

---

## 3. Personal heatmap from ride history: SHELVED (2026-10-08)

> **Owner decision:** not now. The goal is a planner that works well *anywhere*, not just
> where the owner rides, so signals trained on one person's history are out. Prefer
> signals with broad coverage (ORS, OSM) over region- or person-specific ones.

**Goal:** prefer roads the owner has actually ridden: a personal, legitimate stand-in for a
Strava heatmap. These roads are both "where a good rider goes" and proven rideable.

**What we have:** 200 newest Ride with GPS trips cached in `~/.windroute/trips/`
(`rwgps.load_cached_trips`, `rwgps.parse_track_points`), 108 of them outdoor rides. The
scorer already has a road-cell helper: `scoring._route_cells` rounds lat/lng to 3 decimals
(~100 m cells), as used for route overlap.

**Plan:**
1. Build: walk every outdoor trip, collect the cells each passes through (count once per
   trip, so one long ride doesn't dominate), save `{cell: rides}` to
   `~/.windroute/heatmap.json`. New CLI command, e.g. `heatmap-build`; rebuild after `import`.
2. Score: `Candidate.ridden_frac` = share of a route's cells ridden ≥ N times; a small bonus
   weight (off when no heatmap exists, so defaults don't change). Maybe a mild penalty for
   long stretches of never-ridden road only *near home*, where the heatmap is dense.
3. Show it: a stat chip ("72% roads you ride"), and optionally the heatmap as a faint map
   layer for the owner.
4. **Hosted site:** the trips live on the owner's PC, not on Render. Options: upload the
   heatmap file as a Render secret file, or pack it (compressed) into an env var. **Never
   commit it** (it reveals where the owner lives and rides). Without it the hosted site
   simply skips the signal.

**Watch out:** it only knows where the owner has been, so it shouldn't punish exploring new
areas. Keep the bonus modest and the penalty (if any) local.

**Questions for the owner:**
- Should routes lean on familiar roads, or is some exploration wanted? (sets the weight)
- OK to put the heatmap on Render as a secret file?

---

## 4. Real traffic counts (AADT)

> **Reworked 2026-10-08 (owner: must work anywhere):** built from OSM `maxspeed` / `lanes`
> instead of Illinois/Indiana traffic counts. The AADT notes below are kept as an optional
> future add-on.

**Goal:** replace "busy = road class" with actual cars per day, e.g. avoid roads over
~5,000 vehicles/day. This was "Task 4b" in the old route-algo plan.

**Sources found (2026-10-08):**
- Illinois DOT AADT, open data via ArcGIS Hub (2020, 2024, 2025 versions). Catalog record:
  https://geo.btaa.org/catalog/2e3ac25944f245eea3cc542527fa9a27_2025.xml
- Indiana DOT `LRSE_AADT` FeatureServer:
  https://gis.indot.in.gov/ro/rest/services/RAH_GIO_Collaboration/LRSE_AADT/FeatureServer/33
- Still to do: find the exact Illinois FeatureServer URL and field names, and check how many
  *local* roads have counts (state roads are covered well; township roads may not be).

**Plan:**
1. A provider in `surface.py`'s registry pattern (`SurfaceProvider` /
   `regional_providers_for`): query the ArcGIS layer for the route's bounding box (one
   request per plan, cached per area), match segments to the route by proximity, and compute
   the share of distance on roads above a threshold.
2. Feed it into the existing busy penalty (`busy_frac`) where counts exist; fall back to
   road class where they don't. Only active inside Illinois/Indiana.
3. Calibrate the threshold against roads the owner knows are busy vs. fine.

**Questions for the owner:** what traffic level feels "too busy" (a couple of example
roads helps calibrate)?

---

## 5. "Fully routable" routes

**Goal:** never route over roads that can't really be ridden: private or gated roads,
closed roads, roads that don't connect, fords, etc.

**First step:** ask the owner for a concrete bad route (screenshot, GPX, or a share link).
The fix depends on which failure it is.

**Likely fixes, once the cause is known:**
- Penalize OSM `access=private|no|destination` (and maybe `highway=service`) using the
  Overpass reads `surface.py` already does, or ORS `roadaccessrestrictions` if the cycling
  profile returns it (see item 1).
- Ask ORS to avoid fords/ferries/steps with the `options.avoid_features` field.
- Item 1's suitability score and item 3's heatmap both help here too.
- Spurs where a waypoint snapped onto the wrong road: `routing._strip_backtracks` already
  trims some; a bad example would show whether it needs more.
- The owner's correction cache (`cli mark` / `roads-import`) is the manual escape hatch for
  a specific bad road.

---

## 6. Strava segment popularity (optional)

**Goal:** a legitimate "where do riders go" signal from Strava's actual API.

- `GET /api/v3/segments/explore` with `bounds` and `activity_type=riding` returns the most
  popular segments (up to ~10) in that box, with polylines. Tile the area to cover more.
- Needs the owner to create a Strava API application (OAuth); no password sharing.
- Rate limits are tight (check the current numbers in Strava's docs), and Strava's API
  agreement restricts how data can be used and shown to others, so read it before showing
  it on the public site.
- Only worth it if items 3 and 4 still leave the routes on roads the owner wouldn't pick.

---

## 8. Club version: set start points + known routes out of town

**Goal:** a second site for the owner's (public) club: pick one of a few club start
points in one town, and routes leave town on the club's known exit roads, then loop on
the wind like the main site. Same algorithm; every algorithm change reaches both.

**Decided (2026-10-19):**
- **One codebase, "club mode"** (option A). A club config (club name, start points,
  exit routes) turns it on; a second Render service runs the same repo with a setting
  pointing at that config. No fork, no second copy of the planner.
- **Public is fine:** the club is public, so its config and routes can live in the repo
  (e.g. `club/`).
- **Exit routes come from the club's existing route library** (GPX/TCX the owner
  downloads). Plan: find the stretches the club's rides share leaving each start (the
  common way out of town) and use those as the fixed exits, rather than hand-drawing them.
- **Coming home:** the same exit back is fine, but not required; the scorer may pick a
  different known exit for the return.
- **Access:** open to anyone with the link (same hardening and rate limits as the main site).
- **Routing key:** a second free ORS key for the club service (its own 2,000/day), as
  `ORS_API_KEY` on that Render service.

**How it fits the code:** the `staging` shape already does "fixed leg out -> wind loop
-> leg back". Club mode swaps the generated stem for a known exit, built from each exit's
end point; the loop, scoring and options are unchanged. Front-end: the start box becomes
a short list of club starts. Planner stays the single pipeline (`plan_routes` gets the
club starts/exits as inputs).

**Club data (2026-10-19):** 70 TCX files in `club/routes/` (Champaign-Urbana; one
duplicate, `Buck_s_Pond (1)`). Two starts: **Neutral Cycle** (624 S Fifth St,
40.1107,-88.2320; 45 routes) and **CBC** = Campus Bike Center (E Gregory Dr,
40.1040,-88.2402; 19 routes). Three one-off starts are ignored. Clustering where the rides
cross ~4.5 km out found 10 exits (W 27, S via First St 26, NE 15, SE 14, E 9, N 8, WNW 6,
NNE 5, SSE 4, E-2 4). Owner: ride counts don't reflect how often each is used; exit 10 is
mostly a way HOME; exit 9 is rarely used now; the club has no names for them (name them
by main road); end each exit where the rides spread out (open country), not at a fixed
circle. The files are mainly there to show which roads are good for getting out of town.

**Built (2026-10-19), owner's choice: a strong "club roads" preference** (not fixed exits),
so routes can still change things up. See PROJECT_CONTEXT "Club mode" for how it works.

**Still to do:** the club's display name (set: "Illini Cycling");
the owner reviews the exits `club-build` found (disable rarely-used ones, mark way-home-only
ones) (done: Philo Rd off); host it (decided 2026-10-19: same Render service,
`CLUB_HOSTS=routes.illinicycling.org` via a CNAME on WordPress.com DNS, an uptime pinger
so it never sleeps, and a WordPress menu link since the Personal plan strips iframes);
tune `club_road_weight` (0.5) after a few real club plans.

---

## 7. Ride with GPS OAuth for friends (later)

If friends start using the hosted site, let each visitor "Connect Ride with GPS" via OAuth
so routes go to *their* accounts. That means registering an OAuth client with Ride with GPS,
a sign-in flow, storing per-user tokens securely (the app currently has no user accounts or
database), and handling token refresh. Revisit when there's demand.

---

## Status log

| Date | Item | Status / notes |
|------|------|----------------|
| 2026-10-08 | all | Plan written; nothing started |
| 2026-10-08 | 2 | **Built.** `rwgps.upload_route` / `wait_for_task` / `send_route`; web `POST /rwgps/send` (passphrase, rate limit, one upload per GPX); card button + `static/rwgps.js`; CLI `plan --to-rwgps best\|all`; `tests/test_rwgps_send.py`. Owner's answers: one button per route; uploads go to a separate projects account and are set **public** (owner copies/sends them from their personal account); passphrase optional and left unset. Not yet tried against the live API: needs the projects account's key + token as Render secrets, then check the cue sheet question on the first real upload |
| 2026-10-08 | 2 | **Live and verified** on the hosted site (owner sent a route; works). **Uploads get no turn-by-turn cues** (a plain GPX track carries none) |
| 2026-10-08 | 2 | **Cues added** (owner picked: build them from ORS road names). ORS calls now ask for steps; `Candidate.road_names` is carried through stitching/reversal; `windroute/cues.py` makes cues on the final line + writes a TCX (CoursePoints); the send button uploads the `.tcx` twin. Needs one hosted test upload to confirm Ride with GPS shows the cue sheet |
| 2026-10-08 | 2 | Cue sheet confirmed on the hosted site. **Item 2 done.** |
| 2026-10-08 | 1 | **Built.** `suitability` extra on every ORS call; `Candidate.poor_road_frac` = share rated <=5 that isn't a State Road (calibration: arterials are 5, streets 8, paths 9, rural county roads 7 = fine); `W_POOR=1.0` beyond a 2% free band; cards/table "busy road" % = busy + poor. Before/after: top picks unchanged in Mokena/Oak Park/Naperville, one arterial-heavy route dropped 2 places. Also: a dropped ORS connection no longer kills the plan, and a 429 now says "wait a minute" |
| 2026-10-08 | 5 | **Built** (no owner example needed: measured real plans instead). Found 1-3 spurs per lollipop and some loops/rectangles: in-and-back pokes at the stem/candy join, dead ends with turning circles, U-turns on divided roads (Roosevelt, Mannheim, Pershing), up to ~1.3 km each route. `routing._trim_spurs` (distance-paired, 50 m sides, >= 60 m long) cuts them on every shape but out-and-back. ORS now avoids ferries/steps/fords. ORS doesn't return access restrictions for bikes, but its bike profiles already skip private/no-access roads. Cue fixes: hairpins onto another road are "sharp left/right", not "turn around"; no cues in the first/last 30 m |
| 2026-10-08 | 4 | **Built (OSM version).** `surface.FastRoads`: one Overpass read per plan (12 s query, 30 s cap across mirrors; on failure the plan carries on with a note). A stretch counts when the route is ON a road with maxspeed >= 45 mph or 4+ lanes (route road name must match the OSM name/ref, so side paths don't count). `Candidate.fast_road_frac` keeps only the share beyond busy + poor; `W_FAST=1.0` beyond 2%. Calibration: Gougar Rd (55 mph, ORS suitability 7) was the main gap near Mokena. Before/after: Mokena's #2 (6% fast) fell to #4; Naperville's #1 (10% fast) fell to #2. Lookup took ~3 s |
| 2026-10-08 | 5 | Owner's bad/good GPX pair (Urbana, north of the Boneyard): the bad route takes unnamed `highway=service` ways off N Fourth St north of Bradley (OSM #1317157631-34, #1346949062), then N Oak St north of W Anthony Dr (#5338926) and Somer Dr (#5330479, #882135052, #882135051) to Lincoln Ave. OSM tags them as ordinary roads (TIGER import, `tiger:reviewed=no`), ORS suitability rates N Oak 8/10, and `cycling-regular` (our road profile) picks this exact line (`cycling-road` doesn't). No general tag separates them from good unreviewed rural roads, so the fix chosen is **owner edits OSM**. Not built: an avoid-list via ORS `avoid_polygons`, or a penalty on long unnamed service-road runs |
| 2026-10-08 | 3 | **Shelved** by owner: wants the planner to work in as many places as possible, not tuned to their own riding |
| 2026-10-09 | suburban escape (leftover) | **Investigated, on the backburner.** `zones.find_ride_zone` is too slow for every plan (15-52 s: Mokena 44, Oak Park 52, Naperville 15) and its zones sit ~12-13 km out, past the staging cap (0.3 x target) for rides under ~26 mi. From Mokena it pointed WNW (Homer Glen), not the farm country S/E: it counts farmland *polygons*, not area. Default candidates are all aimed within +/-60 deg of the wind (`_BEARING_OFFSETS`, 4 per shape), and the scorer has no rural-vs-suburban signal (ORS waytype Road/Street doesn't separate them). Recommended when resumed: add farmland/residential landuse to the background fast-roads Overpass read, score open-country share per route, and aim a few candidates crosswind/away; show before/after for Mokena, Naperville, Champaign first |
| 2026-10-12 | 4 (follow-up) | **Numbered routes.** Owner's Oct 11 ride vs the plan: rode CR 400 E instead of 2 mi of IL 47 (55 mph), which only cost ~-0.024 because ORS rated it 'poor' like Duncan Rd and didn't call it a State Road. Now OSM `ref` tiers: US routes avoided everywhere (2.0, 1% free), state routes 1.5 (2% free), state routes in town (<= 35 mph) 1.0 (5% free). Each stretch charged once (`RoadName` + `planner._set_traffic`). Rescored Oct 11: IL 47 costs planned -0.088 vs ridden -0.002. Adds ~5 s server time / ~19% more data to the background Overpass read |
| 2026-10-12 | turns + bike markings | **Built.** Turn penalty (0.1 per turn/mi beyond 1/mi) and small bonuses: sharrows anywhere, painted lanes only on busy roads (and those stretches no longer count as 'poor'). Replaces the old 0.6 any-lane bonus. Top picks unchanged on re-rank; less-turny routes move up. Not solved: ORS still never routes along John St on its own (public ORS can't be reweighted); options if wanted: route the stem through waypoints on marked streets, or self-host a router |
| 2026-10-12 | reliable road lookup | **Built.** Root cause of today's 'speed limits still loading': Overpass refused the query for declaring the default 512 MB; declaring 32 MB got it in within ~2 s. Also: partial answers no longer cached as empty, failed mirrors go last, hedged second mirror after 6 s, in-flight joining, and the plan form prefetches the lookup. Champaign plan: 5.6 s with full road data |
| 2026-10-12 | code-health pass | **Done (behavior-neutral; scores byte-identical vs before).** Removed dead code (`scoring.explain`, `Candidate.surface_score`, `generate_candidates(sleep=)`, unused imports/constants, duplicate `_haversine_km`). Safety: web form choice fields whitelisted, GPX names XML-escaped, share links can't inject HTML (tooltip/color), failed region lookups no longer cached forever, thread-safe suggest cache, CLI password prompted hidden. One User-Agent with a contact link for every outside service. Privacy page now covers Photon, map tiles, browser storage and public Ride with GPS uploads. Dependency floors raised to patched releases; CI actions bumped. `tests/test_hardening.py`. **Open for the owner:** rate limit trusts a spoofable `X-Forwarded-For` on Render; the 100 km cap is a leftover of ORS round trips (directions allow far more); archive the two finished workplans; `_smoothed_ascent` treats elevation <= 0 m as missing data |
| 2026-10-12 | crossing counter | **Fixed.** Shared points / shared stretches only count as a crossing when the second pass goes from one side of the first to the other. Oct 11 ridden route: 11 -> 1 (it lost 0.04 to this). Owner: keep wind scoring and wind-led labels on calm days (a small tailwind still matters) |
| 2026-10-12 | owner decisions from the review | **Done.** (1) Per-IP limit raised to 30 plans / 5 min (env `RATE_LIMIT_PLANS`) plus a site-wide cap of 1,800 ORS calls per rolling 24 h (env `SITE_ORS_PER_DAY`), which a faked `X-Forwarded-For` can't get around; past it the form says to try later instead of failing. (2) Ride cap 100 -> 250 km (`routing.MAX_RIDE_KM`), checked before any lookup; wind window +16 h. (3) Finished workplans moved to `docs/archive/`. (4) Climb counts ground below sea level (nodata is now exactly 0.0 or < -500 m). Tests: `tests/test_limits_and_climb.py`; 172 passing |
| 2026-10-19 | 8 club mode | **Built.** Club roads bonus (`w_club` 0.5, in-town share) + `club loop` candidates through the club's exits (found from 69 club rides; 10 exits). Start picker (Neutral Cycle / CBC), "N% club roads" on cards, `cli club-build`. Also: ORS waypoints may snap 1.5 km (corners in farm country were silently failing). `tests/test_club.py`; 184 passing |
