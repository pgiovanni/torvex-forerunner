"""Level reward roles + MEE6 leveling migration for peepos-reclaimer.

Replaces MEE6's levels plugin: the "Level N+" reward roles are handed out on
server levelups (remove-old-give-new — a member holds only their highest
tier), and the MEE6 import pulls every member's XP/level/message-count
straight off MEE6's public leaderboard API into guild_xp — including MEE6's
own role-reward config, so nothing is guessed from role names.

Server levels use MEE6's exact curve (cogs/economy.py mee6_* helpers), so the
imported XP lands on the identical level number and pacing stays MEE6-gradual.
Import policy: GREATEST() everywhere — nobody's XP, level, or message count
ever goes DOWN, re-running the import is safe.

The economy cog dispatches "peepo_guild_level_up" on every server levelup;
this cog listens and swaps reward roles. Grants are done by THIS bot, which
anti-nuke exempts. A sweep repairs drift (missed levelups, manual role edits,
and the initial post-import pass).

**NO SLASH COMMANDS (2026-09-20).** `/levelroles` — import-mee6, sync, list,
set, remove, transfer — was deleted whole; the forerunner dashboard's Levels
page is the only surface. Admin config belongs on the website (the slash tree
is for players, and it was at 98/100). The split that makes it work:

  * the tier map lives in security_config.db (`level_tiers`), which both the
    bot and the dashboard read and write, so editing a tier is an ordinary
    config form with no round-trip through Discord;
  * anything needing Postgres or the Discord API — the import, the reward-role
    sweep, an account XP merge — is queued as a job in level_jobs.db and run
    by `job_runner` below (utils/mee6_jobs.py).
"""
import os
import sys
import json
import asyncio
import logging

import aiohttp
import asyncpg
import discord
from discord.ext import commands, tasks

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cogs.economy import mee6_level_from_xp  # noqa: E402
from utils import mee6_jobs  # noqa: E402
from utils.security_config import get_config, set_config  # noqa: E402

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


# Where an account XP merge is allowed. Moving XP between accounts is an
# operator tool, not something every server's admins get (Paul 9/15: "it
# shouldn't be given to everyone yet, just a tool for us"). Unset = the
# operator's own guild. The dashboard hides the form for guilds outside this
# list AND the job runner refuses them here, because the list lives in the
# bot's environment and a web process is not where that call gets decided.
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
                log.info("level_roles: requeued %s stranded level job(s)", back)
            self.job_runner.start()
        except Exception as e:
            # A broken job queue must never stop reward roles from working:
            # levelups keep handing out tiers from config either way.
            log.error("level_roles: job queue unavailable (%s) — imports, sweeps "
                      "and merges are disabled until it comes back", e)

        # Seed every guild's tier map out of Postgres NOW rather than on its
        # next levelup: until a guild is seeded the dashboard's tier editor
        # would show an empty list for a server that really does have tiers.
        # bot.guilds is empty inside setup_hook, so this waits for ready.
        self._seed_task = self.bot.loop.create_task(self._seed_all())

    async def _seed_all(self):
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                await self._mapping(guild.id)      # seeds + marks on first call
            except Exception as e:
                log.warning("level_roles: couldn't seed tiers for %s: %s", guild.id, e)

    async def cog_unload(self):
        self.job_runner.cancel()
        task = getattr(self, "_seed_task", None)
        if task:
            task.cancel()
        if self.pool:
            await self.pool.close()

    # ── dashboard-queued jobs ────────────────────────────────────────────────
    @tasks.loop(seconds=JOB_POLL_SECONDS)
    async def job_runner(self):
        """Run one job queued from the dashboard: import, sync, or transfer.

        The dashboard can't do any of them itself: they write Postgres and
        assign roles, both of which live in the bot process. Same split as
        reaction-role panels — the web side only ever marks work.

        One job per tick on purpose. A sweep sleeps 1s per adjusted member, so a
        big server can take minutes; running them serially keeps the bot from
        hammering Discord on behalf of several servers at once.
        """
        try:
            job = mee6_jobs.claim_next()
        except Exception as e:
            log.warning("level_roles: couldn't read the job queue: %s", e)
            return
        if not job:
            return

        gid = int(job["guild_id"])
        guild = self.bot.get_guild(gid)
        if guild is None:
            mee6_jobs.finish(job["job_id"], ok=False,
                             detail="The bot isn't in that server any more.")
            return
        kind = job.get("kind") or "import"
        try:
            if kind == "sync":
                await self._job_sync(job, guild)
            elif kind == "transfer":
                await self._job_transfer(job, guild)
            else:
                await self._job_import(job, guild)
        except Mee6Error as e:
            mee6_jobs.finish(job["job_id"], ok=False, detail=str(e))
        except Exception as e:
            log.exception("level_roles: %s job %s failed", kind, job["job_id"])
            mee6_jobs.finish(job["job_id"], ok=False,
                             detail=f"Unexpected error: {type(e).__name__}: {e}")

    @job_runner.before_loop
    async def _before_jobs(self):
        await self.bot.wait_until_ready()

    async def _job_import(self, job, guild: discord.Guild):
        players, rewards = await fetch_mee6(guild.id)
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
        log.info("level_roles: import job %s for %s done — %s",
                 job["job_id"], guild.id, detail)

    async def _job_sync(self, job, guild: discord.Guild):
        """Reward-role sweep on its own — what `/levelroles sync` used to do.

        Kept separate from the import rather than folded into it as "import
        with zero pages": a sweep touches nobody's XP, so an admin repairing
        drift should never have to go back out to MEE6 to do it.
        """
        res = await self._sweep(guild)
        if not res["mapping"]:
            mee6_jobs.finish(
                job["job_id"], ok=False,
                detail="No reward tiers are set for this server, so there was nothing "
                       "to hand out. Add a tier first, or migrate from MEE6.")
            return
        detail = (f"Checked {res['checked']:,} members, {res['changed']:,} adjusted "
                  f"({res['added']} role(s) added, {res['removed']} removed).")
        mee6_jobs.finish(job["job_id"], ok=True, detail=detail, synced=res["changed"])
        log.info("level_roles: sync job %s for %s done — %s",
                 job["job_id"], guild.id, detail)

    async def _job_transfer(self, job, guild: discord.Guild):
        """Merge one account's server progress into another (or preview it)."""
        try:
            payload = json.loads(job.get("payload") or "{}")
        except ValueError:
            payload = {}
        source_id, target_id = int(payload.get("source") or 0), int(payload.get("target") or 0)
        bucks, preview = bool(payload.get("bucks")), bool(payload.get("preview"))

        if guild.id not in XP_TRANSFER_GUILDS:
            mee6_jobs.finish(job["job_id"], ok=False,
                             detail="Moving XP between accounts is an operator tool and "
                                    "isn't enabled for this server.")
            return
        if not source_id or not target_id or source_id == target_id:
            mee6_jobs.finish(job["job_id"], ok=False,
                             detail="Give two different account ids.")
            return

        res = await self._transfer(guild, source_id, target_id, bucks=bucks, preview=preview)
        mee6_jobs.finish(job["job_id"], ok=res["ok"], detail=res["detail"],
                         synced=res.get("roles_changed"))
        if res["ok"] and not preview:
            print(f"[level_roles] {guild.id} xp transfer {source_id} -> {target_id}: "
                  f"{res['moved_xp']} xp, {res['moved_msgs']} msgs"
                  f"{', ' + str(res['moved_bucks']) + ' bucks' if bucks else ''} "
                  f"(queued by {job.get('requested_by')})", flush=True)

    # ── the tier map (security_config, mirrored to Postgres) ─────────────────
    async def _mapping(self, guild_id: int) -> dict[int, int]:
        """{level threshold: role_id} for a guild, from config.

        Config is the source of truth since 2026-09-20 so the dashboard can
        edit it. A guild whose tiers are still only in Postgres is seeded once,
        marked, and never read from Postgres again — otherwise deleting a tier
        on the website would be undone by the next levelup.
        """
        cfg = get_config(guild_id)
        if not cfg.get("level_tiers_seeded"):
            rows = await self.pool.fetch(
                "SELECT level, role_id FROM level_roles WHERE guild_id = $1", str(guild_id))
            seeded = {str(r["level"]): int(r["role_id"]) for r in rows}
            set_config(guild_id, level_tiers=seeded, level_tiers_seeded=1)
            if seeded:
                log.info("level_roles: seeded %s tier(s) for %s out of Postgres",
                         len(seeded), guild_id)
            return {int(k): int(v) for k, v in seeded.items()}
        return {int(k): int(v) for k, v in (cfg.get("level_tiers") or {}).items()}

    async def _set_tier(self, guild_id: int, level: int, role_id: int) -> None:
        """Point a level at a role, in config AND in the old Postgres table.

        The Postgres write is a mirror, not a read path: keeping it current
        means a rollback to a build that still reads `level_roles` finds the
        real map rather than whatever it was before the move.
        """
        tiers = await self._mapping(guild_id)
        tiers[int(level)] = int(role_id)
        set_config(guild_id, level_tiers={str(k): int(v) for k, v in tiers.items()},
                   level_tiers_seeded=1)
        await self.pool.execute("""
            INSERT INTO level_roles (guild_id, level, role_id) VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, level) DO UPDATE SET role_id = $3
        """, str(guild_id), int(level), str(role_id))

    async def _remove_tier(self, guild_id: int, level: int) -> bool:
        tiers = await self._mapping(guild_id)
        if int(level) not in tiers:
            return False
        tiers.pop(int(level))
        set_config(guild_id, level_tiers={str(k): int(v) for k, v in tiers.items()},
                   level_tiers_seeded=1)
        await self.pool.execute(
            "DELETE FROM level_roles WHERE guild_id = $1 AND level = $2",
            str(guild_id), int(level))
        return True

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
        """Write a fetched MEE6 leaderboard into guild_xp + the tier map.

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
            await self._set_tier(guild.id, lvl, role.id)
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

    # ── account XP merge (queued from the dashboard) ─────────────────────────
    async def _transfer(self, guild: discord.Guild, source_id: int, target_id: int,
                        *, bucks: bool = False, preview: bool = False) -> dict:
        """Move one account's server XP, level & messages onto another account.

        Was `/levelroles transfer`; operator-gated then and now (the caller
        checks XP_TRANSFER_GUILDS). Merging an old or alt account into a new
        one is the only place in this cog where a number goes DOWN: the source
        is emptied on purpose, because a transfer that left the XP behind would
        be a copy. Totals ADD, so a target that already chatted keeps what it
        earned. Global (cross-server) XP in discord_users is a separate system,
        untouched.

        Returns {ok, detail, ...} — `detail` is plain text for the job row, so
        it reads the same on the dashboard as it did in an ephemeral reply.
        """
        src_name = self._who(guild, source_id)
        tgt_name = self._who(guild, target_id)

        rows = await self.pool.fetch(
            "SELECT discord_id, xp, level, message_count, regular_bucks "
            "FROM guild_xp WHERE guild_id = $1 AND discord_id = ANY($2)",
            str(guild.id), [str(source_id), str(target_id)])
        by_id = {r["discord_id"]: dict(r) for r in rows}
        empty = {"xp": 0, "level": 0, "message_count": 0, "regular_bucks": 0}
        src = by_id.get(str(source_id), empty)
        dst = by_id.get(str(target_id), empty)

        if not (src["xp"] or src["message_count"] or (bucks and src["regular_bucks"])):
            return {"ok": False,
                    "detail": f"{src_name} has nothing to transfer in this server."}

        new = merged_totals(src, dst)
        moved = (f"{src['xp']:,} XP · {src['message_count']:,} messages"
                 + (f" · {src['regular_bucks']:,} bucks" if bucks else ""))
        lands = (f"{tgt_name}: level {dst['level']} → {new['level']}, "
                 f"{dst['xp']:,} → {new['xp']:,} XP, "
                 f"{dst['message_count']:,} → {new['message_count']:,} messages"
                 + (f", {dst['regular_bucks']:,} → {new['regular_bucks']:,} bucks" if bucks else ""))

        if preview:
            return {"ok": True, "preview": True,
                    "detail": (f"Dry run — nothing was written. From {src_name}: {moved}. "
                               f"{lands}. {src_name} would end at level 0 with 0 XP."),
                    "moved_xp": src["xp"], "moved_msgs": src["message_count"],
                    "moved_bucks": src["regular_bucks"] if bucks else 0}

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if bucks:
                    await conn.execute("""
                        INSERT INTO guild_xp (discord_id, guild_id, xp, level, message_count, regular_bucks)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (discord_id, guild_id) DO UPDATE SET
                            xp = $3, level = $4, message_count = $5, regular_bucks = $6
                    """, str(target_id), str(guild.id), new["xp"], new["level"],
                         new["message_count"], new["regular_bucks"])
                    await conn.execute("""
                        UPDATE guild_xp SET xp = 0, level = 0, message_count = 0, regular_bucks = 0
                        WHERE discord_id = $1 AND guild_id = $2
                    """, str(source_id), str(guild.id))
                else:
                    await conn.execute("""
                        INSERT INTO guild_xp (discord_id, guild_id, xp, level, message_count)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (discord_id, guild_id) DO UPDATE SET
                            xp = $3, level = $4, message_count = $5
                    """, str(target_id), str(guild.id), new["xp"], new["level"], new["message_count"])
                    await conn.execute("""
                        UPDATE guild_xp SET xp = 0, level = 0, message_count = 0
                        WHERE discord_id = $1 AND guild_id = $2
                    """, str(source_id), str(guild.id))

        # Reward roles follow the new numbers on BOTH sides, same as a levelup.
        notes, changed = [], 0
        mapping = await self._mapping(guild.id)
        if mapping:
            tgt_member = guild.get_member(target_id)
            if tgt_member:
                added, removed = await self._apply(
                    tgt_member, new["level"], mapping, reason=f"XP transfer from {source_id}")
                changed += added + removed
                notes.append(f"{tgt_name}: {added} reward role(s) added, {removed} removed.")
            else:
                notes.append(f"{tgt_name} isn't in this server — their reward role lands "
                             f"when they join, or on the next sweep.")
            src_member = guild.get_member(source_id)
            if src_member:
                _, stripped = await self._apply(
                    src_member, 0, mapping, reason=f"XP transferred to {target_id}")
                changed += stripped
                if stripped:
                    notes.append(f"{src_name}: {stripped} reward role(s) stripped.")
        else:
            notes.append("No level reward roles are configured in this server.")

        detail = (f"Moved {moved} from {src_name} → {tgt_name}. {lands}. "
                  f"{src_name} is now level 0 with 0 XP"
                  + (" and 0 bucks" if bucks else "") + ". " + " ".join(notes))
        return {"ok": True, "detail": detail, "roles_changed": changed,
                "moved_xp": src["xp"], "moved_msgs": src["message_count"],
                "moved_bucks": src["regular_bucks"] if bucks else 0}

    def _who(self, guild: discord.Guild, user_id: int) -> str:
        """A name for a job-row line. Falls back to the bare id rather than
        failing — an account being merged is often one that already left."""
        m = guild.get_member(user_id) or self.bot.get_user(user_id)
        return f"{m.display_name if isinstance(m, discord.Member) else m.name} ({user_id})" if m else str(user_id)


async def setup(bot):
    await bot.add_cog(LevelRoles(bot))
