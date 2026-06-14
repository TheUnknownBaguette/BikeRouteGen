"""Example Discord bot — the 'down the line' front-end.

Notice it reuses windroute.engine / render unchanged. This file is a starting
point, not wired into the CLI. To run it:

    pip install discord.py
    export DISCORD_TOKEN=...   ORS_API_KEY=...
    python discord_bot.py

Then in a server:  !route Champaign, IL | 30 | road | 2026-06-14 08:00
"""
import os
import datetime as dt

import discord
from dateutil import parser as dateparser

from windroute import engine, render

ORS_KEY = os.environ["ORS_API_KEY"]
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_message(msg):
    if msg.author == client.user or not msg.content.startswith("!route"):
        return
    try:
        raw = msg.content[len("!route"):].strip()
        parts = [p.strip() for p in raw.split("|")]
        location = parts[0]
        distance_mi = float(parts[1]) if len(parts) > 1 else 25
        ride_type = parts[2].lower() if len(parts) > 2 else "road"
        when = dateparser.parse(parts[3]) if len(parts) > 3 else dt.datetime.now()

        lat, lng, label = engine.geocode(location)
        wind = engine.get_wind(lat, lng, when)
        target_km = distance_mi * 1.609344
        cands = engine.generate_candidates(lat, lng, target_km, ride_type, ORS_KEY, n=6)
        best = engine.evaluate(cands, wind, ride_type, target_km)[0]

        meta = {"title": f"{distance_mi:g} mi {ride_type} loop", "location": label,
                "when": wind.valid_time.replace("T", " "), "ride_type": ride_type}
        render.render_map(best, wind, meta, "route.png")
        render.write_gpx(best.coords, "route.gpx", name=meta["title"])

        await msg.channel.send(
            content=(f"**{label}** | wind {engine.compass_label(wind.direction_from_deg)} "
                     f"{wind.speed_mph:.0f} mph | {engine.explain(best, wind, ride_type)}"),
            files=[discord.File("route.png"), discord.File("route.gpx")],
        )
    except Exception as exc:
        await msg.channel.send(f"Couldn't plan that one: {exc}")


if __name__ == "__main__":
    client.run(os.environ["DISCORD_TOKEN"])
