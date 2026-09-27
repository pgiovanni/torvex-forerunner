import discord
from discord.ext import commands
import os
import json
import aiohttp
from dotenv import load_dotenv

load_dotenv()

from utils import guild_blocklist  # noqa: E402  (needs the env loaded first)

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# strip_after_prefix: "! slime" works the same as "!slime" (Paul, 9/8).
bot = commands.Bot(command_prefix="!", intents=intents, strip_after_prefix=True)
# Guild ids the bot is leaving BECAUSE they are blocklisted (join-time refusal
# below, or `/blocklist add`). on_guild_remove reads it to record the departure
# as `blocked` instead of `remove` and to skip the operator alert.
bot.blocked_leaving = set()

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

    # Blocklist sweep: a server blocked while the bot was in it (or by hand in
    # the db) is left now, not the next time it re-adds the bot.
    for g in list(bot.guilds):
        try:
            await _enforce_blocklist(g)
        except Exception as e:
            print(f"[WARN] blocklist sweep failed for {g.id}: {e}")

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


def _guild_alert_payload(event: str, guild, guild_count: int, reason: str = None, readd: int = 0,
                         notice_posted: bool = None) -> dict:
    """Build the Resend request body for a join/remove/blocked alert (pure, testable)."""
    verb = {"join": "added to", "remove": "removed from"}.get(
        event, "added to a BLOCKED server, and left")
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
    if event == "blocked":
        lines += [f"Blocked:  {reason or '?'}"]
        if readd:
            lines += [f"Re-add:   attempt #{readd} since it was blocked"]
            # Only claim what the journal can back up: the notice is attempted
            # BEFORE this email is built, and the result travels with it.
            lines += ["Notice:   " + ("posted in the server before leaving" if notice_posted
                                      else "NOT posted — no channel the bot could speak in; left silently")]
        lines += ["", "The bot left immediately; its archive for that server is being purged."]
    if event == "join":
        from utils.links import dashboard_url
        lines += ["", f"Dashboard: {dashboard_url(guild.id)}"]
    subject_verb = {"join": "Added to", "remove": "Removed from"}.get(event, "BLOCKED — left")
    return {
        "from": GUILD_ALERT_FROM,
        "to": [GUILD_ALERT_EMAIL],
        "subject": f"[Forerunner] {subject_verb} {name} ({members if members is not None else '?'} members)",
        "text": chr(10).join(lines),
    }


async def _send_guild_alert(event: str, guild, reason: str = None, readd: int = 0,
                            notice_posted: bool = None):
    if not (GUILD_ALERT_EMAIL and GUILD_ALERT_FROM and RESEND_API_KEY):
        return
    try:
        # A blocked alert is built while the bot is still inside the server it
        # is about to leave — don't count that one ("Now in: 29" was wrong).
        count = len(bot.guilds) - (1 if event == "blocked" else 0)
        payload = _guild_alert_payload(event, guild, count, reason=reason, readd=readd,
                                       notice_posted=notice_posted)
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


def _blocked_departures(guild_id) -> int:
    """How many `blocked` rows the ledger already holds for this guild — the
    re-add counter. Read-only; a failure counts as 0 so the alert still goes."""
    try:
        import sqlite3
        con = sqlite3.connect(GUILD_EVENTS_DB, timeout=5)
        try:
            row = con.execute("SELECT COUNT(*) FROM guild_events WHERE guild_id=? AND event='blocked'",
                              (str(guild_id),)).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception:
        return 0


async def _post_blocked_notice(guild) -> bool:
    """One line into the server's system channel (or the first text channel
    the bot can speak in) before leaving. Best-effort with a short timeout —
    the notice must never delay or block the exit."""
    import asyncio
    me = getattr(guild, "me", None)
    candidates = [guild.system_channel] + list(getattr(guild, "text_channels", []))
    for ch in candidates:
        if ch is None or me is None:
            continue
        try:
            perms = ch.permissions_for(me)
            if not (perms.view_channel and perms.send_messages):
                continue
            await asyncio.wait_for(ch.send(guild_blocklist.NOTICE_TEXT), timeout=5)
            return True
        except Exception:
            continue
    return False


async def _enforce_blocklist(guild, readd: bool = False) -> bool:
    """Leave `guild` if it is on the operator blocklist. True when it was.

    The public rule is torvex.app/TrustSafety ("Servers we don't serve"); the
    list itself is managed on the dashboard (/operator/blocklist), and
    cogs/guild_blocklist.py purges the archive on the way out via on_guild_remove.

    `readd=True` is the join-time path: the server just added the bot again.
    That gets the one-line notice and an email that names the attempt number
    (BlackNova re-added three times in 26 minutes on 9/27).
    """
    try:
        entry = guild_blocklist.get(guild.id)
    except Exception as e:
        print(f"[WARN] blocklist lookup failed for {guild.id}: {e}")
        return False
    if not entry:
        return False
    prior = _blocked_departures(guild.id) if readd else 0
    print(f"[GUILD] BLOCKED {guild.id} ({guild.name!r}) — {entry['reason']!r}; "
          f"{'re-add #%d, ' % prior if readd and prior else ''}leaving")
    bot.blocked_leaving.add(guild.id)
    posted = None
    if readd:
        posted = await _post_blocked_notice(guild)
        print(f"[GUILD] BLOCKED notice {'posted' if posted else 'NOT posted (no channel)'} in {guild.id}")
    # Every refusal emails the operator (Paul, 9/27: "send me the notification
    # email"); the attempt number tells them apart, and the notice result is
    # reported as it happened — the 9/27 17:19 email claimed a notice that was
    # never posted because the email was built before the attempt.
    bot.loop.create_task(_send_guild_alert("blocked", guild, reason=entry["reason"], readd=prior,
                                           notice_posted=posted))
    try:
        await guild.leave()
    except Exception as e:
        print(f"[WARN] could not leave blocked guild {guild.id}: {e}")
    return True


@bot.event
async def on_guild_join(guild):
    print(f"[GUILD] JOINED {guild.id} ({guild.name!r}) members={getattr(guild, 'member_count', '?')} — now in {len(bot.guilds)} guilds")
    _record_guild_event("join", guild)
    if await _enforce_blocklist(guild, readd=True):
        return   # the departure is recorded as `blocked` below; one alert, not two
    bot.loop.create_task(_send_guild_alert("join", guild))


@bot.event
async def on_guild_remove(guild):
    # Fires for a kick, a ban, an admin removing the integration, AND for the
    # guild being deleted outright — Discord does not tell us which. A departure
    # WE caused (blocklist) is the one case we do know, so it gets its own row
    # and no alert — the operator either did it or was already emailed.
    blocked = guild.id in bot.blocked_leaving
    bot.blocked_leaving.discard(guild.id)
    print(f"[GUILD] {'LEFT BLOCKED' if blocked else 'REMOVED FROM'} {guild.id} ({guild.name!r}) members={getattr(guild, 'member_count', '?')} — now in {len(bot.guilds)} guilds")
    _record_guild_event("blocked" if blocked else "remove", guild)
    if not blocked:
        bot.loop.create_task(_send_guild_alert("remove", guild))


@bot.event
async def on_disconnect():
    await _post_status("🔴 Bot is going offline for a restart. Back in a moment!")

import sys
sys.stdout.reconfigure(line_buffering=True)

bot.run(os.getenv("DISCORD_TOKEN"))
