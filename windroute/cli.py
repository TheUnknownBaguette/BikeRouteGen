"""The 'fancy wrapper' — a polished CLI over the engine.

    python -m windroute.cli plan -l "Champaign, IL" -d 30 -s "2026-06-14 08:00" -r road
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import List

import typer
from dateutil import parser as dateparser
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.table import Table
from rich.text import Text

from . import engine, render, surface, rwgps, learn, planner
from .planner import SURFACE_DISAGREE
from .corrections import (CorrectionCache, parse_gpx, downsample,
                          parse_road_notes, ROAD_NOTES_TEMPLATE)

# Windows consoles default stdout to cp1252, which raises UnicodeEncodeError on
# the box-drawing / maths / symbol glyphs rich emits (tables, ⚠, ≤, …). Force
# UTF-8 so output renders instead of crashing. Best-effort: a stream without
# reconfigure (e.g. under test capture) is left as-is.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

app = typer.Typer(add_completion=False, help="Generate wind-smart cycling routes.")
console = Console()


@app.callback()
def _root():
    """Generate wind-smart cycling routes."""


@app.command()
def plan(
    location: str = typer.Option(
        ..., "--location", "-l",
        help="Start point: a town ('Chicago, IL'), a full street address "
             "('233 S Wacker Dr, Chicago, IL'), or exact 'lat,lng' coordinates "
             "('41.8789,-87.6359'). Use an address/coords to start right where you "
             "reach the bike path."),
    distance: float = typer.Option(..., "--distance", "-d", help="Ride distance."),
    tolerance: float = typer.Option(3.0, "--tolerance", "-t",
                                    help="Acceptable +/- distance buffer (same unit as -d)."),
    unit: str = typer.Option("mi", "--unit", help="'mi' or 'km'."),
    start: str = typer.Option("now", "--start", "-s",
                              help="'YYYY-MM-DD HH:MM' or 'now'."),
    ride_type: str = typer.Option("road", "--ride-type", "-r", help="'road' or 'gravel'."),
    shapes: str = typer.Option(
        "loop,lollipop,rectangle", "--shapes",
        help="Comma list of route forms to consider: loop, lollipop, rectangle, "
             "out-and-back, roundtrip. 'loop' is a clean geometric polygon routed "
             "through corners around the start (no scattered via-points). A "
             "rectangle is a long leg into the wind, a short crosswind jog, and a "
             "long parallel leg home. 'roundtrip' is the older ORS round_trip "
             "algorithm (can tangle; opt-in). Pure out-and-backs are off by "
             "default; add 'out-and-back' to allow them."),
    surface_source: str = typer.Option(
        "ors", "--surface-source",
        help="Surface data: 'ors' (default, from the route call), "
             "'osm' (OpenStreetMap/Overpass tags, finer for gravel), or "
             "'both' (cross-check ORS vs OSM and flag disagreements)."),
    ride_area: str = typer.Option(
        None, "--ride-area",
        help="Stage the ride to quieter country: 'auto' auto-detects the nearest "
             "good quiet riding zone (open farmland, low-traffic grid) and adds a "
             "'staging' option that transits there, loops on the wind, and rides "
             "home; a compass direction (e.g. 'south', 'SSE') stages to the best "
             "quiet zone that way; or pass a place/'lat,lng' to force a specific "
             "staging zone. Omit for a normal ride from the start point."),
    out: str = typer.Option(None, "--out", "-o", help="Output file basename. Omit for "
                            "auto-named files (e.g. jun14-30mi-loop-Swind.gpx)."),
    candidates: int = typer.Option(12, "--candidates", help="Routes to generate and rank."),
    corrections: bool = typer.Option(
        True, "--corrections/--no-corrections",
        help="Apply your personal 'I rode this' correction cache on top of the "
             "surface data (see the 'mark' command)."),
    corrections_file: str = typer.Option(
        None, "--corrections-file",
        help="Path to the corrections JSON (default ~/.windroute/corrections.json)."),
    classify: bool = typer.Option(
        False, "--classify",
        help="Classify the start's terrain archetype (grid-farmland, mountain, "
             "suburban-sprawl, …) and show it. Diagnostic only for now — it does "
             "not yet change scoring (that's a later work-plan task)."),
    api_key: str = typer.Option(None, "--api-key", envvar="ORS_API_KEY",
                                help="OpenRouteService key (or set ORS_API_KEY)."),
):
    """Plan a route, rank candidates by wind + surface, write image + GPX."""
    ride_type = ride_type.lower().strip()
    try:
        with console.status("[cyan]Planning your route (wind, candidates, scoring)\u2026"):
            result = planner.plan_routes(
                location=location, distance=distance, unit=unit, start=start,
                ride_type=ride_type, shapes=shapes, surface_source=surface_source,
                ride_area=ride_area, tolerance=tolerance, candidates=candidates,
                corrections=corrections, corrections_file=corrections_file,
                api_key=api_key, n_alternatives=2, classify=classify)

        wind, label, when = result.wind, result.location_label, result.when
        mode = result.surface_mode
        ranked, options = result.ranked, result.options
        best = options[0].candidate

        console.print(_wind_panel(wind, label, when))
        if result.region is not None:
            console.print(_region_panel(result.region))
        for note in result.notes:
            console.print(f"[dim]{note}[/]")

        console.print(_candidates_table(ranked, ride_type, compare=(mode == "both"),
                                         show_lane=(mode in ("osm", "both"))))

        # File names: if --out was given, use <out> / <out>-alt1/-alt2 (backwards
        # compatible). Otherwise auto-name each route descriptively (date, distance,
        # shape, wind) and de-dupe, so head units show more than just "route".
        if out:
            bases = [out if i == 0 else f"{out}-alt{i}" for i in range(len(options))]
        else:
            bases = render.dedupe_names([
                render.route_basename(when, o.candidate.distance_km, unit,
                                      o.candidate.shape, wind.direction_from_deg)
                for o in options])
        blocks = []
        for i, opt in enumerate(options):
            c = opt.candidate
            base = bases[i]
            meta = {
                "title": f"{distance:g} {unit} {ride_type} {c.shape} - {opt.headline}",
                "location": label,
                "when": wind.valid_time.replace("T", " "),
                "ride_type": ride_type,
            }
            png = render.render_map(c, wind, meta, f"{base}.png")
            gpx = render.write_gpx(c.coords, f"{base}.gpx", name=meta["title"])
            tag = ("[bold green]RECOMMENDED[/]" if opt.role == "recommended"
                   else f"[bold cyan]Option {i + 1}[/]")
            reasons = "\n".join(f"     - {r}" for r in opt.reasons)
            blocks.append(f"{tag}  [bold]{opt.headline}[/]\n{reasons}\n"
                          f"     [dim]{png}  |  {gpx}[/]")

        if mode == "both" and "osm" in best.surface_by_source:
            delta = abs(best.surface_by_source["osm"] - best.surface_by_source.get("ors", 0.0))
            blocks.append("[green]Surface check:[/] ORS and OSM agree on the pick"
                          if delta <= SURFACE_DISAGREE else
                          "[yellow]Surface check:[/] ORS and OSM disagree on the pick — "
                          "worth eyeballing in Street View")

        console.print(Panel(
            Text.from_markup("\n\n".join(blocks)),
            title="[bold green]Route options (import any to Ride with GPS)",
            border_style="green"))

    except Exception as exc:                                    # surface a clean message
        console.print(Panel(str(exc), title="[bold red]Error", border_style="red"))
        raise typer.Exit(code=1)


@app.command()
def mark(
    surface: str = typer.Option(
        None, "--surface", help="What it really is: 'paved' or 'unpaved' (gravel)."),
    traffic: str = typer.Option(
        None, "--traffic", help="How it really rides: 'quiet' or 'busy'."),
    gpx: str = typer.Option(None, "--gpx", help="A GPX file of a ride along the road."),
    point: List[str] = typer.Option(
        None, "--point", help="'lat,lng' (repeat the flag to trace a polyline)."),
    between: str = typer.Option(
        None, "--between", help="Start place; with --to, route the road and mark it."),
    to: str = typer.Option(None, "--to", help="End place, used with --between."),
    radius: float = typer.Option(40.0, "--radius", help="Match radius in metres."),
    label: str = typer.Option(None, "--label", help="Optional name for this correction."),
    note: str = typer.Option("", "--note", help="Optional free-text note."),
    ride_type: str = typer.Option("road", "--ride-type", "-r",
                                  help="Routing profile for --between."),
    corrections_file: str = typer.Option(None, "--corrections-file"),
    api_key: str = typer.Option(None, "--api-key", envvar="ORS_API_KEY"),
):
    """Record a personal 'I rode this' correction (surface and/or traffic).

    Pick exactly one geometry source: --gpx (a ride you recorded), --point
    (repeat for a line), or --between/--to (route a road by its endpoints).
    """
    surface = surface.lower().strip() if surface else None
    traffic = traffic.lower().strip() if traffic else None
    try:
        chosen = [bool(gpx), bool(point), bool(between and to)]
        if sum(chosen) != 1:
            raise ValueError("Choose exactly one geometry source: --gpx, --point "
                             "(one or more), or --between with --to.")

        if gpx:
            coords = downsample(parse_gpx(gpx))
            origin = f"GPX {gpx}"
            if not coords:
                raise ValueError(f"No track points found in {gpx}.")
        elif point:
            coords = [_parse_latlng(p) for p in point]
            origin = f"{len(coords)} point(s)"
        else:
            profile = engine.PROFILE_BY_RIDE.get(ride_type.lower().strip(), "cycling-regular")
            with console.status("[cyan]Routing the road for your correction…"):
                lat1, lng1, lbl1 = engine.geocode(between)
                lat2, lng2, lbl2 = engine.geocode(to)
                road, _e, _d, _p, _u, _b, _pa, _pr = engine._ors_directions(
                    api_key, profile, [[lng1, lat1], [lng2, lat2]], timeout=40)
            coords = downsample(road)
            origin = f"{lbl1} -> {lbl2}"

        cache = CorrectionCache.load(corrections_file)
        rec = cache.add(coords, surface=surface, traffic=traffic,
                        radius_m=radius, label=label, note=note)
        path = cache.save()

        labels = ", ".join(x for x in (
            f"surface={surface}" if surface else "",
            f"traffic={traffic}" if traffic else "") if x)
        console.print(Panel.fit(
            Text.from_markup(
                f"[green]Saved correction[/] [bold]{rec['label']}[/] ({labels})\n"
                f"From {origin}: {len(coords)} point(s), {radius:.0f} m match radius\n"
                f"[dim]{path}[/]"),
            title="[bold green]Marked", border_style="green"))
    except Exception as exc:
        console.print(Panel(str(exc), title="[bold red]Error", border_style="red"))
        raise typer.Exit(code=1)


@app.command(name="corrections")
def corrections_cmd(
    corrections_file: str = typer.Option(None, "--corrections-file"),
):
    """List the personal corrections on file."""
    cache = CorrectionCache.load(corrections_file)
    if not cache.records:
        console.print(f"[dim]No corrections yet ({cache.path}). "
                      f"Add one with 'mark'.[/]")
        return
    t = Table(title=f"Personal corrections ({cache.path})", header_style="bold")
    t.add_column("#", justify="right")
    t.add_column("Label")
    t.add_column("Surface")
    t.add_column("Traffic")
    t.add_column("Pts", justify="right")
    t.add_column("Radius", justify="right")
    t.add_column("Added")
    t.add_column("Note")
    for i, rec in enumerate(cache.records, 1):
        t.add_row(str(i), rec.get("label", ""), rec.get("surface") or "[dim]-[/]",
                  rec.get("traffic") or "[dim]-[/]", str(len(rec.get("coords", []))),
                  f"{rec.get('radius_m', 40):.0f} m",
                  (rec.get("added", "") or "").replace("T", " "), rec.get("note", ""))
    console.print(t)


@app.command()
def forget(
    key: str = typer.Argument(..., help="Label or 1-based index to remove."),
    corrections_file: str = typer.Option(None, "--corrections-file"),
):
    """Delete a personal correction by label or number."""
    cache = CorrectionCache.load(corrections_file)
    if cache.remove(key):
        cache.save()
        console.print(f"[green]Removed[/] correction {key!r}.")
    else:
        console.print(f"[yellow]No correction matched[/] {key!r}. "
                      f"Run 'corrections' to see the list.")
        raise typer.Exit(code=1)


@app.command(name="roads-import")
def roads_import(
    file: str = typer.Argument(
        "road-notes.txt",
        help="Road-notes text file. If it doesn't exist, a template is created."),
    radius: float = typer.Option(40.0, "--radius",
                                 help="Match radius in metres for each road."),
    ride_type: str = typer.Option("road", "--ride-type", "-r",
                                  help="Routing profile used to trace each A->B road."),
    append: bool = typer.Option(
        False, "--append",
        help="Keep notes previously imported from this file instead of re-syncing "
             "(default replaces this file's prior imports so the file stays the "
             "source of truth)."),
    corrections_file: str = typer.Option(None, "--corrections-file"),
    api_key: str = typer.Option(None, "--api-key", envvar="ORS_API_KEY"),
):
    """Bulk-import road notes (gravel / busy / quiet / paved) from a text file.

    Each line is `<tags>: <A> -> <B>` — e.g. `gravel: Manhattan, IL -> Symerton, IL`.
    Endpoints are geocoded and the road between them is traced and added to your
    personal correction cache, so every future `plan` knows about it. Re-run after
    editing the file; by default it re-syncs (replaces this file's earlier import).
    """
    path = Path(file)
    if not path.exists():
        path.write_text(ROAD_NOTES_TEMPLATE, encoding="utf-8")
        console.print(Panel.fit(
            Text.from_markup(
                f"[green]Created a road-notes template[/] at [bold]{path}[/].\n"
                f"Open it, add your roads (one per line), then run "
                f"[bold]roads-import {file}[/] again."),
            title="[bold cyan]Edit this, then re-run", border_style="cyan"))
        return

    try:
        entries, errors = parse_road_notes(path.read_text(encoding="utf-8"))
        for ln, txt, why in errors:
            console.print(f"[yellow]line {ln}:[/] {why} — [dim]{txt}[/]")
        if not entries:
            console.print(Panel("No usable road lines found. Each line must read "
                                "`<tags>: <A> -> <B>`.", title="[bold yellow]Nothing to import",
                                border_style="yellow"))
            raise typer.Exit(code=1)

        cache = CorrectionCache.load(corrections_file)
        origin = str(path.resolve())
        removed = 0
        if not append:
            before = len(cache.records)
            cache.records = [r for r in cache.records
                             if not (r.get("source") == "roads-import"
                                     and r.get("origin") == origin)]
            removed = before - len(cache.records)

        profile = engine.PROFILE_BY_RIDE.get(ride_type.lower().strip(), "cycling-regular")
        added = 0
        failed = []
        with Progress(console=console, transient=True) as prog:
            task = prog.add_task("Tracing roads", total=len(entries))
            for e in entries:
                try:
                    lat1, lng1, _l1 = engine.geocode(e["a"])
                    lat2, lng2, _l2 = engine.geocode(e["b"])
                    road, *_rest = engine._ors_directions(
                        api_key, profile, [[lng1, lat1], [lng2, lat2]], timeout=40)
                    coords = downsample(road)
                    if not coords:
                        raise ValueError("no route between those endpoints")
                    rec = cache.add(coords, surface=e["surface"], traffic=e["traffic"],
                                    radius_m=radius, note=e["raw"])
                    rec["source"] = "roads-import"
                    rec["origin"] = origin
                    added += 1
                except Exception as exc:                  # geocode / routing failure
                    failed.append((e, str(exc)))
                prog.advance(task)
                time.sleep(0.3)                           # polite API pacing

        cache.save()
        for e, why in failed:
            console.print(f"[red]line {e['line']}:[/] couldn't trace — {why} "
                          f"[dim]{e['raw']}[/]")
        msg = f"[green]Imported {added} road note(s)[/] into [dim]{cache.path}[/]"
        if removed:
            msg += f"\nReplaced {removed} earlier note(s) from this file (use --append to keep)."
        if failed:
            msg += (f"\n[yellow]{len(failed)} line(s) failed[/] — usually a place that "
                    f"didn't geocode; try an address or lat,lng pin.")
        msg += "\nThey're applied on top of the surface data on every plan."
        console.print(Panel.fit(Text.from_markup(msg), title="[bold green]Done",
                                border_style="green"))
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(Panel(str(exc), title="[bold red]Error", border_style="red"))
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# Ride with GPS: learn from your real trip history
# --------------------------------------------------------------------------- #
@app.command(name="rwgps-login")
def rwgps_login(
    api_key: str = typer.Option(
        ..., "--api-key", envvar="RWGPS_API_KEY",
        help="RWGPS API key from your API client (ridewithgps.com/settings/developers, "
             "or set RWGPS_API_KEY)."),
    auth_token: str = typer.Option(
        None, "--auth-token", envvar="RWGPS_AUTH_TOKEN",
        help="Auth token from the API client's edit page ('Create new Auth Token'). "
             "Paste this to skip the password exchange entirely (recommended)."),
    email: str = typer.Option(
        None, "--email", help="Account email - only for the password fallback."),
    password: str = typer.Option(
        None, "--password",
        help="Account password - only if you can't paste an --auth-token. Exchanged "
             "for a token, never stored."),
):
    """Save RWGPS credentials. Paste --auth-token (no password), or use --email/--password.

    Preferred path (no password): create an API client at
    ridewithgps.com/settings/developers, open its edit page, click 'Create new
    Auth Token', then run this with --api-key and --auth-token.
    """
    try:
        if auth_token:
            user_id = None
            with console.status("[cyan]Verifying token with Ride with GPS…"):
                try:
                    user = rwgps.get_current_user(api_key, auth_token)
                    user_id = rwgps._first(user, "id", "user_id")
                except rwgps.RwgpsError as exc:
                    raise typer.BadParameter(
                        f"That api_key/auth_token pair was rejected: {exc}")
        elif email and password:
            with console.status("[cyan]Authenticating with Ride with GPS…"):
                auth_token, user_id = rwgps.get_auth_token(email, password, api_key)
        else:
            raise typer.BadParameter(
                "Provide --auth-token (recommended — no password needed), or both "
                "--email and --password.")

        path = rwgps.Credentials(api_key=api_key, auth_token=auth_token,
                                 user_id=user_id).save()
        console.print(Panel.fit(
            Text.from_markup(
                f"[green]Connected to Ride with GPS[/]"
                f"{f' as user {user_id}' if user_id else ''}.\n"
                f"Credentials saved to [dim]{path}[/]\n"
                f"Now run [bold]import[/] to download your trips, then [bold]learn[/]."),
            title="[bold green]Connected", border_style="green"))
    except typer.BadParameter:
        raise
    except Exception as exc:
        console.print(Panel(str(exc), title="[bold red]Login failed", border_style="red"))
        raise typer.Exit(code=1)


@app.command(name="import")
def import_trips(
    limit: int = typer.Option(None, "--limit",
                              help="Max trips to consider (newest first)."),
    since: str = typer.Option(None, "--since",
                              help="Only trips on/after this date (YYYY-MM-DD)."),
    refresh: bool = typer.Option(False, "--refresh",
                                 help="Re-download trips already in the cache."),
):
    """Download your recorded trips into the local cache (~/.windroute/trips)."""
    creds = rwgps.Credentials.load()
    if not creds.ok:
        console.print(Panel(
            "Not logged in. Run 'rwgps-login' first (or set RWGPS_API_KEY + "
            "RWGPS_AUTH_TOKEN).", title="[bold red]No credentials",
            border_style="red"))
        raise typer.Exit(code=1)

    since_date = dateparser.parse(since).date() if since else None
    have = set() if refresh else rwgps.cached_trip_ids()
    try:
        with console.status("[cyan]Listing your trips…"):
            rows = list(rwgps.list_trips(creds.api_key, creds.auth_token,
                                         max_trips=limit))
        todo, skipped = [], 0
        for row in rows:
            s = rwgps.trip_summary(row)
            if since_date and s["departed_at"]:
                try:
                    if dt.datetime.fromisoformat(s["departed_at"]).date() < since_date:
                        continue
                except ValueError:
                    pass
            if str(s["id"]) in have:
                skipped += 1
                continue
            todo.append(s["id"])

        fetched = failed = 0
        if todo:
            with Progress(console=console, transient=True) as prog:
                task = prog.add_task("Downloading trips", total=len(todo))
                for tid in todo:
                    try:
                        trip = rwgps.get_trip(creds.api_key, creds.auth_token, tid)
                        rwgps.save_trip(trip)
                        fetched += 1
                    except Exception:
                        failed += 1
                    prog.advance(task)
                    time.sleep(0.3)               # polite API pacing
    except Exception as exc:
        console.print(Panel(str(exc), title="[bold red]Error", border_style="red"))
        raise typer.Exit(code=1)

    msg = (f"[green]Imported {fetched} new trip(s)[/] into "
           f"[dim]{rwgps.trips_dir()}[/]")
    if skipped:
        msg += f"\n{skipped} already cached (use --refresh to re-download)."
    if failed:
        msg += f"\n[yellow]{failed} failed to download.[/]"
    msg += "\nRun [bold]learn[/] to see what your history reveals."
    console.print(Panel.fit(Text.from_markup(msg), title="[bold green]Done",
                            border_style="green"))


def learn_cmd(
    save_json: bool = typer.Option(
        False, "--save-json",
        help="Also write the raw analysis to ~/.windroute/trip_analysis.json."),
    no_surface: bool = typer.Option(
        False, "--no-surface",
        help="Skip OSM surface/waytype lookups (much faster; geometry + wind only)."),
    no_wind: bool = typer.Option(
        False, "--no-wind", help="Skip the historical wind backfill."),
    all_activities: bool = typer.Option(
        False, "--all-activities",
        help="Include walks/hikes/indoor too (default: outdoor bike rides only)."),
):
    """Analyse your cached trips and report what they reveal about your routes."""
    trips = rwgps.load_cached_trips()
    if not trips:
        console.print(Panel("No cached trips. Run 'import' first.",
                            title="[bold yellow]Nothing to analyse",
                            border_style="yellow"))
        raise typer.Exit(code=1)

    # Keep only real outdoor bike rides unless asked for everything.
    if all_activities:
        kept = trips
        excluded = 0
    else:
        kept = [(s, c) for s, c in trips if rwgps.is_outdoor_cycling(s)]
        excluded = len(trips) - len(kept)
    if not kept:
        console.print(Panel(
            "No outdoor cycling trips found (only walks/indoor?). Re-run with "
            "--all-activities to analyse everything.",
            title="[bold yellow]Nothing to analyse", border_style="yellow"))
        raise typer.Exit(code=1)
    if excluded:
        console.print(f"[dim]Analysing {len(kept)} outdoor cycling trips "
                      f"({excluded} walks/hikes/indoor excluded; "
                      f"--all-activities to include).[/]")

    feats = []
    with Progress(console=console, transient=True) as prog:
        task = prog.add_task("Analysing trips", total=len(kept))
        for summ, coords in kept:
            surf = None
            if not no_surface and coords:
                try:
                    surf = surface.OverpassSurface().build([coords])
                except Exception:
                    surf = None
            f = learn.trip_features(coords, departed_at=summ.get("departed_at"),
                                    surf=surf, do_wind=not no_wind,
                                    track_type=summ.get("track_type"),
                                    activity_type=summ.get("activity_type"))
            if f:
                f["name"] = summ.get("name")
                feats.append(f)
            prog.advance(task)
            if not no_surface:
                time.sleep(0.5)                   # Overpass etiquette

    profile = learn.analyze_trips(feats)
    _print_profile(profile)
    console.print("\n[bold]Suggested tuning[/] [dim](review before changing any "
                  "weights — nothing is changed automatically)[/]:")
    for line in learn.suggest_weight_changes(profile):
        console.print(f"  • {line}")

    if save_json:
        path = Path.home() / ".windroute" / "trip_analysis.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        console.print(f"[dim]Wrote {path}[/]")


# Registered explicitly as 'learn' ('learn_cmd' avoids shadowing the imported
# `learn` module used inside the function body).
app.command(name="learn")(learn_cmd)


def _print_profile(p):
    n = p.get("n_trips", 0)
    if n == 0:
        console.print("[yellow]No analysable trips.[/]")
        return
    d = p["distance_mi"]
    console.print(Panel.fit(
        Text.from_markup(
            f"[bold]{n}[/] trips analysed\n"
            f"Distance (mi): median [bold]{d['median']:.1f}[/]  "
            f"(p25 {d['p25']:.1f} – p75 {d['p75']:.1f}, "
            f"range {d['min']:.1f}–{d['max']:.1f})"),
        title="[bold cyan]Ride history", border_style="cyan"))

    sectors = p["sectors"]
    mx = max(sectors.values()) or 1
    t = Table(title="Outbound direction (where you head from the start)",
              header_style="bold")
    t.add_column("Dir")
    t.add_column("Trips", justify="right")
    t.add_column("")
    for s in engine.COMPASS_16:
        c = sectors.get(s, 0)
        if c:
            # ASCII bar: the block char U+2588 isn't in the legacy Windows
            # console codepage (cp1252) and crashes rich's win32 renderer.
            bar = "#" * max(1, int(round(20 * c / mx)))
            t.add_row(s, str(c), f"[green]{bar}[/]")
    console.print(t)

    t2 = Table(title="What your rides look like", header_style="bold")
    t2.add_column("Metric")
    t2.add_column("Value")
    shapes = ", ".join(f"{k} {v}" for k, v in
                       sorted(p["shapes"].items(), key=lambda kv: -kv[1]))
    t2.add_row("Shape mix", shapes)
    t2.add_row("Mean self-overlap", f"{p['mean_self_overlap'] * 100:.0f}%")
    for key, label in (("unpaved_frac", "Unpaved (gravel)"),
                       ("busy_frac", "Busy highway"),
                       ("path_frac", "Multiuse path (total)"),
                       ("path_run_frac", "Path (longest run)"),
                       ("bikelane_frac", "On-road bike lane")):
        st = p.get(key)
        if st:
            t2.add_row(label, f"mean {st['mean']*100:.0f}%  "
                              f"median {st['median']*100:.0f}%  p90 {st['p90']*100:.0f}%")
    w = p.get("wind")
    if w:
        t2.add_row("Into-wind-first", f"{w['into_wind_share']*100:.0f}% of rides "
                                      f"(mean score {w['mean_score']:+.2f})")
        t2.add_row("Outbound vs wind", f"{w['mean_align_deg']:.0f}° off the "
                                       f"headwind line")
    console.print(t2)
    if p.get("dominant_sectors"):
        console.print(f"[dim]Favourite directions: "
                      f"{', '.join(p['dominant_sectors'])}[/]")


def _parse_latlng(s: str):
    """'41.79,-86.74' -> (41.79, -86.74)."""
    parts = s.replace(" ", "").split(",")
    if len(parts) != 2:
        raise ValueError(f"Bad --point {s!r}; expected 'lat,lng'.")
    return float(parts[0]), float(parts[1])


@app.command()
def classify(
    location: str = typer.Option(
        ..., "--location", "-l",
        help="Place to classify: a town, a street address, or 'lat,lng'."),
    radius: float = typer.Option(
        None, "--radius", help="Sample radius in km (default ~10)."),
):
    """Classify a start point's terrain archetype (no ORS key / routing needed).

    Reads the road network + land use (Overpass) and coarse relief (Open-Meteo)
    around the point and labels it grid-farmland / forested-rolling / mountain /
    suburban-sprawl / coastal / arid-open / unknown, with the feature vector that
    drove the call. Diagnostic foundation for location-aware tuning.
    """
    from . import regions
    try:
        with console.status("[cyan]Reading the area (roads, land use, relief)…"):
            lat, lng, label = engine.geocode(location)
            kwargs = {} if radius is None else {"radius_km": radius}
            prof = regions.classify_region((lat, lng), **kwargs)
        console.print(_region_panel(prof, label))
    except Exception as exc:
        console.print(Panel(str(exc), title="[bold red]Error", border_style="red"))
        raise typer.Exit(code=1)


def _region_panel(prof, label=None):
    """Render a RegionProfile: the archetype headline + the raw feature vector."""
    conf_style = "green" if not prof.low_confidence else "yellow"
    head = (f"[bold]{prof.archetype}[/]   "
            f"[{conf_style}]confidence {prof.confidence:.0%}[/]")
    if label:
        head = f"[bold]{label}[/]\n{head}"
    f = prof.features
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column("k", style="dim")
    t.add_column("v", justify="right")

    def pct(key):
        v = f.get(key)
        return f"{v * 100:.0f}%" if isinstance(v, (int, float)) else "-"

    t.add_row("farmland", pct("farmland_frac"))
    t.add_row("forest", pct("forest_frac"))
    t.add_row("built-up", pct("residential_frac"))
    t.add_row("water", pct("water_frac"))
    rd = f.get("road_density")
    t.add_row("road density", f"{rd:.2f} km/km²" if rd is not None else "-")
    t.add_row("arterial share", pct("arterial_frac"))
    rng, std = f.get("relief_range_m"), f.get("relief_std_m")
    t.add_row("relief range", f"{rng:.0f} m" if rng is not None else "[dim]n/a[/]")
    t.add_row("relief std", f"{std:.0f} m" if std is not None else "[dim]n/a[/]")
    cl = f.get("coastline_km")
    if cl:
        t.add_row("coastline", f"{cl:.1f} km")
    t.add_row("OSM elements", str(f.get("n_elements", "-")))

    body = Table.grid(padding=(0, 0))
    body.add_row(Text.from_markup(head))
    body.add_row(t)
    return Panel(body, title="[bold cyan]Region", border_style="cyan")


def _wind_panel(wind, label, when):
    body = (f"[bold]{label}[/]   {when:%a %b %d, %H:%M}\n"
            f"Wind from [bold]{engine.compass_label(wind.direction_from_deg)}[/] "
            f"({wind.direction_from_deg:.0f}\u00b0) at "
            f"[bold]{wind.speed_mph:.0f} mph[/], gusting {wind.gust_mph:.0f}\n"
            f"[dim]Ride out toward {engine.compass_label(wind.direction_from_deg)} "
            f"to take the headwind while fresh.[/]")
    return Panel(body, title="[bold cyan]Wind", border_style="cyan")


def _candidates_table(ranked, ride_type, compare=False, show_lane=False):
    # Both modes show the *confirmed-unpaved* fraction. On gravel rides that's the
    # number you want high ("Unpaved %"); on road rides it's the known gravel you
    # want low ("Gravel %"). Showing paved% on road was misleading because unknown
    # surface (most of it) isn't gravel — it just isn't tagged.
    surf_col = "Unpaved %" if ride_type == "gravel" else "Gravel %"
    t = Table(title="Candidate routes (ranked)", header_style="bold")
    t.add_column("#", justify="right")
    t.add_column("Shape")
    t.add_column("Dist (km)", justify="right")
    t.add_column("Climb (m)", justify="right")
    t.add_column("Wind", justify="right")
    if compare:
        # show both readings side by side (always as unpaved %, the gravel-relevant number)
        t.add_column("Unp% ORS", justify="right")
        t.add_column("Unp% OSM", justify="right")
        t.add_column("Check", justify="center")
    else:
        t.add_column(surf_col, justify="right")
    t.add_column("Hwy %", justify="right")
    t.add_column("Path %", justify="right")
    if show_lane:
        t.add_column("Lane %", justify="right")
    t.add_column("Cross", justify="right")            # self-intersections (tangle)
    t.add_column("Score", justify="right")
    for i, c in enumerate(ranked, 1):
        wind_cell = ("[green]into wind 1st[/]" if c.wind_score > 0.2
                     else "[red]wind against[/]" if c.wind_score < -0.2
                     else "neutral")
        style = "bold green" if i == 1 else ""
        row = [str(i), c.shape, f"{c.distance_km:.1f}", f"{c.ascent_m:.0f}", wind_cell]
        if compare:
            ors = c.surface_by_source.get("ors")
            osm = c.surface_by_source.get("osm")
            ors_cell = f"{ors * 100:.0f}" if ors is not None else "[dim]-[/]"
            osm_cell = f"{osm * 100:.0f}" if osm is not None else "[dim]-[/]"
            if ors is None or osm is None:
                check = "[dim]-[/]"
            elif abs(ors - osm) > SURFACE_DISAGREE:
                check = "[yellow]warn[/]"
            else:
                check = "[green]ok[/]"
            row += [ors_cell, osm_cell, check]
        elif ride_type == "gravel":
            cell = f"{c.unpaved_frac * 100:.0f}"              # more is better
            if c.good_gravel_frac:                            # confirmed good gravel
                cell += f" [green]+{c.good_gravel_frac * 100:.0f}g[/]"
            if c.unrideable_frac:                             # unrideable % (avoided)
                cell += f" [red]!{c.unrideable_frac * 100:.0f}[/]"
            row.append(cell)
        else:
            g = c.unpaved_frac * 100                          # confirmed gravel; less is better
            cell = (f"{g:.0f}" if c.unpaved_frac < 0.15
                    else f"[yellow]{g:.0f}[/]" if c.unpaved_frac < 0.35
                    else f"[red]{g:.0f}[/]")
            if c.unrideable_frac:
                cell += f" [red]!{c.unrideable_frac * 100:.0f}[/]"
            row.append(cell)
        hwy_pct = c.busy_frac * 100
        hwy_cell = (f"{hwy_pct:.0f}" if c.busy_frac <= engine.BUSY_FREE_FRAC
                    else f"[yellow]{hwy_pct:.0f}[/]" if c.busy_frac < 0.20
                    else f"[red]{hwy_pct:.0f}[/]")
        row.append(hwy_cell)
        # Path %: mildly disliked multiuse trails (yellow as they grow).
        path_pct = c.path_frac * 100
        path_cell = (f"{path_pct:.0f}" if c.path_frac <= engine.PATH_FREE_FRAC
                     else f"[yellow]{path_pct:.0f}[/]")
        row.append(path_cell)
        if show_lane:
            # Lane %: on-road bike lanes are a bonus (green when present).
            lane_pct = c.bikelane_frac * 100
            row.append(f"[green]{lane_pct:.0f}[/]" if c.bikelane_frac > 0.0
                       else f"{lane_pct:.0f}")
        # Cross: self-intersections — 0 is a clean loop, high means a tangle.
        x = c.self_intersections
        row.append(f"{x}" if x <= 2 else f"[yellow]{x}[/]" if x <= 10
                   else f"[red]{x}[/]")
        row.append(f"{c.total_score:.2f}")
        t.add_row(*row, style=style)
    return t


if __name__ == "__main__":
    app()
