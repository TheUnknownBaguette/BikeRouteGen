# windroute

Generate **wind-smart cycling routes** from a few inputs — start point, distance,
ride time, ride type — and get back a **recommended route plus two alternatives**
(labelled map images + GPX files) you import into Ride with GPS.

The point of the *start time* input: it pulls the wind forecast for the actual
hour you'll be riding, then ranks candidate routes so you head **out into the
wind while fresh and get the tailwind home**.

## What it does

1. Geocodes your start (a town, a full street address, or exact `lat,lng` — even
   Google-Maps degrees-minutes-seconds like `41°31'36.3"N 87°52'18.0"W`).
2. Pulls hourly wind for your start time (Open-Meteo, free, no key).
3. Generates candidate routes of your target distance (OpenRouteService) in
   several shapes — loop, lollipop, wind-aligned rectangle, optional out-and-back.
   Loops are built as clean geometric polygons through the road grid (no tangled,
   spurry routing).
4. Scores each on wind (into-wind first half, tailwind home), surface (avoid
   gravel on road rides, seek it on gravel rides), busy-highway avoidance,
   bike-lane bonus / multiuse-path penalty, tidiness, and distance, then writes a
   **recommendation plus two alternatives** — each leading on a different benefit
   (stronger wind line, quieter roads, more bike lane, or a closer distance) and a
   genuinely different ride. Files: `route.png/.gpx` (pick), `route-alt1.*`,
   `route-alt2.*`.
5. Optionally **stages** the ride to quieter country (`--ride-area`): transit to
   the nearest good quiet riding zone, loop on the wind there, ride home.

---

## First-time setup (PowerShell)

Run these once. They assume you're in the project folder.

```powershell
# 1. Go to the project
cd "C:\Users\gcook\OneDrive\Gus' School Folder\Code\claude\BikeRouteGen"

# 2. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
#    If activation is blocked by execution policy, allow it for your user once:
#    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# 3. Install dependencies
pip install -r requirements.txt

# 4. Get a free OpenRouteService key (one-time, in a browser):
#    https://openrouteservice.org/dev/#/signup
#    The free plan covers round trips up to 100 km.

# 5. Save the key permanently to your Windows user environment
[Environment]::SetEnvironmentVariable("ORS_API_KEY", "PASTE_YOUR_KEY_HERE", "User")
```

The key is now stored for your Windows user, so **every new PowerShell window
picks it up automatically** — you don't set it again.

> If you set the key in a terminal that was *already open*, that one session
> won't see it yet. Either open a fresh terminal, or load it into the current
> one with:
> ```powershell
> $env:ORS_API_KEY = [Environment]::GetEnvironmentVariable("ORS_API_KEY","User")
> ```

---

## Easiest: the local web app (no terminal)

After the one-time setup above, you can skip PowerShell entirely:

**Double-click `run.bat`.** It starts a local server and opens your browser to
`http://127.0.0.1:5000`. Fill in the form (start point, distance, time, ride type —
plus an "advanced" block for shapes, surface source, ride-area staging), hit **Plan
my routes**, and you get the recommended route plus two alternatives, each with its
map shown inline and a GPX download button. A plan takes ~20–40 s (it's calling the
routing + wind services, same as the CLI).

It runs only on your own machine (`127.0.0.1`, not exposed to your network) and reads
`ORS_API_KEY` just like the CLI. To stop it, close the terminal window it opened.

> Same engine underneath — the web app and the CLI both call `windroute.planner`, so
> they always agree.

## Share it with friends (free hosting)

Host the web app once and your friends just open a link — no install, no API key,
no commands on their end. Your key stays a server-side secret.

On a free host like [Render](https://render.com):

1. Make sure your code is pushed to GitHub.
2. Create a new **Web Service** and connect this repo.
3. **Build command:** `pip install -r requirements.txt`
   **Start command:** `waitress-serve --listen=*:$PORT webapp:app`
4. Add an environment variable **`ORS_API_KEY`** = your key (mark it secret).
5. Deploy, then share the service URL.

`waitress` (in `requirements.txt`) is the production server; `webapp.py` reads the
host/port from the environment, so nothing in the code changes between local and
hosted. The same `waitress-serve --listen=*:$PORT webapp:app` runs on your own
server later (see below).

> **Never commit your API key** — the repo is public, so a committed key gets
> scraped and revoked. Keep it in the host's secret env var only.
>
> **Limits:** the OpenRouteService free tier is ~2,000 routing calls/day and one
> plan uses ~12–15, so it's good for a handful of friends (~130 plans/day shared),
> not a public launch. Free hosts also sleep after idle, so the first request after
> a quiet spell can take ~a minute to wake.

### Self-hosting later (your own box)

When you have your own server, run the exact same command there — `waitress` is
cross-platform (Windows/Linux/Pi):

```bash
ORS_API_KEY=your_key  PORT=5000  HOST=0.0.0.0  waitress-serve --listen=*:5000 webapp:app
```

Set `ORS_API_KEY` in that machine's environment and open the port; no code changes.

## Everyday use (PowerShell)

Prefer the terminal? Once setup is done, a normal session is just two steps:

```powershell
# 1. Activate the environment
cd "C:\Users\gcook\OneDrive\Gus' School Folder\Code\claude\BikeRouteGen"
.\.venv\Scripts\Activate.ps1

# 2. Plan a ride
python -m windroute.cli plan -l "Mokena, IL" -d 30 -s "2026-06-14 08:00" -r road
```

You'll get a ranked table in the terminal, then three route options: the
recommendation as `route.png` / `route.gpx`, plus two alternatives as
`route-alt1.*` and `route-alt2.*`. Import whichever GPX you like into Ride with
GPS to refine.

### More examples

```powershell
# Start from an exact address or coordinates (most precise — use this for a
# specific bike-path access point a town centroid would miss)
python -m windroute.cli plan -l "41.52675,-87.87167" -d 25 -s now -r road

# Gravel ride, distance in miles, cross-check surface against OpenStreetMap
python -m windroute.cli plan -l "Champaign, IL" -d 40 --unit mi -r gravel --surface-source both

# Auto-detect the good quiet riding zone and stage the ride to it
python -m windroute.cli plan -l "Mokena, IL" -d 40 --unit mi --ride-area auto
```

### `plan` options

| Option | Meaning |
| --- | --- |
| `-l, --location` | Town (`"Mokena, IL"`), street address, or `lat,lng` / DMS coords. |
| `-d, --distance` | Target ride distance (total, including any transit). |
| `--unit` | `mi` (default) or `km`. |
| `-t, --tolerance` | Free +/- distance buffer; only distance beyond it is penalized (default 3). |
| `-s, --start` | `"YYYY-MM-DD HH:MM"` or `now`. Sets the wind forecast hour. |
| `-r, --ride-type` | `road` (avoid gravel) or `gravel` (seek it). |
| `--shapes` | Comma list: `loop,lollipop,rectangle,out-and-back,roundtrip` (default `loop,lollipop,rectangle`). `roundtrip` is the old ORS round-trip algorithm (can tangle; opt-in). |
| `--surface-source` | `ors` (default), `osm` (finer OSM tags + bike lanes), or `both` (cross-check). |
| `--ride-area` | `auto` to stage to the nearest quiet zone, or a place / `lat,lng` to force one. Omit for a normal ride from the start. |
| `--corrections / --no-corrections` | Apply your personal "I rode this" cache (on by default). |
| `--candidates` | How many routes to generate and rank (default 12; more = better odds, slower, more API calls). |
| `-o, --out` | Output file basename (default `route`). |
| `--api-key` | ORS key override (normally read from `ORS_API_KEY`). |

### Personal "I rode this" corrections

Teach the tool ground truth it can't see — a road that's really gravel, or really
busy — and it's applied on top of the surface data on every future plan.

```powershell
# Mark a road you routed by its endpoints as unpaved and quiet
python -m windroute.cli mark --between "New Buffalo, MI" --to "Three Oaks, MI" --surface unpaved --traffic quiet

# Mark from a recorded ride, or from points you trace
python -m windroute.cli mark --gpx "ride.gpx" --surface unpaved
python -m windroute.cli mark --point "41.79,-86.74" --point "41.80,-86.75" --traffic busy

python -m windroute.cli corrections        # list what's on file
python -m windroute.cli forget 2           # delete by number or label
```

**Bulk notes from a text file.** Instead of one `mark` at a time, keep a plain-text
list of roads and import it in one go:

```powershell
# Creates road-notes.txt (a commented template) if it doesn't exist yet
python -m windroute.cli roads-import road-notes.txt
```

Edit the file — one road per line, `<tags>: <A> -> <B>` — then run it again:

```text
# road-notes.txt
gravel: Manhattan, IL -> Symerton, IL
busy:   41.605,-87.861 -> 41.585,-87.861
gravel, quiet: 19150 88th Ave, Mokena, IL -> Frankfort, IL
```

Tags are `gravel`, `paved`, `busy`, `quiet` (combine with commas). Each line's two
endpoints are geocoded and the road between them is traced into your correction
cache. Pick endpoints *on* the road (addresses or `lat,lng` pins are most reliable).
Re-running re-syncs the file (replaces its earlier import); pass `--append` to keep
old entries.

---

## Honest limitations

- **Traffic** isn't a clean free data layer. Routes do penalize time on arterial
  "State Road" class (US-highways) via ORS waytype, and you can mark busy roads
  yourself, but there's no per-road car-traffic feed. Eyeball + correct as you go.
- **Gravel** relies on OpenStreetMap `surface` tags, which are incomplete. Treat
  the gravel % as a hint; `--surface-source osm`, `both`, and your correction
  cache are how you inject reality. Street View still wins for surface truth.
- **Wind optimization** ranks a handful of generated routes rather than searching
  exhaustively. More `--candidates` improves the odds at the cost of speed/API calls.
- **Ride-area staging** uses open farmland as its "quiet country" proxy, so it
  shines in grid/cornfield regions and is weaker where good riding isn't farmland.

## Project layout

```
windroute/
  engine.py       core: geocode, wind, route generation + shapes, scoring (no I/O)
  planner.py      the shared planning pipeline both front-ends call (plan_routes)
  zones.py        auto-detect the nearest quiet riding zone (for --ride-area)
  surface.py      OpenStreetMap/Overpass surface + bike-lane source
  corrections.py  the personal "I rode this" correction cache + road-notes parser
  render.py       map image + GPX output
  cli.py          the CLI wrapper (typer + rich)
webapp.py         the local web front-end (Flask) + templates/
run.bat           double-click launcher for the web app
```

Every front-end is a thin layer over `engine` + `render` — to add one (web UI,
Discord bot), import them and call them; never reimplement the logic.
