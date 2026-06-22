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
   the nearest good quiet riding zone — or one in a compass direction you choose
   (e.g. `south`) — loop on the wind there, and ride home.

---

## First-time setup (PowerShell)

> **Only want the web app?** You can skip steps 2–3 below — `run.bat` builds the
> virtual environment and installs dependencies for you the first time you launch
> it. You just need [Python](https://www.python.org/downloads/) on your PATH and an
> OpenRouteService key (steps 4–5). The full setup here is for the command line.

Run these once. They assume you're in the project folder.

```powershell
# 1. Go to the project folder (wherever you cloned it)
cd path\to\BikeRouteGen

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

**Double-click `run.bat`.** On first run (or on a new machine) it builds the local
Python environment for you, then starts a local server and opens your browser to
`http://127.0.0.1:5000`. Fill in the form — the **start point** autocompletes
addresses and towns as you type, **start time** is a calendar/clock picker, and an
*advanced* block adds shapes, surface source, and ride-area staging — then hit **Plan
my routes** to get the recommendation plus two alternatives, each with its map inline
and a GPX download. A plan takes ~20–40 s (same routing + wind services as the CLI).
A footer **About** link covers privacy and the ride-safety disclaimer.

It runs only on your own machine (`127.0.0.1`, not exposed to your network) and reads
`ORS_API_KEY` just like the CLI. To stop it, close the terminal window it opened.

> `run.bat` is self-healing: if the environment is missing, or was copied/synced
> from another computer (a virtualenv can't be moved between machines), it rebuilds
> it automatically before launching.

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

Because the hosted form is reachable by others, the web app ships with sensible
defaults for a small public instance: security headers (CSP, `X-Frame-Options`,
HSTS over HTTPS), server-side input limits, a per-IP rate limit on planning (each
plan makes ~12–15 routing calls, so this protects your free-tier quota), and a
visible **About / privacy / disclaimer** page (`/about`). None of it needs
configuration — it's on by default.

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
cd path\to\BikeRouteGen
.\.venv\Scripts\Activate.ps1

# 2. Plan a ride
python -m windroute.cli plan -l "Chicago, IL" -d 30 -s "2026-06-14 08:00" -r road
```

You'll get a ranked table in the terminal, then three route options, each written
as a `.png` map + a `.gpx` track. By default the files are **auto-named** from the
ride — date, distance, shape, and wind — e.g. `jun14-30mi-loop-Swind.gpx`,
`jun14-28mi-rectangle-Swind.gpx`, so they're easy to tell apart on a head unit
instead of all being "route". (Pass `-o myride` to force `myride.gpx` /
`myride-alt1.*` / `myride-alt2.*` instead.) Import whichever GPX you like into
Ride with GPS to refine.

### More examples

```powershell
# Start from an exact address or coordinates (most precise — use this for a
# specific bike-path access point a town centroid would miss)
python -m windroute.cli plan -l "41.8789,-87.6359" -d 25 -s now -r road

# Gravel ride, distance in miles, cross-check surface against OpenStreetMap
python -m windroute.cli plan -l "Champaign, IL" -d 40 --unit mi -r gravel --surface-source both

# Auto-detect the good quiet riding zone and stage the ride to it
python -m windroute.cli plan -l "Chicago, IL" -d 40 --unit mi --ride-area auto

# Adapt the tuning to the local terrain (handy when riding away from home)
python -m windroute.cli plan -l "Asheville, NC" -d 30 --classify
```

### `plan` options

| Option | Meaning |
| --- | --- |
| `-l, --location` | Town (`"Chicago, IL"`), street address, or `lat,lng` / DMS coords. |
| `-d, --distance` | Target ride distance (total, including any transit). |
| `--unit` | `mi` (default) or `km`. |
| `-t, --tolerance` | Free +/- distance buffer; only distance beyond it is penalized (default 3). |
| `-s, --start` | `"YYYY-MM-DD HH:MM"` or `now`. Sets the wind forecast hour. |
| `-r, --ride-type` | `road` (avoid gravel) or `gravel` (seek it). |
| `--shapes` | Comma list: `loop,lollipop,rectangle,out-and-back,roundtrip,wind` (default `loop,lollipop,rectangle`). `wind` rides headwind-out to a turnaround then takes **different roads home** with the tailwind (opt-in). `roundtrip` is the old ORS round-trip algorithm (can tangle; opt-in). |
| `--surface-source` | `ors` (default), `osm` (finer OSM tags + bike lanes), or `both` (cross-check). |
| `--ride-area` | `auto` to stage to the nearest quiet zone, a compass direction (`south`, `SSE`) to stage to the best quiet zone that way, or a place / `lat,lng` to force one. Omit for a normal ride from the start. |
| `--classify` | Detect the terrain (grid-farmland, mountain, suburban, coastal, …) and **adapt the tuning to it** — weights, route shapes, quiet-zone scoring, and a region-normalized busy penalty. Off by default (your home grid-farmland results are unchanged without it). |
| `--refine` | Local-search the top routes: nudge their corners and re-route to squeeze out a better score, keeping length in tolerance. Costs a few extra routing calls; off by default. |
| `--corrections / --no-corrections` | Apply your personal "I rode this" cache (on by default). |
| `--candidates` | How many routes to generate and rank (default 12; more = better odds, slower, more API calls). |
| `-o, --out` | Output file basename. Omit to auto-name each file by date/distance/shape/wind (e.g. `jun14-30mi-loop-Swind.gpx`); pass a name to force `<name>.*` / `<name>-alt1.*`. |
| `--api-key` | ORS key override (normally read from `ORS_API_KEY`). |

### Terrain-aware tuning (`--classify`)

The scoring was tuned for flat Illinois grid-and-farmland riding. Add `--classify` and the tool
first reads the terrain around your start — grid-farmland, forested-rolling, mountain,
suburban-sprawl, coastal, arid-open — and adapts: which route **shapes** make sense (e.g. it drops
the grid-only "rectangle" in the mountains), the **weights** (wind matters a little less where the
terrain dominates), what counts as a good **quiet zone** (farmland in the grid, forest in the hills,
the shore on the coast), and it normalizes the busy-road penalty to the local network so unavoidable
arterials don't sink every route. It's **off by default**, so your home-region results don't change
unless you ask for it.

Just want to see what terrain a place is? `classify` needs no API key (it only reads OpenStreetMap +
elevation):

```powershell
python -m windroute.cli classify -l "Asheville, NC"
```

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
busy:   41.8500,-87.6500 -> 41.8400,-87.6500
gravel, quiet: 100 N Main St, Joliet, IL -> Plainfield, IL
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
  cache are how you inject reality. Street View still wins for surface truth. When the
  tags are sparse, the tool now says so — a **low surface-data confidence** flag rather
  than a confidently-wrong gravel figure.
- **Wind optimization** ranks a handful of generated routes rather than searching
  exhaustively. More `--candidates` improves the odds at the cost of speed/API calls.
- **Ride-area staging** uses open farmland as its "quiet country" proxy, so it
  shines in grid/cornfield regions and is weaker where good riding isn't farmland.
- **Terrain adaptation** (`--classify`) is calibrated against real rides only for flat
  grid-farmland; other terrains use sensible first-pass weights, so treat its tuning
  away from home as a reasonable default rather than a finely-tuned one.

## Project layout

```
windroute/
  engine.py       core: geocode + autocomplete, wind, route generation + shapes, scoring (no I/O)
  planner.py      the shared planning pipeline every front-end calls (plan_routes)
  regions.py      classify the terrain archetype around a start (for --classify / classify)
  zones.py        auto-detect a quiet riding zone, by direction or nearest (for --ride-area)
  surface.py      OpenStreetMap/Overpass surface + bike-lane + gravel-quality source; provider registry
  corrections.py  the personal "I rode this" correction cache + road-notes parser
  rwgps.py        Ride with GPS API client (for the `learn` command's trip history)
  learn.py        analyse imported trips -> rider profile + per-region clusters + suggested weight changes
  render.py       map image + GPX output
  cli.py          the CLI wrapper (typer + rich)
webapp.py         the local/hosted web front-end (Flask)
discord_bot.py    optional Discord front-end (thin over planner; not wired in)
templates/        web app HTML (base, index, results, about)
static/           web app JS (app.js) + generated maps/GPX (out/, swept hourly)
tests/            offline unit tests (regions, weights, surface quality, providers)
run.bat           double-click launcher for the web app (self-builds the venv)
Procfile          production start command for a host (waitress-serve webapp:app)
```

Every front-end is a thin layer over `planner` (orchestration) + `engine`/`render`
(logic + output) — to add one, import them and call them; never reimplement the
pipeline.

---

## Credits & data sources

windroute runs entirely on free and open data. **If you share or host it, please
keep these attributions** — a couple are required by license:

- **Routing** — [OpenRouteService](https://openrouteservice.org/) by
  [HeiGIT](https://heigit.org/), which is built on OpenStreetMap data.
- **Wind & geocoding** — [Open-Meteo](https://open-meteo.com/). Open-Meteo data is
  licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), so this credit
  is **required**: *Weather data by [Open-Meteo.com](https://open-meteo.com/).*
- **Wind fallback (US)** — the U.S. [National Weather Service](https://www.weather.gov/)
  (`api.weather.gov`); public-domain, used when Open-Meteo is unavailable.
- **Maps, geocoding & surface data** — © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, licensed under the [ODbL](https://opendatacommons.org/licenses/odbl/).
  Accessed via [Nominatim](https://nominatim.org/) (address geocoding), the
  [Overpass API](https://overpass-api.de/) (surface / bike-lane / farmland tags), and
  [Photon](https://photon.komoot.io/) by [komoot](https://www.komoot.com/) (the
  type-ahead location autocomplete in the web form).
- **Basemap tiles** in the route images — © OpenStreetMap contributors, served from the
  OpenStreetMap [tile servers](https://operations.osmfoundation.org/policies/tiles/)
  (light personal use only; don't point a busy public deployment at them).
- **Trip history** — the [Ride with GPS](https://ridewithgps.com/) API, used by the
  `learn` command on your own recorded rides.

Built with Python and [Flask](https://flask.palletsprojects.com/),
[staticmap](https://github.com/komoot/staticmap), [Pillow](https://python-pillow.org/),
[requests](https://requests.readthedocs.io/), [Typer](https://typer.tiangolo.com/),
[Rich](https://github.com/Textualize/rich), [python-dateutil](https://github.com/dateutil/dateutil),
and [waitress](https://github.com/Pylons/waitress).

> **Note:** OpenStreetMap and CC BY 4.0 both expect the attribution to be *visible to
> people viewing the maps*, not only in this README. The web app already does this —
> every page footer shows "© OpenStreetMap contributors · Weather by Open-Meteo.com"
> with links. Keep that footer (and the `/about` page) if you fork or host it.
