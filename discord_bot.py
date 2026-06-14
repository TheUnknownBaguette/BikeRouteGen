"""Example Discord bot — a thin front-end over windroute.

Like the CLI and web app, it just calls `planner.plan_routes` (the shared
pipeline) and presents the result; no routing/scoring logic lives here. Not wired
into the CLI — it's a starting point if you ever want a Discord front-end.

    pip install discord.py
    set DISCORD_TOKEN=...        (and have ORS_API_KEY set, like the CLI)
    python discord_bot.py

Then in a server:  !route Mokena, IL | 30 | road | 2026-06-15 08:00
"""
import os
import tempfile

import discord

from windroute import engine, render, planner

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_message(msg):
    if msg.author == client.user or not msg.content.startswith("!route"):
        return
    try:
        parts = [p.strip() for p in msg.content[len("!route"):].split("|")]
        location = parts[0]
        distance = float(parts[1]) if len(parts) > 1 and parts[1] else 25
        ride_type = parts[2].lower() if len(parts) > 2 and parts[2] else "road"
        when = parts[3] if len(parts) > 3 and parts[3] else "now"

        result = planner.plan_routes(
            location=location, distance=distance, start=when, ride_type=ride_type,
            api_key=os.environ.get("ORS_API_KEY"))
        best = result.options[0]
        c, wind = best.candidate, result.wind

        # Render the recommended route to temp files (render does the work).
        out = os.path.join(tempfile.gettempdir(), "windroute")
        meta = {"title": f"{distance:g} mi {ride_type} {c.shape}",
                "location": result.location_label,
                "when": wind.valid_time.replace("T", " "), "ride_type": ride_type}
        render.render_map(c, wind, meta, out + ".png")
        render.write_gpx(c.coords, out + ".gpx", name=meta["title"])

        alts = " · ".join(f"{o.headline} ({o.candidate.distance_km / 1.609344:.0f} mi)"
                          for o in result.options[1:])
        await msg.channel.send(
            content=(f"**{result.location_label}** | wind "
                     f"{engine.compass_label(wind.direction_from_deg)} "
                     f"{wind.speed_mph:.0f} mph\n"
                     f"**{best.headline}:** {'; '.join(best.reasons)}\n"
                     f"Alternatives: {alts}"),
            files=[discord.File(out + ".png"), discord.File(out + ".gpx")],
        )
    except Exception as exc:
        await msg.channel.send(f"Couldn't plan that one: {exc}")


if __name__ == "__main__":
    client.run(os.environ["DISCORD_TOKEN"])
