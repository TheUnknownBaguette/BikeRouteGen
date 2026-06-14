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

## Everyday use (PowerShell)

Once setup is done, a normal session is just two steps:

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
  zones.py        auto-detect the nearest quiet riding zone (for --ride-area)
  surface.py      OpenStreetMap/Overpass surface + bike-lane source
  corrections.py  the personal "I rode this" correction cache
  render.py       map image + GPX output
  cli.py          the CLI wrapper (typer + rich)
```

Every front-end is a thin layer over `engine` + `render` — to add one (web UI,
Discord bot), import them and call them; never reimplement the logic.
