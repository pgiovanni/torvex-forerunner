"""Last to survive — the ban race. Discord side of utils/ban_race.py.

    /race start|stop|status   host controls (Manage Server)
    /vote <player>            fire this round's shot — private until the round closes
    /powerup [use] [target]   inventory, or aim an Overload / Transfuse

Everything else is buttons on the lobby and round messages, plus power-up
drops that appear in the channel mid-round (first click takes it). Buttons
carry fixed custom_ids ("lts:<action>:<race>") and are answered by the
on_interaction listener, so a restart mid-race doesn't kill the panel; the
round loop itself is resumed from the DB on the next on_ready.

Eliminations in mode "real" are actual bans, executed by the bot — the only
way members at the same role level can knock each other out. Each victim is
DM'd their elimination card WITH the return invite BEFORE the ban lands
(after it, no shared server usually means no DM), and the bot unbans every
race ban the moment a winner is declared or the host stops the race. Race
bans are marked quiet so the goodbye channel and mod-log embeds don't get
thirty cards in a night; member_events still records every one.
"""
import asyncio
import logging
import os
import random
import secrets
import sys
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import ban_race as engine  # noqa: E402
from utils.quiet_removals import mark as quiet_mark  # noqa: E402

log = logging.getLogger("ban_race")

PREFIX = "lts"
COLOR = 0xE74C3C
COLOR_ROUND = 0xF1C40F
COLOR_DROP = 0x9B59B6
COLOR_WIN = 0x2ECC71
MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)
NO_MENTIONS = discord.AllowedMentions.none()
INVITE_DAYS = 7
RACER_ROLE = "Racer"     # only this role can talk in the race channel; Join grants it
MAX_PINGS = 60
START_DELAY = 30        # seconds between the start ping and round 1 opening

MODE_CHOICES = [
    app_commands.Choice(name="ghost — no bans, eliminated players are just out (default)", value="ghost"),
    app_commands.Choice(name="real — actual bans, auto-unban when it ends", value="real"),
]
USE_CHOICES = [
    app_commands.Choice(name="Overload — take 1 damage to deal 2", value="overload"),
    app_commands.Choice(name="Transfuse — give someone 1 life, lose 1", value="transfuse"),
]

PITCH = ("You've got **{lives} lives**. There are **power-ups**. Pick who you're going for "
         "and shoot. See you after the round ends.")


# ── custom ids ────────────────────────────────────────────────────────────────

def _cid(action, race_id, extra=None):
    s = f"{PREFIX}:{action}:{race_id}"
    return f"{s}:{extra}" if extra else s


def _parse_cid(cid):
    if not cid or not cid.startswith(PREFIX + ":"):
        return None
    parts = cid.split(":")
    if len(parts) < 3:
        return None
    try:
        rid = int(parts[2])
    except ValueError:
        return None
    return parts[1], rid, (parts[3] if len(parts) > 3 else None)


# ── views ─────────────────────────────────────────────────────────────────────

def lobby_view(race_id, closed=False):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Join", emoji="🔫", style=discord.ButtonStyle.success,
                                 custom_id=_cid("join", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Leave", style=discord.ButtonStyle.secondary,
                                 custom_id=_cid("leave", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Start the race", emoji="🏁", style=discord.ButtonStyle.danger,
                                 custom_id=_cid("begin", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary,
                                 custom_id=_cid("cancel", race_id), disabled=closed))
    return v


def round_view(race_id, closed=False):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Vote", emoji="🔫", style=discord.ButtonStyle.danger,
                                 custom_id=_cid("vote", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Power-ups", emoji="🎒", style=discord.ButtonStyle.primary,
                                 custom_id=_cid("powerup", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Standings", emoji="📊", style=discord.ButtonStyle.secondary,
                                 custom_id=_cid("standings", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Close round now", emoji="⏭️", style=discord.ButtonStyle.secondary,
                                 custom_id=_cid("close", race_id), disabled=closed))
    return v


def drop_view(race_id, nonce):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="GRAB IT", emoji="⚡", style=discord.ButtonStyle.success,
                                 custom_id=_cid("drop", race_id, nonce)))
    return v


class _TargetSelect(discord.ui.View):
    """Ephemeral picker: who to aim at. One select, up to 25 living players."""

    def __init__(self, cog, race_id, kind, me, rows):
        super().__init__(timeout=120)
        self.cog, self.race_id, self.kind = cog, race_id, kind
        opts = [discord.SelectOption(label=p["name"][:100], value=p["user_id"],
                                     description=f"{p['lives']} lives · {p['kills']} kills")
                for p in engine.alive(rows) if p["user_id"] != str(me)][:25]
        label = {"shot": "Who are you going for?", "overload": "Overload — who takes 2?",
                 "transfuse": "Transfuse — who gets your life?"}[kind]
        sel = discord.ui.Select(placeholder=label, options=opts, min_values=1, max_values=1)
        sel.callback = self._pick
        self.add_item(sel)

    async def _pick(self, interaction):
        target = interaction.data["values"][0]
        text = await self.cog._do_cast(interaction.guild, self.race_id, interaction.user.id,
                                       int(target), self.kind)
        await interaction.response.edit_message(content=text, embed=None, view=None)


class _PowerupView(discord.ui.View):
    """Ephemeral inventory with a button per aimable power-up."""

    def __init__(self, cog, race_id, p, rows):
        super().__init__(timeout=120)
        self.cog, self.race_id, self.rows = cog, race_id, rows
        if p["overload"] > 0:
            b = discord.ui.Button(label=f"Use Overload ×{p['overload']}", emoji="💥",
                                  style=discord.ButtonStyle.danger)
            b.callback = self._mk("overload")
            self.add_item(b)
        if p["transfuse"] > 0:
            b = discord.ui.Button(label=f"Use Transfuse ×{p['transfuse']}", emoji="💉",
                                  style=discord.ButtonStyle.primary)
            b.callback = self._mk("transfuse")
            self.add_item(b)

    def _mk(self, kind):
        async def cb(interaction):
            v = _TargetSelect(self.cog, self.race_id, kind, interaction.user.id, self.rows)
            await interaction.response.edit_message(content=engine.POWERUPS[kind][2], embed=None, view=v)
        return cb


# ── embeds ────────────────────────────────────────────────────────────────────

def _lives_bar(n):
    return "❤️" * max(0, n) if n else "💀"


def lobby_embed(race, rows, guild_name):
    s = race["settings"]
    e = discord.Embed(title="🔫 LAST TO SURVIVE", color=COLOR,
                      description=PITCH.format(lives=s["lives"]))
    how = (f"• Rounds last **{s['round_secs'] // 60} min**; shots are secret and all land at once.\n"
           f"• Zero lives = {'**actually banned**' if s['mode'] == 'real' else 'out'}. "
           f"{'Everyone is unbanned the moment it ends, and you get the invite by DM first.' if s['mode'] == 'real' else ''}\n"
           f"• Don't vote in a round and you lose a life. AFK is not a strategy.\n"
           f"• Only racers can talk here — **Join** unlocks the channel; ghosts watch in silence.\n"
           f"• Power-ups drop in this channel. First click takes it.\n"
           f"• Last one standing wins. 🎁")
    e.add_field(name="How it works", value=how, inline=False)
    names = [p["name"] for p in rows]
    shown = ", ".join(names[:40]) + (f" … +{len(names) - 40}" if len(names) > 40 else "")
    e.add_field(name=f"Players ({len(rows)})", value=shown or "*nobody yet — hit Join*", inline=False)
    e.set_footer(text=f"{guild_name} · starts automatically at {s['min_players']} players")
    return e


def round_embed(race, rows, round_no, ends_at, sudden, extra_lines):
    n = len(engine.alive(rows))
    e = discord.Embed(title=f"⏱️ Round {round_no}", color=COLOR_ROUND,
                      description=f"**{n}** alive · closes <t:{int(ends_at)}:R>\n"
                                  f"Hit **Vote** (or `/vote`) to fire. Shots land when the round closes.")
    if sudden:
        e.add_field(name="☠️ SUDDEN DEATH", value="Shields are off. Rounds are half as long.", inline=False)
    for line in extra_lines:
        e.add_field(name="​", value=line, inline=False)
    return e


def standings_embed(race, rows):
    live, fallen = engine.standings(rows)
    e = discord.Embed(title=f"📊 Standings · round {race['round_no']}", color=COLOR)
    lines = []
    for p in live:
        tag = " 🎯" if p["bounty"] else ""
        lines.append(f"{_lives_bar(p['lives'])} **{p['name']}**{tag} · {p['kills']} kills")
    e.add_field(name=f"Alive ({len(live)})", value="\n".join(lines[:30]) or "—", inline=False)
    if fallen:
        f = [f"💀 {p['name']} · round {p['died_round']}" for p in fallen[:30]]
        e.add_field(name=f"Fallen ({len(fallen)})", value="\n".join(f), inline=False)
    e.set_footer(text="Shields are secret. Kills pay a shield; the bounty pays two shots.")
    return e


def drop_embed(kind):
    emoji, name, blurb = engine.POWERUPS[kind]
    return discord.Embed(title=f"⚡ POWER-UP DROP — {emoji} {name}", description=blurb, color=COLOR_DROP)


def elimination_dm(race, round_no, killer_id, guild_name):
    s = race["settings"]
    e = discord.Embed(title="⛔ You're out", color=COLOR,
                      description=f"Eliminated in **round {round_no}** of Last to survive in **{guild_name}**"
                                  + (f", by <@{killer_id}>." if killer_id else "."))
    if s["mode"] != "real":
        e.add_field(name="What happens now",
                    value="You're a ghost: you can watch the race channel but not talk in it. "
                          "You'll still get each round's results here.", inline=False)
    if s["mode"] == "real":
        e.add_field(name="What happens now",
                    value="You're being banned — that's the game. The bot **unbans everyone the moment the race "
                          "ends**. Keep this invite; it's how you get back in:\n"
                          + (race["invite_url"] or "*(the host will post the invite)*"),
                    inline=False)
    return e


# ── cog ───────────────────────────────────────────────────────────────────────

class BanRace(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._tasks = {}                  # race_id -> asyncio.Task
        self._running = {}                # guild_id -> race_id
        self._msgs = defaultdict(int)     # (race_id, user_id) -> messages this round
        self._drops = {}                  # nonce -> {kind, race_id, claimed}
        self._resumed = False
        self._rng = random.Random()
        engine.init()

    def cog_unload(self):
        for t in self._tasks.values():
            t.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._resumed:
            return
        self._resumed = True
        for race in engine.running_races():
            self._start_task(race)
            log.info("resumed race %s in guild %s", race["id"], race["guild_id"])

    # ── helpers ───────────────────────────────────────────────────────────────

    def _start_task(self, race):
        self._running[int(race["guild_id"])] = race["id"]
        t = self._tasks.get(race["id"])
        if t and not t.done():
            return
        self._tasks[race["id"]] = asyncio.create_task(self._run(race["id"]))

    def _bannable(self, guild, member):
        if member is None:
            return True     # not in the server: ban-by-id works
        return member.id != guild.owner_id and member.top_role < guild.me.top_role

    async def _channel_for(self, guild, race):
        return guild.get_channel(int(race["channel_id"]))

    # ── the Racer role: the channel's send permission ─────────────────────────
    # "No one can message the channel unless they click Join" (Paul, 9/11).
    # @everyone loses Send in the race channel, the Racer role gets it; Join
    # grants the role, Leave / elimination / race end take it away — so ghosts
    # can watch but not talk, and the channel is silent between races.

    async def _ensure_racer_role(self, guild):
        role = discord.utils.get(guild.roles, name=RACER_ROLE)
        if role is None:
            role = await guild.create_role(name=RACER_ROLE, mentionable=False,
                                           colour=discord.Colour(COLOR),
                                           reason="Last to survive — racers can talk in the race channel")
        return role

    async def _lock_channel(self, channel, role):
        await channel.set_permissions(channel.guild.default_role, send_messages=False,
                                      send_messages_in_threads=False,
                                      reason="Last to survive — only racers talk here")
        await channel.set_permissions(role, send_messages=True, send_messages_in_threads=True,
                                      reason="Last to survive — racers talk here")

    def _racer_role(self, guild, race):
        rid = race["settings"].get("racer_role_id")
        return guild.get_role(int(rid)) if rid else None

    async def _set_racer(self, guild, race, uid, on):
        role = self._racer_role(guild, race)
        member = guild.get_member(int(uid))
        if role is None or member is None:
            return
        try:
            if on:
                await member.add_roles(role, reason="Last to survive — joined the race")
            elif role in member.roles:
                await member.remove_roles(role, reason="Last to survive — out of the race")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("race %s: racer role %s for %s failed: %s", race["id"], on, uid, e)

    async def _strip_all_racers(self, guild, race):
        for p in engine.players(race["id"]):
            await self._set_racer(guild, race, p["user_id"], False)
            await asyncio.sleep(0.3)

    async def _post_lines(self, channel, title, lines, color, content=None, extra_embed=None):
        """Send lines as one or more embeds, never over Discord's limits."""
        chunks, cur = [], []
        size = 0
        for ln in lines:
            if size + len(ln) + 1 > 3800 and cur:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(ln)
            size += len(ln) + 1
        if cur:
            chunks.append(cur)
        first = None
        for i, ch in enumerate(chunks):
            e = discord.Embed(title=title if i == 0 else f"{title} (cont.)", color=color,
                              description="\n".join(ch))
            embeds = [e]
            if extra_embed is not None and i == len(chunks) - 1:
                embeds.append(extra_embed)
            msg = await channel.send(content=content if i == 0 else None, embeds=embeds,
                                     allowed_mentions=MENTIONS)
            first = first or msg
        return first

    async def _dm(self, user_id, **kwargs):
        try:
            user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
            await user.send(**kwargs)
            return True
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return False

    async def _do_cast(self, guild, race_id, shooter_id, target_id, kind):
        """Shared by /vote, /powerup and the buttons. Returns the ephemeral text."""
        race = engine.get_race(race_id)
        if not race or race["status"] != "running":
            return "There's no round to shoot in right now."
        p = engine.player(race_id, shooter_id)
        t = engine.player(race_id, target_id)
        err = engine.cast_error(p, t, kind)
        if err:
            return err
        rn = race["round_no"]
        if kind == "shot" and p["shots"] <= 0:
            if engine.retarget(race_id, rn, shooter_id, target_id):
                return f"🔁 Re-aimed at **{t['name']}**. Still resolves <t:{int(race['round_ends_at'])}:R>."
            return "You're out of shots this round and have nothing to re-aim. Grab a drop."
        engine.spend(p, kind)
        engine.update_player(race_id, shooter_id, shots=p["shots"], overload=p["overload"],
                             transfuse=p["transfuse"])
        engine.cast(race_id, rn, shooter_id, target_id, kind)
        when = f"<t:{int(race['round_ends_at'])}:R>"
        if kind == "shot":
            return (f"🔫 Locked on **{t['name']}**. Lands {when}. "
                    f"Shots left this round: **{p['shots']}**." +
                    (" Fire again to use them." if p["shots"] > 0 else ""))
        if kind == "overload":
            return f"💥 Overload armed at **{t['name']}** — you burn 1, they take 2. Lands {when}."
        return f"💉 Transfuse set for **{t['name']}** — they gain 1, you lose 1. Lands {when}."

    # ── round loop ────────────────────────────────────────────────────────────

    async def _run(self, race_id):
        try:
            if (engine.get_race(race_id) or {}).get("round_no", 1) == 0:
                await asyncio.sleep(START_DELAY)     # the countdown promised by the start ping
            while True:
                race = engine.get_race(race_id)
                if not race or race["status"] != "running":
                    return
                s = race["settings"]
                guild = self.bot.get_guild(int(race["guild_id"]))
                channel = guild and await self._channel_for(guild, race)
                if channel is None:
                    engine.update_race(race_id, status="aborted", finished_at=time.time())
                    return
                rows = engine.players(race_id)
                round_no = race["round_no"] + 1
                sudden = engine.is_sudden_death(rows, s["sudden_death_at"])
                extra = engine.open_round(rows, round_no, race["purge_round"], s["shot_cap"])
                engine.save_players(race_id, rows)
                secs = max(30, s["round_secs"] // 2 if sudden else s["round_secs"])
                ends = time.time() + secs
                for k in [k for k in self._msgs if k[0] == race_id]:
                    del self._msgs[k]
                engine.update_race(race_id, round_no=round_no, round_ends_at=ends)
                msg = await channel.send(embed=round_embed(race, rows, round_no, ends, sudden, extra),
                                         view=round_view(race_id))
                engine.update_race(race_id, round_msg_id=str(msg.id))

                drop_at = None
                if self._rng.random() < s["drop_chance"]:
                    drop_at = time.time() + self._rng.uniform(0.15, 0.7) * secs
                while True:
                    race = engine.get_race(race_id)
                    if race["status"] != "running":
                        return
                    now = time.time()
                    if now >= (race["round_ends_at"] or 0):
                        break
                    if drop_at and now >= drop_at:
                        drop_at = None
                        asyncio.create_task(self._spawn_drop(race_id, channel, s))
                    await asyncio.sleep(min(2.0, max(0.2, race["round_ends_at"] - now)))
                try:
                    await msg.edit(view=round_view(race_id, closed=True))
                except discord.HTTPException:
                    pass
                await self._close_round(race_id, round_no, guild, channel)
                race = engine.get_race(race_id)
                if race["status"] != "running":
                    return
                await asyncio.sleep(6)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("race %s loop crashed", race_id)

    async def _close_round(self, race_id, round_no, guild, channel):
        race = engine.get_race(race_id)
        s = race["settings"]
        rows = engine.players(race_id)
        shot_rows = engine.shots(race_id, round_no)
        sudden = engine.is_sudden_death(rows, s["sudden_death_at"])
        msgs = {p["user_id"]: self._msgs.get((race_id, p["user_id"]), 0) for p in rows}
        res = engine.resolve_round(rows, shot_rows, round_no, self._rng,
                                   backfire=s["backfire"], sudden=sudden,
                                   storm=round_no >= s["storm_from_round"], msgs=msgs,
                                   max_lives=s["lives"], shot_cap=s["shot_cap"])
        engine.save_players(race_id, rows)
        for ln in res["lines"]:
            engine.log(race_id, round_no, ln)

        involved = []
        for sh in shot_rows:
            involved += [sh["shooter_id"], sh["target_id"]]
        involved += res["dead"]
        seen, pings = set(), []
        for uid in involved:
            if uid not in seen:
                seen.add(uid)
                pings.append(f"<@{uid}>")
        content = " ".join(pings[:MAX_PINGS]) or None
        lines = res["lines"] or ["🕊️ Nobody fired. The storm noticed."]
        board = None if res["winners"] else standings_embed(engine.get_race(race_id), rows)
        await self._post_lines(channel, f"💥 Round {round_no} — results", lines, COLOR, content=content,
                               extra_embed=board)

        for uid in res["dead"]:
            await self._eliminate(guild, channel, race, uid, round_no, res["killers"].get(uid))

        # the dead still watch: DM earlier casualties the round's results
        for p in rows:
            if not p["alive"] and p["user_id"] not in res["dead"]:
                e = discord.Embed(title=f"👻 Round {round_no} — from the grave", color=COLOR,
                                  description="\n".join(lines)[:4000])
                await self._dm(p["user_id"], embed=e)
                await asyncio.sleep(0.4)

        if res["winners"]:
            await self._finish(race_id, guild, channel, res["winners"])

    async def _eliminate(self, guild, channel, race, uid, round_no, killer_id):
        member = guild.get_member(int(uid))
        s = race["settings"]
        await self._set_racer(guild, race, uid, False)
        if member:
            await self._dm(uid, embed=elimination_dm(race, round_no, killer_id, guild.name))
        if s["mode"] != "real":
            return
        quiet_mark(uid)
        try:
            await guild.ban(discord.Object(id=int(uid)),
                            reason=f"Last to survive: eliminated round {round_no} (auto-unban when the race ends)",
                            delete_message_seconds=0)
            engine.update_player(race["id"], uid, banned=1)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("race %s: could not ban %s: %s", race["id"], uid, e)
            try:
                await channel.send(f"⚠️ Couldn't ban <@{uid}> ({e.__class__.__name__}) — they're out of the "
                                   f"race regardless.", allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    async def _unban_all(self, guild, race):
        n = 0
        for p in engine.players(race["id"]):
            if p["banned"]:
                try:
                    await guild.unban(discord.Object(id=int(p["user_id"])),
                                      reason="Last to survive: race over — everyone comes back")
                    n += 1
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    pass
                engine.update_player(race["id"], p["user_id"], banned=0)
                await asyncio.sleep(0.3)
        return n

    async def _finish(self, race_id, guild, channel, winners):
        engine.update_race(race_id, status="finished", finished_at=time.time(), winner_ids=winners)
        self._running.pop(guild.id, None)
        race = engine.get_race(race_id)
        rows = engine.players(race_id)
        restored = await self._unban_all(guild, race)
        await self._strip_all_racers(guild, race)
        live, fallen = engine.standings(rows)
        killers = sorted(rows, key=lambda p: -p["kills"])[:3]
        if len(winners) == 1:
            title, desc = "🏆 LAST TO SURVIVE", f"<@{winners[0]}> is the last one standing after **{race['round_no']}** rounds."
        else:
            title = "🤝 Mutual destruction"
            desc = ("Nobody's left. Died in the final round together: " + ", ".join(f"<@{w}>" for w in winners)
                    + " — host settles it.")
        e = discord.Embed(title=title, description=desc, color=COLOR_WIN)
        e.add_field(name="Top killers",
                    value="\n".join(f"{p['kills']} — {p['name']}" for p in killers if p["kills"]) or "—",
                    inline=True)
        if fallen:
            e.add_field(name="Runner-up", value=fallen[0]["name"], inline=True)
        if race["settings"]["mode"] == "real":
            e.add_field(name="Everyone's back", value=f"Unbanned **{restored}** — the invite is in their DMs.",
                        inline=False)
        e.set_footer(text="Host: pay the winner. 🎁")
        await channel.send(content=" ".join(f"<@{w}>" for w in winners), embed=e, allowed_mentions=MENTIONS)
        for p in rows:
            if not p["alive"]:
                await self._dm(p["user_id"], content=f"Race over in **{guild.name}** — you're unbanned. "
                                                     f"{race['invite_url'] or ''}".strip())
                await asyncio.sleep(0.3)

    async def _abort(self, guild, channel, race, by):
        engine.update_race(race["id"], status="aborted", finished_at=time.time())
        self._running.pop(guild.id, None)
        t = self._tasks.pop(race["id"], None)
        if t and not t.done():
            t.cancel()
        restored = await self._unban_all(guild, race)
        await self._strip_all_racers(guild, race)
        try:
            await channel.send(f"🛑 Race stopped by {by.mention}."
                               + (f" Unbanned **{restored}**." if restored else ""),
                               allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass

    async def _spawn_drop(self, race_id, channel, s):
        kind = engine.roll_drop(self._rng)
        nonce = secrets.token_hex(4)
        self._drops[nonce] = {"kind": kind, "race_id": race_id, "claimed": None}
        try:
            msg = await channel.send(embed=drop_embed(kind), view=drop_view(race_id, nonce))
        except discord.HTTPException:
            self._drops.pop(nonce, None)
            return
        await asyncio.sleep(s["drop_window"])
        d = self._drops.pop(nonce, None)
        if d and not d["claimed"]:
            try:
                await msg.edit(embed=discord.Embed(title="💨 The drop vanished", color=COLOR_DROP,
                                                   description="Too slow, all of you."), view=None)
            except discord.HTTPException:
                pass

    # ── /race ─────────────────────────────────────────────────────────────────

    race = app_commands.Group(name="race", description="Last to survive — the ban race (host controls)",
                              guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @race.command(name="start", description="Open a lobby. Posts in #last-to-survive (created if missing) or the channel you pick.")
    @app_commands.describe(lives="Lives per player (default 3)", round_minutes="Minutes per round (default 3)",
                           mode="ghost = no bans (default); real = actual bans, auto-unban at the end",
                           min_account_days="Minimum account age to enter (default 7)",
                           min_players="Players needed before the race can start (default 3)",
                           channel="Where the race runs (default: #last-to-survive, created if missing)")
    @app_commands.choices(mode=MODE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def race_start(self, interaction: discord.Interaction,
                         lives: app_commands.Range[int, 1, 5] = 3,
                         round_minutes: app_commands.Range[int, 1, 30] = 3,
                         mode: app_commands.Choice[str] = None,
                         min_account_days: app_commands.Range[int, 0, 365] = 7,
                         min_players: app_commands.Range[int, 3, 500] = 3,
                         channel: discord.TextChannel = None):
        guild = interaction.guild
        mode_v = mode.value if mode else engine.DEFAULTS["mode"]
        if engine.active_race(guild.id):
            return await interaction.response.send_message(
                "A race is already on here — `/race stop` it first.", ephemeral=True)
        me = guild.me.guild_permissions
        if mode_v == "real" and not me.ban_members:
            return await interaction.response.send_message(
                "I need **Ban Members** to run a real race (that's the whole game).", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        if channel is None:
            channel = discord.utils.get(guild.text_channels, name=engine.DEFAULT_CHANNEL)
        if channel is None:
            if not me.manage_channels:
                return await interaction.followup.send(
                    f"No #{engine.DEFAULT_CHANNEL} here and I can't create channels — pass `channel:`.",
                    ephemeral=True)
            try:
                channel = await guild.create_text_channel(
                    engine.DEFAULT_CHANNEL, reason=f"Last to survive — opened by {interaction.user}",
                    topic="Last to survive: lives, secret shots, power-up drops. Zero lives = banned. "
                          "Everyone comes back when it ends.")
            except discord.HTTPException as e:
                return await interaction.followup.send(f"Couldn't create the channel: {e}", ephemeral=True)

        warn = []
        racer_role_id = None
        try:
            role = await self._ensure_racer_role(guild)
            racer_role_id = role.id
            await self._lock_channel(channel, role)
        except (discord.Forbidden, discord.HTTPException) as e:
            warn.append(f"⚠️ Couldn't set up the Racer role / channel lock ({e.__class__.__name__}) — "
                        f"I need Manage Roles and Manage Channels; anyone can talk there until then.")

        invite_url = None
        try:
            inv = await channel.create_invite(max_age=INVITE_DAYS * 86400, max_uses=0, unique=True,
                                              reason="Last to survive — return invite for the eliminated")
            invite_url = inv.url
        except discord.HTTPException:
            pass

        try:
            race = engine.create_race(guild.id, channel.id, interaction.user.id, settings={
                "lives": lives, "round_secs": round_minutes * 60, "mode": mode_v,
                "min_account_days": min_account_days, "min_players": min_players,
                "racer_role_id": racer_role_id})
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        engine.update_race(race["id"], invite_url=invite_url)
        race = engine.get_race(race["id"])
        msg = await channel.send(embed=lobby_embed(race, [], guild.name), view=lobby_view(race["id"]))
        engine.update_race(race["id"], lobby_msg_id=str(msg.id))
        if not invite_url:
            warn.append("⚠️ Couldn't mint a return invite (need Create Invite in that channel) — post one yourself before it starts.")
        note = ("\n" + "\n".join(warn)) if warn else ""
        await interaction.followup.send(f"Lobby's open in {channel.mention}. Hit **Start the race** on it when "
                                        f"enough people have joined.{note}", ephemeral=True)

    @race.command(name="stop", description="Stop the race. Unbans everyone it banned.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def race_stop(self, interaction: discord.Interaction):
        race = engine.active_race(interaction.guild.id)
        if not race:
            return await interaction.response.send_message("No race running here.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        channel = await self._channel_for(interaction.guild, race) or interaction.channel
        await self._abort(interaction.guild, channel, race, interaction.user)
        await interaction.followup.send("Stopped.", ephemeral=True)

    @race.command(name="status", description="Standings for the race in progress.")
    async def race_status(self, interaction: discord.Interaction):
        race = engine.active_race(interaction.guild.id)
        if not race:
            return await interaction.response.send_message("No race running here.", ephemeral=True)
        rows = engine.players(race["id"])
        if race["status"] == "lobby":
            return await interaction.response.send_message(
                embed=lobby_embed(race, rows, interaction.guild.name), ephemeral=True)
        await interaction.response.send_message(embed=standings_embed(race, rows), ephemeral=True)

    # ── /vote and /powerup ────────────────────────────────────────────────────

    @app_commands.command(name="vote", description="Last to survive: fire this round's shot at a player.")
    @app_commands.describe(player="Who you're going for")
    @app_commands.guild_only()
    async def vote(self, interaction: discord.Interaction, player: discord.Member):
        race = engine.active_race(interaction.guild.id)
        if not race or race["status"] != "running":
            return await interaction.response.send_message("No round to shoot in right now.", ephemeral=True)
        text = await self._do_cast(interaction.guild, race["id"], interaction.user.id, player.id, "shot")
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="powerup", description="Last to survive: your power-ups — or aim an Overload / Transfuse.")
    @app_commands.describe(use="Which power-up to use (leave empty to see what you hold)",
                           player="Who it's aimed at")
    @app_commands.choices(use=USE_CHOICES)
    @app_commands.guild_only()
    async def powerup(self, interaction: discord.Interaction, use: app_commands.Choice[str] = None,
                      player: discord.Member = None):
        race = engine.active_race(interaction.guild.id)
        if not race or race["status"] != "running":
            return await interaction.response.send_message("No race running right now.", ephemeral=True)
        if use and player:
            text = await self._do_cast(interaction.guild, race["id"], interaction.user.id, player.id, use.value)
            return await interaction.response.send_message(text, ephemeral=True)
        if use and not player:
            return await interaction.response.send_message("Pick a `player:` to aim it at.", ephemeral=True)
        await self._send_inventory(interaction, race)

    async def _send_inventory(self, interaction, race):
        p = engine.player(race["id"], interaction.user.id)
        if not p:
            return await interaction.response.send_message("You're not in this race.", ephemeral=True)
        if not p["alive"]:
            return await interaction.response.send_message("You're out — power-ups are for the living.", ephemeral=True)
        rows = engine.players(race["id"])
        e = discord.Embed(title="🎒 Your kit", color=COLOR_DROP)
        e.add_field(name="Lives", value=_lives_bar(p["lives"]), inline=True)
        e.add_field(name="Shots banked", value=str(p["shots"]), inline=True)
        e.add_field(name="Shield", value="🛡️ held" if p["shield"] else "none", inline=True)
        e.add_field(name="Overload", value=f"💥 ×{p['overload']}", inline=True)
        e.add_field(name="Transfuse", value=f"💉 ×{p['transfuse']}", inline=True)
        e.add_field(name="Kills", value=str(p["kills"]), inline=True)
        e.set_footer(text="Shields and extra shots work on their own. Overload and Transfuse need a target.")
        view = _PowerupView(self, race["id"], p, rows)
        await interaction.response.send_message(embed=e, view=view if view.children else None,
                                                ephemeral=True)

    # ── buttons ───────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component or interaction.guild is None:
            return
        parsed = _parse_cid((interaction.data or {}).get("custom_id"))
        if not parsed:
            return
        action, race_id, extra = parsed
        race = engine.get_race(race_id)
        if not race or str(interaction.guild.id) != race["guild_id"]:
            return await interaction.response.send_message("That race is gone.", ephemeral=True)
        try:
            handler = getattr(self, f"_btn_{action}", None)
            if handler:
                await handler(interaction, race, extra)
        except discord.HTTPException as e:
            log.warning("race button %s failed: %s", action, e)

    def _is_host(self, interaction, race):
        return (str(interaction.user.id) == race["host_id"]
                or interaction.user.guild_permissions.manage_guild)

    async def _refresh_lobby(self, interaction, race, closed=False):
        rows = engine.players(race["id"])
        try:
            await interaction.message.edit(embed=lobby_embed(race, rows, interaction.guild.name),
                                           view=lobby_view(race["id"], closed=closed))
        except discord.HTTPException:
            pass

    async def _btn_join(self, interaction, race, _):
        if race["status"] != "lobby":
            return await interaction.response.send_message("The lobby's closed.", ephemeral=True)
        s = race["settings"]
        m = interaction.user
        err = engine.join_error(m.created_at.timestamp(), time.time(), s["min_account_days"],
                                is_bot=m.bot, bannable=self._bannable(interaction.guild, m), mode=s["mode"])
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        if engine.player(race["id"], m.id):
            return await interaction.response.send_message("You're in already.", ephemeral=True)
        engine.join(race["id"], m.id, m.display_name, s["lives"])
        rows = engine.players(race["id"])
        need = s.get("min_players", engine.MIN_PLAYERS)
        await interaction.response.send_message(
            f"You're in. {s['lives']} lives. Don't trust anyone. 🔫"
            + (f" ({len(rows)}/{need} — it starts the moment the lobby fills.)" if len(rows) < need else ""),
            ephemeral=True)
        await self._set_racer(interaction.guild, race, m.id, True)
        if len(rows) >= need:
            # the lobby is full: no waiting on the host, it starts now
            await self._begin(interaction.guild, interaction.channel, interaction.message, race, rows)
        else:
            await self._refresh_lobby(interaction, race)

    async def _btn_leave(self, interaction, race, _):
        if race["status"] != "lobby":
            return await interaction.response.send_message("Too late to leave — the race is on.", ephemeral=True)
        if engine.leave(race["id"], interaction.user.id):
            await interaction.response.send_message("Out of the lobby.", ephemeral=True)
            await self._set_racer(interaction.guild, race, interaction.user.id, False)
            await self._refresh_lobby(interaction, race)
        else:
            await interaction.response.send_message("You weren't in.", ephemeral=True)

    async def _btn_begin(self, interaction, race, _):
        if not self._is_host(interaction, race):
            return await interaction.response.send_message("Host only.", ephemeral=True)
        if race["status"] != "lobby":
            return await interaction.response.send_message("Already started.", ephemeral=True)
        rows = engine.players(race["id"])
        need = race["settings"].get("min_players", engine.MIN_PLAYERS)
        if len(rows) < need:
            return await interaction.response.send_message(
                f"Need at least {need} players ({len(rows)} in).", ephemeral=True)
        await interaction.response.send_message("🏁 Go.", ephemeral=True)
        await self._begin(interaction.guild, interaction.channel, interaction.message, race, rows)

    async def _begin(self, guild, channel, lobby_msg, race, rows):
        """Flip the lobby to running, ping everyone in, start the round loop.
        Shared by the host's Start button and the auto-start on a full lobby;
        the status check makes a double trigger harmless."""
        if engine.get_race(race["id"])["status"] != "lobby":
            return
        purge = engine.pick_purge_round(self._rng, len(rows))
        engine.update_race(race["id"], status="running", started_at=time.time(), purge_round=purge)
        race = engine.get_race(race["id"])
        try:
            await lobby_msg.edit(embed=lobby_embed(race, rows, guild.name),
                                 view=lobby_view(race["id"], closed=True))
        except discord.HTTPException:
            pass
        opens = int(time.time() + START_DELAY)
        await channel.send(
            content=" ".join(f"<@{p['user_id']}>" for p in rows[:MAX_PINGS]),
            embed=discord.Embed(title="🏁 THE RACE IS ON", color=COLOR,
                                description=f"**{len(rows)}** players. {PITCH.format(lives=race['settings']['lives'])}\n\n"
                                            f"⏳ **Round 1 opens <t:{opens}:R>.**"),
            allowed_mentions=MENTIONS)
        self._start_task(race)

    async def _btn_cancel(self, interaction, race, _):
        if not self._is_host(interaction, race):
            return await interaction.response.send_message("Host only.", ephemeral=True)
        if race["status"] != "lobby":
            return await interaction.response.send_message("Use `/race stop` once it's running.", ephemeral=True)
        engine.update_race(race["id"], status="aborted", finished_at=time.time())
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        await self._refresh_lobby(interaction, engine.get_race(race["id"]), closed=True)
        await self._strip_all_racers(interaction.guild, race)

    async def _btn_vote(self, interaction, race, _):
        if race["status"] != "running":
            return await interaction.response.send_message("No round open.", ephemeral=True)
        rows = engine.players(race["id"])
        p = engine.player(race["id"], interaction.user.id)
        if not p or not p["alive"]:
            return await interaction.response.send_message("You're not in this round.", ephemeral=True)
        others = [q for q in engine.alive(rows) if q["user_id"] != str(interaction.user.id)]
        if not others:
            return await interaction.response.send_message("Nobody left to shoot.", ephemeral=True)
        note = "" if len(others) <= 25 else "\n(Showing 25 — use `/vote` for anyone else.)"
        await interaction.response.send_message(
            f"Shots banked: **{p['shots']}**. Pick a target.{note}",
            view=_TargetSelect(self, race["id"], "shot", interaction.user.id, rows), ephemeral=True)

    async def _btn_powerup(self, interaction, race, _):
        if race["status"] != "running":
            return await interaction.response.send_message("No round open.", ephemeral=True)
        await self._send_inventory(interaction, race)

    async def _btn_standings(self, interaction, race, _):
        rows = engine.players(race["id"])
        await interaction.response.send_message(embed=standings_embed(race, rows), ephemeral=True)

    async def _btn_close(self, interaction, race, _):
        if not self._is_host(interaction, race):
            return await interaction.response.send_message("Host only.", ephemeral=True)
        if race["status"] != "running":
            return await interaction.response.send_message("No round open.", ephemeral=True)
        engine.update_race(race["id"], round_ends_at=time.time())
        await interaction.response.send_message("Closing the round.", ephemeral=True)

    async def _btn_drop(self, interaction, race, nonce):
        d = self._drops.get(nonce)
        if not d or d["race_id"] != race["id"]:
            return await interaction.response.send_message("Gone.", ephemeral=True)
        p = engine.player(race["id"], interaction.user.id)
        if not p or not p["alive"]:
            return await interaction.response.send_message("Power-ups are for players still alive.", ephemeral=True)
        if d["claimed"]:
            return await interaction.response.send_message("Too slow.", ephemeral=True)
        d["claimed"] = str(interaction.user.id)
        line = engine.grant(p, d["kind"], race["settings"]["shot_cap"])
        engine.update_player(race["id"], p["user_id"], shots=p["shots"], shield=p["shield"],
                             overload=p["overload"], transfuse=p["transfuse"])
        emoji, name, _ = engine.POWERUPS[d["kind"]]
        await interaction.response.send_message(f"{emoji} **{name}** is yours.", ephemeral=True)
        try:
            await interaction.message.edit(
                embed=discord.Embed(title=f"⚡ {emoji} {name} — taken", description=line, color=COLOR_DROP),
                view=None)
        except discord.HTTPException:
            pass

    # ── activity for the storm ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        rid = self._running.get(message.guild.id)
        if rid:
            self._msgs[(rid, str(message.author.id))] += 1


async def setup(bot):
    await bot.add_cog(BanRace(bot))
