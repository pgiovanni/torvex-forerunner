"""Level reward roles + MEE6 leveling migration for peepos-reclaimer.

Replaces MEE6's levels plugin: the "Level N+" reward roles are handed out on
server levelups (remove-old-give-new — a member holds only their highest
tier), and /levelroles import-mee6 pulls every member's XP/level/message-count
straight off MEE6's public leaderboard API into guild_xp — including MEE6's
own role-reward config, so nothing is guessed from role names.

Server levels use MEE6's exact curve (cogs/economy.py mee6_* helpers), so the
imported XP lands on the identical level number and pacing stays MEE6-gradual.
Import policy: GREATEST() everywhere — nobody's XP, level, or message count
ever goes DOWN, re-running the import is safe.

The economy cog dispatches "peepo_guild_level_up" on every server levelup;
this cog listens and swaps reward roles. Grants are done by THIS bot, which
anti-nuke exempts. /levelroles sync repairs drift (missed levelups, manual
role edits, and the initial post-import sweep).
"""
import os
import sys
import asyncio
import logging

import aiohttp
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cogs.economy import mee6_level_from_xp  # noqa: E402
from utils import mee6_jobs  # noqa: E402

log = logging.getLogger("level_roles")

DB_DSN = os.getenv("DISCORD_DB_DSN", "")
MEE6_API = "https://mee6.xyz/api/plugins/levels/leaderboard/{gid}?limit=1000&page={page}"

JOB_POLL_SECONDS = 10
LEADERBOARD_HELP = (
    "MEE6's leaderboard for this server isn't public (or MEE6 was never here). "
    "Turn it on: MEE6 dashboard → Levels → **Make leaderboard public**, then try again."
)


class Mee6Error(Exception):
    """Fetch failed for a reason worth showing a server owner verbatim."""


async def fetch_mee6(guild_id: int) -> tuple[list, list]:
    """Every page of a guild's MEE6 leaderboard → (players, role_rewards).

    Read-only and unauthenticated: MEE6 serves this publicly per guild, so it
    works for ANY server, and the dashboard can call the same endpoint to build
    a preview without the bot being involved at all.
    """
    players, rewards = [], []
    async with aiohttp.ClientSession() as session:
        page, retries = 0, 0
        while True:
            url = MEE6_API.format(gid=guild_id, page=page)
            async with session.get(url) as r:
                if r.status == 429:
                    retries += 1
                    if retries > 5:
                        raise Mee6Error("MEE6's API keeps rate-limiting us — "
                                        "try again in a few minutes.")
                    await asyncio.sleep(5 * retries)
                    continue
                if r.status in (401, 403, 404):
                    raise Mee6Error(LEADERBOARD_HELP)
                if r.status != 200:
                    raise Mee6Error(f"MEE6's API returned HTTP {r.status}.")
                data = await r.json()
            retries = 0
            if page == 0:
                rewards = data.get("role_rewards") or []
            batch = data.get("players") or []
            players.extend(batch)
            if len(batch) < 1000:
                break
            page += 1
    return players, rewards


def pick_reward(level: int, mapping: dict[int, int]) -> int | None:
    """Highest-threshold role a member of `level` qualifies for, or None.
    `mapping` is {level_threshold: role_id}."""
    best_lvl, best_role = -1, None
    for lvl, rid in mapping.items():
        if level >= lvl > best_lvl:
            best_lvl, best_role = lvl, rid
    return best_role


def role_changes(member_role_ids: set[int], level: int, mapping: dict[int, int]) -> tuple[list[int], list[int]]:
    """(to_add, to_remove) role ids so the member holds exactly their highest
    qualifying reward role and no other reward role."""
    want = pick_reward(level, mapping)
    all_rewards = set(mapping.values())
    to_add = [want] if want is not None and want not in member_role_ids else []
    to_remove = [rid for rid in all_rewards & member_role_ids if rid != want]
    return to_add, to_remove


class LevelRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pool: asyncpg.Pool | None = None

    async def cog_load(self):
        self.pool = await asyncpg.create_pool(DB_DSN)
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS level_roles (
                guild_id TEXT NOT NULL,
                level    INT  NOT NULL,
                role_id  TEXT NOT NULL,
                PRIMARY KEY (guild_id, level)
            )
        """)

        try:
            mee6_jobs.ensure()
            back = mee6_jobs.requeue_stuck()
            if back:
                log.info("level_roles: requeued %s stranded migration job(s)", back)
            self.migration_runner.start()
        except Exception as e:
            # A broken job queue must never stop reward roles from working.
            log.error("level_roles: migration queue unavailable (%s) — "
                      "dashboard migrations disabled, slash commands unaffected", e)

    async def cog_unload(self):
        self.migration_runner.cancel()
        if self.pool:
            await self.pool.close()

    # ── dashboard-queued migrations ───────────────────────────────────────────
    @tasks.loop(seconds=JOB_POLL_SECONDS)
    async def migration_runner(self):
        """Run one MEE6 migration queued from the dashboard.

        The dashboard can't do this itself: the import writes Postgres and the
        sweep assigns roles, both of which live in the bot process. Same split
        as reaction-role panels — the web side only ever marks work.

        One job per tick on purpose. A sweep sleeps 1s per adjusted member, so a
        big server can take minutes; running them serially keeps the bot from
        hammering Discord on behalf of several servers at once.
        """
        try:
            job = mee6_jobs.claim_next()
        except Exception as e:
            log.warning("level_roles: couldn't read migration queue: %s", e)
            return
        if not job:
            return

        gid = int(job["guild_id"])
        guild = self.bot.get_guild(gid)
        if guild is None:
            mee6_jobs.finish(job["job_id"], ok=False,
                             detail="The bot isn't in that server any more.")
            return
        try:
            players, rewards = await fetch_mee6(gid)
            if not players:
                mee6_jobs.finish(job["job_id"], ok=False,
                                 detail="MEE6 returned zero players — nothing to import.")
                return
            res = await self._write_import(guild, players, rewards,
                                           bool(job["create_missing"]))
            swept = {"checked": 0, "changed": 0}
            if job["run_sync"]:
                swept = await self._sweep(guild)

            detail = (f"Imported {res['imported']:,} members and "
                      f"{len(res['rewards'])} reward tier(s).")
            if res["created"]:
                detail += f" Recreated {len(res['created'])} role(s)."
            if res["missing"]:
                detail += (f" Skipped {len(res['missing'])} tier(s) whose role is gone: "
                           f"{', '.join(res['missing'])}.")
            if job["run_sync"]:
                detail += (f" Swept {swept['checked']} members, "
                           f"{swept['changed']} adjusted.")
            mee6_jobs.finish(job["job_id"], ok=True, detail=detail,
                             imported=res["imported"], tiers=len(res["rewards"]),
                             roles_created=len(res["created"]),
                             synced=swept["changed"])
            log.info("level_roles: migration job %s for %s done — %s",
                     job["job_id"], gid, detail)
        except Mee6Error as e:
            mee6_jobs.finish(job["job_id"], ok=False, detail=str(e))
        except Exception as e:
            log.exception("level_roles: migration job %s failed", job["job_id"])
            mee6_jobs.finish(job["job_id"], ok=False,
                             detail=f"Unexpected error: {type(e).__name__}: {e}")

    @migration_runner.before_loop
    async def _before_migrations(self):
        await self.bot.wait_until_ready()

    async def _mapping(self, guild_id: int) -> dict[int, int]:
        rows = await self.pool.fetch(
            "SELECT level, role_id FROM level_roles WHERE guild_id = $1", str(guild_id)
        )
        return {r["level"]: int(r["role_id"]) for r in rows}

    async def _apply(self, member: discord.Member, level: int, mapping: dict[int, int], reason: str) -> tuple[int, int]:
        """Give the highest qualifying reward role, strip the rest. Returns
        (#added, #removed); silently skips roles that are gone or above me."""
        to_add, to_remove = role_changes({r.id for r in member.roles}, level, mapping)
        guild = member.guild
        me_top = guild.me.top_role
        added = removed = 0
        add_roles = [r for rid in to_add if (r := guild.get_role(rid)) and r < me_top]
        rem_roles = [r for rid in to_remove if (r := guild.get_role(rid)) and r < me_top]
        try:
            if add_roles:
                await member.add_roles(*add_roles, reason=reason)
                added = len(add_roles)
            if rem_roles:
                await member.remove_roles(*rem_roles, reason=reason)
                removed = len(rem_roles)
        except discord.HTTPException as e:
            # Forbidden, member left mid-sweep (404), transient 5xx — never let
            # one member kill a whole sweep.
            log.warning("level_roles: couldn't adjust %s in %s: %s", member.id, guild.id, e)
        return added, removed

    # ── migration internals (shared by the slash command and dashboard jobs) ──
    async def _write_import(self, guild: discord.Guild, players: list, rewards: list,
                            create_missing: bool = False) -> dict:
        """Write a fetched MEE6 leaderboard into guild_xp + level_roles.

        GREATEST() on every column: nobody's XP, level or message count can go
        DOWN, so re-running is safe and a partially-applied job can simply be
        replayed.

        `create_missing` remakes reward roles MEE6 references that no longer
        exist. Off by default — silently minting roles in someone's server is
        not a thing to do without them asking.
        """
        await self.pool.executemany("""
            INSERT INTO guild_xp (discord_id, guild_id, xp, level, message_count)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (discord_id, guild_id) DO UPDATE SET
                xp            = GREATEST(guild_xp.xp,            EXCLUDED.xp),
                level         = GREATEST(guild_xp.level,         EXCLUDED.level),
                message_count = GREATEST(guild_xp.message_count, EXCLUDED.message_count)
        """, [
            (str(p["id"]), str(guild.id), int(p["xp"]),
             max(int(p["level"]), mee6_level_from_xp(int(p["xp"]))),
             int(p.get("message_count", 0)))
            for p in players
        ])

        imported_rewards, missing, created = [], [], []
        for rr in rewards:
            lvl = int(rr["rank"])
            name = rr["role"].get("name") or f"Level {lvl}"
            role = guild.get_role(int(rr["role"]["id"]))
            if role is None and create_missing:
                try:
                    role = await guild.create_role(
                        name=name, reason=f"MEE6 migration: level {lvl} reward")
                    created.append(f"{lvl}→{role.name}")
                except discord.HTTPException as e:
                    log.warning("level_roles: couldn't create role for level %s in %s: %s",
                                lvl, guild.id, e)
            if role is None:
                missing.append(f"level {lvl} ({name})")
                continue
            await self.pool.execute("""
                INSERT INTO level_roles (guild_id, level, role_id) VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, level) DO UPDATE SET role_id = $3
            """, str(guild.id), lvl, str(role.id))
            imported_rewards.append(f"{lvl}→{role.name}")

        top = max(players, key=lambda p: int(p["xp"])) if players else None
        return {"imported": len(players), "rewards": imported_rewards,
                "missing": missing, "created": created, "top": top}

    async def _sweep(self, guild: discord.Guild, progress=None) -> dict:
        """Give every member their highest qualifying reward role, strip the rest.

        Survives anything one member can throw — a sweep that dies halfway
        leaves the server in a worse state than not running it.
        """
        mapping = await self._mapping(guild.id)
        if not mapping:
            return {"mapping": False, "checked": 0, "changed": 0, "added": 0, "removed": 0}

        rows = await self.pool.fetch(
            "SELECT discord_id, xp, level FROM guild_xp WHERE guild_id = $1", str(guild.id))
        levels = {r["discord_id"]: max(r["level"], mee6_level_from_xp(r["xp"])) for r in rows}

        # Snapshot the member list so cache mutations mid-sweep can't skip the tail.
        checked = changed = added = removed = 0
        for member in list(guild.members):
            if member.bot:
                continue
            checked += 1
            try:
                a, r = await self._apply(member, levels.get(str(member.id), 0),
                                         mapping, reason="levelroles sync")
            except Exception as e:
                log.warning("level_roles: sync skipped %s: %s", member.id, e)
                continue
            if a or r:
                changed += 1
                added += a
                removed += r
                await asyncio.sleep(1)   # only throttle when we actually hit the API
            if progress and changed and changed % 25 == 0:
                await progress(checked, changed)
        return {"mapping": True, "checked": checked, "changed": changed,
                "added": added, "removed": removed}

    @commands.Cog.listener()
    async def on_peepo_guild_level_up(self, member: discord.Member, guild: discord.Guild, new_level: int):
        try:
            mapping = await self._mapping(guild.id)
            if mapping:
                await self._apply(member, new_level, mapping, reason=f"level {new_level} reward")
        except Exception as e:
            log.error("level_roles: levelup apply failed for %s: %s", member.id, e)

    # ── /levelroles ───────────────────────────────────────────────────────────
    group = app_commands.Group(
        name="levelroles", description="Level reward roles + MEE6 XP import (admin)",
        default_permissions=discord.Permissions(administrator=True), guild_only=True)

    @group.command(name="import-mee6", description="Import XP, levels & role rewards from MEE6's leaderboard API")
    @app_commands.describe(
        preview="Show what would be imported without writing anything",
        create_missing="Recreate reward roles MEE6 references that no longer exist")
    @app_commands.checks.has_permissions(administrator=True)
    async def import_mee6(self, interaction: discord.Interaction,
                          preview: bool = False, create_missing: bool = False):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        try:
            players, rewards = await fetch_mee6(guild.id)
        except Mee6Error as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        if not players:
            await interaction.followup.send(
                "MEE6 returned zero players — nothing to import.", ephemeral=True)
            return

        top = max(players, key=lambda p: int(p["xp"]))
        if preview:
            gone = [f"level {int(rr['rank'])} ({rr['role'].get('name', '?')})"
                    for rr in rewards if guild.get_role(int(rr["role"]["id"])) is None]
            msg = (f"**Dry run — nothing was written.**\n"
                   f"Found **{len(players):,}** members with XP and "
                   f"**{len(rewards)}** reward tier(s).\n"
                   f"Top: {top['username']} — level {top['level']}, {int(top['xp']):,} XP.")
            if gone:
                msg += (f"\n⚠️ {len(gone)} tier(s) whose role was deleted: "
                        f"{', '.join(gone)}\nRe-run with `create_missing:True` to remake them.")
            msg += "\n\nRun again without `preview` to import."
            await interaction.followup.send(msg[:1900], ephemeral=True)
            return

        res = await self._write_import(guild, players, rewards, create_missing)
        msg = (f"✅ Imported **{res['imported']:,}** members from MEE6 (top: "
               f"{top['username']} — level {top['level']}, {int(top['xp']):,} XP). "
               f"Nobody was lowered.\n"
               f"**Role rewards:** {', '.join(res['rewards']) or 'none found'}")
        if res["created"]:
            msg += f"\n🆕 Recreated: {', '.join(res['created'])}"
        if res["missing"]:
            msg += (f"\n⚠️ Rewards whose role is gone (skipped): "
                    f"{', '.join(res['missing'])}")
        msg += "\n\nNow run `/levelroles sync` to hand out the right Level N+ role to everyone."
        await interaction.followup.send(msg[:1900], ephemeral=True)

    @group.command(name="sync", description="Sweep all members: give each their highest Level N+ role, strip the rest")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        async def progress(checked, changed):
            try:
                await interaction.edit_original_response(
                    content=f"⏳ Syncing… {checked} checked, {changed} adjusted.")
            except discord.HTTPException:
                pass  # token expired on a long sweep — keep sweeping

        res = await self._sweep(guild, progress=progress)
        if not res["mapping"]:
            await interaction.followup.send(
                "No level roles configured — run `/levelroles import-mee6` or "
                "`/levelroles set`.", ephemeral=True)
            return

        summary = (f"✅ Sync done: **{res['checked']}** members checked, "
                   f"**{res['changed']}** adjusted "
                   f"({res['added']} roles added, {res['removed']} removed).")
        print(f"[level_roles] {guild.id} sync complete: {res['checked']} checked, "
              f"{res['changed']} adjusted (+{res['added']}/-{res['removed']})", flush=True)
        try:
            await interaction.followup.send(summary, ephemeral=True)
        except discord.HTTPException:
            pass  # completion already in the journal

    @group.command(name="list", description="Show the level → role reward map")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_rewards(self, interaction: discord.Interaction):
        mapping = await self._mapping(interaction.guild.id)
        if not mapping:
            await interaction.response.send_message("No level roles configured.", ephemeral=True)
            return
        lines = [f"**Level {lvl}+** → <@&{rid}>" for lvl, rid in sorted(mapping.items())]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @group.command(name="set", description="Set the reward role for a level")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(level="level threshold (e.g. 10)", role="role to award at that level")
    async def set_reward(self, interaction: discord.Interaction, level: int, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                f"{role.mention} is above my top role — move **Peepo's Reclaimer** higher first.", ephemeral=True)
            return
        await self.pool.execute("""
            INSERT INTO level_roles (guild_id, level, role_id) VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, level) DO UPDATE SET role_id = $3
        """, str(interaction.guild.id), level, str(role.id))
        await interaction.response.send_message(f"✅ Level **{level}+** now rewards {role.mention}.", ephemeral=True)

    @group.command(name="remove", description="Remove the reward role for a level")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_reward(self, interaction: discord.Interaction, level: int):
        res = await self.pool.execute(
            "DELETE FROM level_roles WHERE guild_id = $1 AND level = $2",
            str(interaction.guild.id), level)
        if res.endswith("0"):
            await interaction.response.send_message(f"No reward configured for level {level}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"🗑️ Level {level} reward removed.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You need the **Administrator** permission to use this."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(LevelRoles(bot))
