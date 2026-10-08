# BikeRouteGen — Game Plan (Oct 2026)

**What this is:** the next batch of ideas the owner and Claude brainstormed on 2026-10-08,
written so any later session can pick them up cold. Go through it together, one item at a
time: confirm the open questions with the owner, build, test, then mark the item done here.

**Read first:** `PROJECT_CONTEXT.md` (what exists, gotchas). The older
`ROUTE_ALGO_WORKPLAN.md` / `CODE_HEALTH_WORKPLAN.md` are finished and only historical.

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
- Tests: `.venv/Scripts/python.exe -m pytest -q` (98 passing as of 2026-10-08, all offline).
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

Smaller leftovers already listed in PROJECT_CONTEXT "Possible next steps" (do any time):
default the start box to the most recent start; fit `WIND_SPEED_EFFECT` (0.25) to ride
history; wind-exposure weighting (shelter vs. open); suburban-escape by default.

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
| 2026-10-08 | 5 | Owner's bad/good GPX pair (Urbana, north of the Boneyard): the bad route takes unnamed `highway=service` ways off N Fourth St north of Bradley (OSM #1317157631-34, #1346949062), then N Oak St north of W Anthony Dr (#5338926) and Somer Dr (#5330479, #882135052, #882135051) to Lincoln Ave. OSM tags them as ordinary roads (TIGER import, `tiger:reviewed=no`), ORS suitability rates N Oak 8/10, and `cycling-regular` (our road profile) picks this exact line (`cycling-road` doesn't). No general tag separates them from good unreviewed rural roads, so the fix chosen is **owner edits OSM**. Not built: an avoid-list via ORS `avoid_polygons`, or a penalty on long unnamed service-road runs |
| 2026-10-08 | 3 | **Shelved** by owner: wants the planner to work in as many places as possible, not tuned to their own riding |
