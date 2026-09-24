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

# strip_after_prefix: "! slime" works the same as "!slime" (Paul, 9/8).
bot = commands.Bot(command_prefix="!", intents=intents, strip_after_prefix=True)

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
async def setup_hook():
    # Cogs MUST load before the gateway connects: several cogs listen for
    # on_ready (role_menu view re-registration, invites cache priming,
    # quarantine_lock sweep) and a listener added after READY fires never runs.
    with open("commands.json") as f:
        schema = json.load(f)

    # Shelved commands: the code stays, the command never enters the tree.
    # Discord caps a bot at 100 top-level commands and enforces it at
    # REGISTRATION (CommandLimitReached inside load_extension), so the 101st
    # knocks a whole cog out — removing after the loads is too late. The
    # least-used commands are parked by name in commands.json "shelved" and
    # filtered right here in add_command; edit that list to swap one back.
    shelved = set(schema.get("shelved", []))
    _tree_add = bot.tree.add_command

    def _add_command(command, /, *args, **kwargs):
        name = getattr(command, "name", None)
        if name in shelved and getattr(command, "parent", None) is None:
            print(f"Shelved command: /{name}")
            return None
        return _tree_add(command, *args, **kwargs)

    bot.tree.add_command = _add_command

    # A cog loads because it owns a command below — or because it's named in
    # "listener_cogs". The second list is for cogs that only listen (auto_rules
    # runs the dashboard's rules and owns no command since /automation left the
    # tree 9/22); without it they drop out silently on the next restart, which
    # is exactly what took every server's automation rules down 9/23.
    cogs = set(cmd["cog"] for cmd in schema["commands"])
    cogs |= set(schema.get("listener_cogs", []))
    for cog in cogs:
        try:
            await bot.load_extension(f"cogs.{cog}")
            print(f"Loaded cog: {cog}")
        except Exception as e:
            print(f"[WARN] Could not load cog '{cog}': {e}")

    # Say so, loudly, when a cog file with a setup() isn't on either list.
    # Not loaded automatically — the VPS keeps stray files in cogs/ — just named,
    # so the next command removal that orphans a cog shows up in the journal.
    cog_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cogs")
    for fn in sorted(os.listdir(cog_dir)):
        name = fn[:-3]
        if not fn.endswith(".py") or fn.startswith("_") or name in cogs:
            continue
        try:
            with open(os.path.join(cog_dir, fn), encoding="utf-8") as f:
                has_setup = "async def setup(" in f.read()
        except OSError:
            continue
        if has_setup:
            print(f"[WARN] cogs/{fn} has a setup() but is NOT loaded — "
                  f"add it to commands.json \"listener_cogs\" if it should run")

    # Seed the home guild's per-guild security config from the legacy ALTGUARD_*/
    # ANTINUKE_* env vars on first run, so it keeps its exact current protection
    # through the multi-guild refactor (no protection gap). No-op once seeded.
    try:
        from utils.security_config import seed_from_env
        if seed_from_env(1215140346800119868):
            print("Security config: seeded home guild from env.")
    except Exception as e:
        print(f"[WARN] security seed failed: {e}")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Sync globally so the bot works on any server (takes up to 1 hour to propagate)
    await bot.tree.sync()
    print("Slash commands synced globally.")
    # Clear any stale guild-specific commands (removes duplicates caused by old copy_global_to)
    home_guild = discord.Object(id=1215140346800119868)
    bot.tree.clear_commands(guild=home_guild)
    await bot.tree.sync(guild=home_guild)
    print("Cleared stale guild-specific commands.")

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

    await _post_status("✅ Torvex Forerunner is back online and ready!")

async def _post_status(msg: str):
    """Post a status message to every guild's configured status channel."""
    for guild in bot.guilds:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{TORVEX_API_URL}/api/bot/guild-config/{guild.id}",
                    headers={"X-Bot-Key": TORVEX_BOT_KEY}
                ) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
            channel_id = data.get("statusChannelId")
            if not channel_id:
                continue
            channel = guild.get_channel(int(channel_id))
            if channel:
                await channel.send(msg)
        except Exception:
            pass

GUILD_EVENTS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guild_events.db")


def _record_guild_event(event: str, guild):
    """Durably record the bot being added to / removed from a guild.

    BlackEye Cafe (921423625112928286) removed the bot at 2026-08-07 04:00 and
    nothing recorded it: the departure had to be reconstructed from the last
    row in stats.db. A guild we are no longer in is unreadable — no audit log,
    no member list, not even the name — so whatever we want to know afterwards
    has to be written down at the moment it happens.
    """
    try:
        import sqlite3
        import time

        # guild.me is gone on removal for an unavailable guild; joined_at is the
        # only record of how long we were actually in there.
        me = getattr(guild, "me", None)
        joined_at = getattr(me, "joined_at", None)

        con = sqlite3.connect(GUILD_EVENTS_DB, timeout=5)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS guild_events ("
                "ts REAL, event TEXT, guild_id TEXT, guild_name TEXT, "
                "member_count INTEGER, owner_id TEXT, joined_at TEXT)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_guild_events_gid ON guild_events(guild_id, ts)"
            )
            con.execute(
                "INSERT INTO guild_events VALUES (?,?,?,?,?,?,?)",
                (
                    time.time(),
                    event,
                    str(guild.id),
                    str(guild.name),
                    getattr(guild, "member_count", None),
                    str(getattr(guild, "owner_id", "") or ""),
                    joined_at.isoformat() if joined_at else None,
                ),
            )
            con.commit()
        finally:
            con.close()
    except Exception as e:
        # Never let bookkeeping take the bot down.
        print(f"[WARN] guild event record failed: {e}")


# Operator-only alert when the bot is added to / removed from a server.
# Delivered by email through Resend (the same provider torvex.app uses), so
# nothing is posted in Discord — the server never sees it. All three env
# values must be set or the alert is skipped silently; the ledger row above
# is still written either way.
GUILD_ALERT_EMAIL = (os.getenv("GUILD_ALERT_EMAIL") or "").strip()
GUILD_ALERT_FROM = (os.getenv("GUILD_ALERT_FROM") or "").strip()
RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()


def _guild_alert_payload(event: str, guild, guild_count: int) -> dict:
    """Build the Resend request body for a join/remove alert (pure, testable)."""
    verb = "added to" if event == "join" else "removed from"
    name = str(getattr(guild, "name", "") or "?")
    members = getattr(guild, "member_count", None)
    owner_id = getattr(guild, "owner_id", None)
    owner = getattr(guild, "owner", None)
    owner_txt = f"{owner} ({owner_id})" if owner and owner_id else str(owner_id or "?")
    created = getattr(guild, "created_at", None)
    created_txt = created.strftime("%Y-%m-%d") if created else "?"
    lines = [
        f"Torvex Forerunner was {verb} a server.",
        "",
        f"Server:   {name}",
        f"Guild ID: {guild.id}",
        f"Members:  {members if members is not None else '?'}",
        f"Owner:    {owner_txt}",
        f"Created:  {created_txt}",
        f"Now in:   {guild_count} servers",
    ]
    if event == "join":
        from utils.links import dashboard_url
        lines += ["", f"Dashboard: {dashboard_url(guild.id)}"]
    return {
        "from": GUILD_ALERT_FROM,
        "to": [GUILD_ALERT_EMAIL],
        "subject": f"[Forerunner] {'Added to' if event == 'join' else 'Removed from'} {name} ({members if members is not None else '?'} members)",
        "text": chr(10).join(lines),
    }


async def _send_guild_alert(event: str, guild):
    if not (GUILD_ALERT_EMAIL and GUILD_ALERT_FROM and RESEND_API_KEY):
        return
    try:
        payload = _guild_alert_payload(event, guild, len(bot.guilds))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            ) as r:
                if r.status >= 300:
                    body = (await r.text())[:300]
                    print(f"[WARN] guild alert email failed: HTTP {r.status} {body}")
    except Exception as e:
        # Never let the alert take the bot down or delay the event handlers.
        print(f"[WARN] guild alert email failed: {e}")


@bot.event
async def on_guild_join(guild):
    print(f"[GUILD] JOINED {guild.id} ({guild.name!r}) members={getattr(guild, 'member_count', '?')} — now in {len(bot.guilds)} guilds")
    _record_guild_event("join", guild)
    bot.loop.create_task(_send_guild_alert("join", guild))


@bot.event
async def on_guild_remove(guild):
    # Fires for a kick, a ban, an admin removing the integration, AND for the
    # guild being deleted outright — Discord does not tell us which.
    print(f"[GUILD] REMOVED FROM {guild.id} ({guild.name!r}) members={getattr(guild, 'member_count', '?')} — now in {len(bot.guilds)} guilds")
    _record_guild_event("remove", guild)
    bot.loop.create_task(_send_guild_alert("remove", guild))


@bot.event
async def on_disconnect():
    await _post_status("🔴 Bot is going offline for a restart. Back in a moment!")

import sys
sys.stdout.reconfigure(line_buffering=True)

bot.run(os.getenv("DISCORD_TOKEN"))
