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


def parse_guild_allowlist(*values):
    """Guild ids from the first env value that has any — commas or spaces.

    Same shape as RECON_GUILDS / BACKUP_GUILDS / MSGLOG_ARCHIVE_GUILDS: the
    operator grants a capability to their OWN servers from the environment,
    because it is their data and their call, not a toggle a guild admin finds
    on a dashboard.
    """
    for raw in values:
        ids = {int(g) for g in (raw or "").replace(",", " ").split() if g.strip().isdigit()}
        if ids:
            return ids
    return set()


# Where /levelroles transfer works. Moving XP between accounts is an operator
# tool, not something every server's admins get (Paul 9/15: "it shouldn't be
# given to everyone yet, just a tool for us"). Unset = the operator's own guild.
XP_TRANSFER_GUILDS = parse_guild_allowlist(
    os.environ.get("XP_TRANSFER_GUILDS"), os.environ.get("ALTGUARD_GUILD_ID"))
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


def merged_totals(src: dict, dst: dict) -> dict:
    """Destination totals after absorbing `src` — an XP transfer between two
    accounts of the same person. Progress ADDS up (both accounts really did
    chat), and the level is re-derived from the merged XP but never drops
    below a level either account already held."""
    xp = src["xp"] + dst["xp"]
    return {
        "xp": xp,
        "level": max(dst["level"], src["level"], mee6_level_from_xp(xp)),
        "message_count": src["message_count"] + dst["message_count"],
        "regular_bucks": src["regular_bucks"] + dst["regular_bucks"],
    }


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

    @group.command(name="transfer",
                   description="Move one account's server XP, level & messages onto another account")
    @app_commands.describe(
        source="account to take the XP FROM (it ends at level 0)",
        target="account to give the XP TO",
        bucks="also move this server's 💵 Server Bucks balance",
        preview="show what would move without writing anything")
    @app_commands.rename(source="from", target="to")
    @app_commands.checks.has_permissions(administrator=True)
    async def transfer(self, interaction: discord.Interaction,
                       source: discord.User, target: discord.User,
                       bucks: bool = False, preview: bool = False):
        """Merge an old/alt account's server progress into a new one.

        Operator-gated: only guilds in XP_TRANSFER_GUILDS (default: the
        operator's own) can run it, on top of the group's admin requirement.

        The ONE deliberate exception to the never-lower policy everywhere else
        in this cog: the source account is emptied (0 XP / level 0 / 0 msgs) on
        purpose — a transfer that left the XP behind would be a copy. Totals are
        ADDED, so a target that already chatted keeps what it earned. Global
        (cross-server) XP in discord_users is a separate system, untouched.
        """
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if guild.id not in XP_TRANSFER_GUILDS:
            await interaction.followup.send(
                "❌ Moving XP between accounts is an operator tool and isn't enabled for this "
                "server. Everything else in `/levelroles` works normally.", ephemeral=True)
            return
        if source.id == target.id:
            await interaction.followup.send("❌ Those are the same account.", ephemeral=True)
            return
        if source.bot or target.bot:
            await interaction.followup.send("❌ Bots don't hold server XP.", ephemeral=True)
            return

        rows = await self.pool.fetch(
            "SELECT discord_id, xp, level, message_count, regular_bucks "
            "FROM guild_xp WHERE guild_id = $1 AND discord_id = ANY($2)",
            str(guild.id), [str(source.id), str(target.id)])
        by_id = {r["discord_id"]: dict(r) for r in rows}
        empty = {"xp": 0, "level": 0, "message_count": 0, "regular_bucks": 0}
        src = by_id.get(str(source.id), empty)
        dst = by_id.get(str(target.id), empty)

        if not (src["xp"] or src["message_count"] or (bucks and src["regular_bucks"])):
            await interaction.followup.send(
                f"{source.mention} has nothing to transfer in this server.", ephemeral=True)
            return

        new = merged_totals(src, dst)
        moved = (f"**{src['xp']:,} XP** · **{src['message_count']:,}** messages"
                 + (f" · **{src['regular_bucks']:,}** 💵" if bucks else ""))
        lands = (f"{target.mention}: level **{dst['level']}** → **{new['level']}**, "
                 f"{dst['xp']:,} → **{new['xp']:,} XP**, "
                 f"{dst['message_count']:,} → **{new['message_count']:,}** messages"
                 + (f", {dst['regular_bucks']:,} → **{new['regular_bucks']:,}** 💵" if bucks else ""))

        if preview:
            await interaction.followup.send(
                f"**Dry run — nothing was written.**\nFrom {source.mention}: {moved}\n{lands}\n"
                f"{source.mention} would end at level 0 with 0 XP.\n\n"
                f"Run again without `preview` to move it.", ephemeral=True)
            return

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if bucks:
                    await conn.execute("""
                        INSERT INTO guild_xp (discord_id, guild_id, xp, level, message_count, regular_bucks)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (discord_id, guild_id) DO UPDATE SET
                            xp = $3, level = $4, message_count = $5, regular_bucks = $6
                    """, str(target.id), str(guild.id), new["xp"], new["level"],
                         new["message_count"], new["regular_bucks"])
                    await conn.execute("""
                        UPDATE guild_xp SET xp = 0, level = 0, message_count = 0, regular_bucks = 0
                        WHERE discord_id = $1 AND guild_id = $2
                    """, str(source.id), str(guild.id))
                else:
                    await conn.execute("""
                        INSERT INTO guild_xp (discord_id, guild_id, xp, level, message_count)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (discord_id, guild_id) DO UPDATE SET
                            xp = $3, level = $4, message_count = $5
                    """, str(target.id), str(guild.id), new["xp"], new["level"], new["message_count"])
                    await conn.execute("""
                        UPDATE guild_xp SET xp = 0, level = 0, message_count = 0
                        WHERE discord_id = $1 AND guild_id = $2
                    """, str(source.id), str(guild.id))

        # Reward roles follow the new numbers on BOTH sides, same as a levelup.
        notes = []
        mapping = await self._mapping(guild.id)
        if mapping:
            tgt_member = guild.get_member(target.id)
            if tgt_member:
                added, removed = await self._apply(
                    tgt_member, new["level"], mapping, reason=f"XP transfer from {source.id}")
                notes.append(f"🎖️ {target.mention}: {added} reward role(s) added, {removed} removed.")
            else:
                notes.append(f"⚠️ {target.mention} isn't in this server — their reward role "
                             f"lands when they join (or run `/levelroles sync`).")
            src_member = guild.get_member(source.id)
            if src_member:
                _, stripped = await self._apply(
                    src_member, 0, mapping, reason=f"XP transferred to {target.id}")
                if stripped:
                    notes.append(f"🎖️ {source.mention}: {stripped} reward role(s) stripped.")
        else:
            notes.append("⚠️ No level reward roles are configured in this server.")

        print(f"[level_roles] {guild.id} xp transfer {source.id} -> {target.id}: "
              f"{src['xp']} xp, {src['message_count']} msgs"
              f"{', ' + str(src['regular_bucks']) + ' bucks' if bucks else ''} "
              f"(by {interaction.user.id})", flush=True)

        msg = (f"✅ Moved {moved}\nfrom {source.mention} → {target.mention}.\n"
               f"{lands}\n{source.mention} is now level **0** with 0 XP"
               + (" and 0 💵" if bucks else "") + ".")
        if notes:
            msg += "\n" + "\n".join(notes)
        await interaction.followup.send(msg[:1900], ephemeral=True)

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
                f"{role.mention} is above my top role — move **Torvex Forerunner** higher first.", ephemeral=True)
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
