import discord
from discord.ext import commands
import os
import json
import aiohttp
from dotenv import load_dotenv

load_dotenv()

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    # Fire-and-forget peepo bucks reward for linked users
    if TORVEX_BOT_KEY:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{TORVEX_API_URL}/api/bot/orbs/message-reward",
                    json={"discordUserId": str(message.author.id)},
                    headers={"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}
                )
        except Exception:
            pass

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    with open("commands.json") as f:
        schema = json.load(f)

    cogs = set(cmd["cog"] for cmd in schema["commands"])
    for cog in cogs:
        try:
            await bot.load_extension(f"cogs.{cog}")
            print(f"Loaded cog: {cog}")
        except Exception as e:
            print(f"[WARN] Could not load cog '{cog}': {e}")

    guild = discord.Object(id=1215140346800119868)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print("Slash commands synced to guild.")

    # Auto-sync Discord guild emojis → peepo catalog on startup
    if TORVEX_BOT_KEY:
        try:
            guild_obj = bot.get_guild(1215140346800119868)
            print(f"Peepo sync: guild={guild_obj}, emoji_count={len(guild_obj.emojis) if guild_obj else 'N/A'}")
            if guild_obj:
                for e in guild_obj.emojis[:3]:
                    print(f"  emoji: name={e.name!r} url={str(e.url)!r}")
            emoji_payload = [{"name": e.name, "url": str(e.url)} for e in guild_obj.emojis] if guild_obj else []
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{TORVEX_API_URL}/api/bot/peepos/sync",
                    json=emoji_payload,
                    headers={"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}
                ) as r:
                    text = await r.text()
                    print(f"Peepo sync status={r.status} body={text[:200]}")
                    if r.status == 200:
                        import json as _json
                        d = _json.loads(text)
                        print(f"Peepo sync: created={d.get('created',0)}, updated={d.get('updated',0)}, total={d.get('total',0)}")
        except Exception as e:
            print(f"[WARN] Peepo auto-sync failed: {e}")

import sys
sys.stdout.reconfigure(line_buffering=True)

bot.run(os.getenv("DISCORD_TOKEN"))
