import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import aiohttp
import os
import time
import random
import logging
from datetime import date

log = logging.getLogger("economy")

DB_DSN = os.getenv("DISCORD_DB_DSN", "")
TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")
_API_HEADERS = {"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}

DAILY_COIN_BONUS = 5


async def _award_daily_coins(discord_id: str):
    """Award the daily coin bonus to a player's CoinBalance via the API."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{TORVEX_API_URL}/api/bot/game/add-coins",
                json={"discordId": discord_id, "amount": DAILY_COIN_BONUS, "reason": "daily_bonus"},
                headers=_API_HEADERS
            ) as r:
                if r.status >= 400:
                    log.error(f"daily coin award failed for {discord_id}: {r.status}")
    except Exception as e:
        log.error(f"daily coin award error for {discord_id}: {e}")


async def _api(method: str, path: str, **kwargs):
    url = f"{TORVEX_API_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=_API_HEADERS, **kwargs) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {}
                if r.status >= 400:
                    log.error(f"{method} {path} → {r.status} | {data}")
                return r.status, data
    except Exception as e:
        log.error(f"{method} {path} → connection error: {e}")
        return 0, {}

DAILY_CAP = 200
BUCKS_PER_MESSAGE = 1
XP_PER_MESSAGE = 10
PEEPOS_GUILD_ID = 1215140346800119868

STORE_ITEMS = [
    {"id": "nitro_basic",   "name": "Discord Nitro Basic",  "emoji": "💎", "price": 7_500,   "description": "1 month of Nitro Basic ($2.99)"},
    {"id": "nitro",         "name": "Discord Nitro",        "emoji": "✨", "price": 25_000,  "description": "1 month of Nitro ($9.99)"},
    {"id": "robux_400",     "name": "400 Robux",            "emoji": "🎮", "price": 12_500,  "description": "400 Robux ($4.99)"},
    {"id": "robux_800",     "name": "800 Robux",            "emoji": "🎮", "price": 25_000,  "description": "800 Robux ($9.99)"},
    {"id": "robux_1700",    "name": "1,700 Robux",          "emoji": "🎮", "price": 50_000,  "description": "1,700 Robux ($19.99)"},
    {"id": "robux_4500",    "name": "4,500 Robux",          "emoji": "🎮", "price": 125_000, "description": "4,500 Robux ($49.99)"},
    {"id": "robux_10000",   "name": "10,000 Robux",         "emoji": "🎮", "price": 250_000, "description": "10,000 Robux ($99.99)"},
]


def xp_for_level(level: int) -> int:
    return int(500 * (1.3 ** (level - 1)))

def level_from_xp(xp: int) -> int:
    level = 1
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


# ── MEE6-parity curve for SERVER (guild) levels ───────────────────────────────
# MEE6's formula: XP to go from level L to L+1 = 5L² + 50L + 100, levels start
# at 0. Server XP uses this curve so XP imported from MEE6's leaderboard API
# lands on the exact same level number and future levelups pace identically.
# Global level keeps the old exponential curve — it feeds RPG stat multipliers
# and is a separate system.
GUILD_XP_MIN = 15
GUILD_XP_MAX = 25
GUILD_XP_COOLDOWN = 60  # seconds — MEE6 awards XP at most once per minute

def mee6_xp_for_level(level: int) -> int:
    """Cumulative XP required to REACH `level` (level 0 = 0 XP)."""
    l = max(level, 0)
    return (5 * l * (l - 1) * (2 * l - 1)) // 6 + 25 * l * (l - 1) + 100 * l

def mee6_level_from_xp(xp: int) -> int:
    level = 0
    while xp >= mee6_xp_for_level(level + 1):
        level += 1
    return level


class PermsFixView(discord.ui.View):
    """Dropdown + button for /check-perms fix flow."""

    def __init__(self, me: discord.Member, blocked: list[discord.TextChannel]):
        super().__init__(timeout=120)
        self.me = me
        self.blocked = blocked
        self.excluded_ids: set[int] = set()

        select = discord.ui.Select(
            placeholder="Channels to keep blocked (e.g. #mod-chat) — leave blank to fix all",
            min_values=0,
            max_values=len(blocked),
            options=[
                discord.SelectOption(label=f"#{c.name}", value=str(c.id))
                for c in blocked
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

        fix_btn = discord.ui.Button(label="Apply Fix", style=discord.ButtonStyle.green, emoji="🔧")
        fix_btn.callback = self._on_fix
        self.add_item(fix_btn)

    async def _on_select(self, interaction: discord.Interaction):
        self.excluded_ids = {int(v) for v in interaction.data["values"]}
        count = len(self.blocked) - len(self.excluded_ids)
        await interaction.response.defer()
        # Update button label to reflect pending changes
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.label = f"Apply Fix ({count} channel{'s' if count != 1 else ''})"
        await interaction.edit_original_response(view=self)

    async def _on_fix(self, interaction: discord.Interaction):
        await interaction.response.defer()
        overwrite = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
        fixed, missing_access, other_errors = 0, 0, 0
        for channel in self.blocked:
            if channel.id in self.excluded_ids:
                continue
            try:
                await channel.set_permissions(self.me, overwrite=overwrite, reason="check-perms fix")
                fixed += 1
            except discord.Forbidden as e:
                if e.code == 50001:
                    missing_access += 1
                else:
                    other_errors += 1
            except Exception:
                other_errors += 1

        skipped = len(self.excluded_ids)
        lines = []
        if fixed:
            lines.append(f"✅ Fixed **{fixed}** channel(s).")
        if skipped:
            lines.append(f"⏭️ Skipped **{skipped}** (kept blocked by your choice).")
        if missing_access:
            # Look for an existing bots role to assign
            bot_role_names = {"bots", "bot", "robots", "verified bots"}
            bots_role = discord.utils.find(
                lambda r: r.name.lower() in bot_role_names,
                self.me.guild.roles
            )
            if bots_role and bots_role not in self.me.roles:
                try:
                    await self.me.add_roles(bots_role, reason="check-perms: assigned existing bots role")
                    lines.append(
                        f"\n🤖 Found and assigned the **{bots_role.name}** role to the bot. "
                        "If that role already has access to the right channels, the bot should be able to read them now. "
                        "Run `/check-perms` again to verify."
                    )
                except Exception:
                    lines.append(
                        f"\n⚠️ Found a **{bots_role.name}** role but couldn't assign it — "
                        "assign it to the bot manually in Server Settings → Members."
                    )
            else:
                lines.append(
                    f"\n❌ **{missing_access} channel(s) couldn't be fixed automatically.**\n"
                    "This server has all channels private — Discord won't let the bot edit "
                    "permissions on channels it can't already see.\n\n"
                    "**To fix, pick one:**\n"
                    "**A)** Give the bot **Administrator** in Server Settings → Roles → [bot role].\n"
                    "**B)** Create a role (e.g. `Bots`) allowed in the right channels and assign it to the bot.\n"
                    "**C)** Manually add the bot role to each channel's allow list."
                )
        if other_errors:
            lines.append(f"⚠️ Failed on **{other_errors}** for an unexpected reason — check bot has Manage Roles.")

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content="\n".join(lines) or "Nothing to do.", view=self)


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pool: asyncpg.Pool | None = None
        # (user_id, guild_id) → monotonic ts of last guild-XP award (MEE6 pacing)
        self._guild_xp_last: dict[tuple[str, str], float] = {}

    async def cog_load(self):
        self.pool = await asyncpg.create_pool(DB_DSN)
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS discord_users (
                discord_id        TEXT PRIMARY KEY,
                username          TEXT NOT NULL DEFAULT '',
                peepo_bucks       BIGINT NOT NULL DEFAULT 0,
                xp                BIGINT NOT NULL DEFAULT 0,
                level             INT NOT NULL DEFAULT 1,
                message_count     BIGINT NOT NULL DEFAULT 0,
                daily_bucks_date  DATE,
                daily_bucks_count INT NOT NULL DEFAULT 0,
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Add columns for upgrading from old schema
        await self.pool.execute("""
            ALTER TABLE discord_users
                ADD COLUMN IF NOT EXISTS daily_bucks_date         DATE,
                ADD COLUMN IF NOT EXISTS daily_bucks_count        INT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS daily_regular_bucks_count INT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS levelup_notifs           BOOLEAN NOT NULL DEFAULT TRUE,
                ADD COLUMN IF NOT EXISTS regular_bucks            BIGINT NOT NULL DEFAULT 0
        """)
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id        TEXT PRIMARY KEY,
                levelup_notifs  BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS guild_xp (
                discord_id                TEXT NOT NULL,
                guild_id                  TEXT NOT NULL,
                xp                        BIGINT NOT NULL DEFAULT 0,
                level                     INT NOT NULL DEFAULT 1,
                message_count             BIGINT NOT NULL DEFAULT 0,
                regular_bucks             BIGINT NOT NULL DEFAULT 0,
                daily_regular_bucks_count INT NOT NULL DEFAULT 0,
                daily_regular_bucks_date  DATE,
                PRIMARY KEY (discord_id, guild_id)
            )
        """)
        await self.pool.execute("""
            ALTER TABLE guild_xp
                ADD COLUMN IF NOT EXISTS regular_bucks             BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS daily_regular_bucks_count INT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS daily_regular_bucks_date  DATE
        """)
        # Register all current guilds in guild_settings
        for guild in self.bot.guilds:
            await self.pool.execute("""
                INSERT INTO guild_settings (guild_id, levelup_notifs)
                VALUES ($1, TRUE)
                ON CONFLICT DO NOTHING
            """, str(guild.id))

        # Backfill Peepos server from global xp (only if no rows exist yet for that guild)
        peepos_guild = str(PEEPOS_GUILD_ID)
        existing = await self.pool.fetchval(
            "SELECT COUNT(*) FROM guild_xp WHERE guild_id = $1", peepos_guild
        )
        if existing == 0:
            await self.pool.execute("""
                INSERT INTO guild_xp (discord_id, guild_id, xp, level, message_count)
                SELECT discord_id, $1, xp, level, message_count
                FROM discord_users
                ON CONFLICT DO NOTHING
            """, peepos_guild)

    async def cog_unload(self):
        if self.pool:
            await self.pool.close()

    async def get_or_create(self, user: discord.User | discord.Member) -> asyncpg.Record:
        await self.pool.execute(
            "INSERT INTO discord_users (discord_id, username) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            str(user.id), user.display_name
        )
        return await self.pool.fetchrow(
            "SELECT * FROM discord_users WHERE discord_id = $1", str(user.id)
        )

    async def record_message(self, user: discord.User | discord.Member, guild_id: int | None) -> tuple:
        """Award bucks + XP.
        Returns (new_level, leveled_up, levelup_notifs, guild_level, guild_leveled_up)."""
        row = await self.get_or_create(user)
        today = date.today()

        is_new_day = row["daily_bucks_date"] != today
        peepo_count   = 0 if is_new_day else row["daily_bucks_count"]
        regular_count = 0 if is_new_day else row["daily_regular_bucks_count"]

        if is_new_day:
            await _award_daily_coins(str(user.id))

        in_peepos = guild_id == PEEPOS_GUILD_ID
        peepo_to_award   = BUCKS_PER_MESSAGE if (in_peepos and peepo_count < DAILY_CAP) else 0
        regular_to_award = BUCKS_PER_MESSAGE if regular_count < DAILY_CAP else 0

        old_level = row["level"]
        new_xp = row["xp"] + XP_PER_MESSAGE
        new_level = level_from_xp(new_xp)

        await self.pool.execute("""
            UPDATE discord_users SET
                peepo_bucks              = peepo_bucks   + $1,
                regular_bucks            = regular_bucks + $2,
                xp                       = $3,
                level                    = $4,
                message_count            = message_count + 1,
                daily_bucks_date         = $5,
                daily_bucks_count        = CASE WHEN daily_bucks_date = $5 THEN daily_bucks_count        + $6 ELSE $6 END,
                daily_regular_bucks_count = CASE WHEN daily_bucks_date = $5 THEN daily_regular_bucks_count + $7 ELSE $7 END,
                username                 = $8,
                updated_at               = NOW()
            WHERE discord_id = $9
        """,
            peepo_to_award, regular_to_award,
            new_xp, new_level, today,
            peepo_to_award, regular_to_award,
            user.display_name, str(user.id)
        )

        # Track XP + regular bucks per guild for local leaderboards.
        # Server XP paces like MEE6: 15-25 XP per message, at most once a minute,
        # levels on the MEE6 curve, and a level never goes DOWN (import policy).
        g_new_level  = None
        g_leveled_up = False
        if guild_id:
            guild_xp_row = await self.pool.fetchrow(
                "SELECT xp, level, regular_bucks, daily_regular_bucks_count, daily_regular_bucks_date FROM guild_xp WHERE discord_id = $1 AND guild_id = $2",
                str(user.id), str(guild_id)
            )
            g_old_xp    = guild_xp_row["xp"]    if guild_xp_row else 0
            g_old_level = guild_xp_row["level"] if guild_xp_row else 0

            cd_key  = (str(user.id), str(guild_id))
            now_ts  = time.monotonic()
            g_xp_gain = 0 if now_ts - self._guild_xp_last.get(cd_key, -GUILD_XP_COOLDOWN) < GUILD_XP_COOLDOWN \
                        else random.randint(GUILD_XP_MIN, GUILD_XP_MAX)
            if g_xp_gain:
                self._guild_xp_last[cd_key] = now_ts

            g_new_xp     = g_old_xp + g_xp_gain
            g_new_level  = max(g_old_level, mee6_level_from_xp(g_new_xp))
            g_leveled_up = g_new_level > g_old_level

            g_is_new_day       = (not guild_xp_row) or (guild_xp_row["daily_regular_bucks_date"] != today)
            g_regular_count    = 0 if g_is_new_day else (guild_xp_row["daily_regular_bucks_count"] or 0)
            g_regular_to_award = BUCKS_PER_MESSAGE if g_regular_count < DAILY_CAP else 0

            # $6 = g_regular_to_award for BIGINT regular_bucks
            # $7 = g_regular_to_award for INT daily_regular_bucks_count (avoids type conflict)
            # $8 = today (date)
            await self.pool.execute("""
                INSERT INTO guild_xp (discord_id, guild_id, xp, level, message_count, regular_bucks, daily_regular_bucks_count, daily_regular_bucks_date)
                VALUES ($1, $2, $3, $4, 1, $6, $7, $8)
                ON CONFLICT (discord_id, guild_id) DO UPDATE SET
                    xp                        = guild_xp.xp + $5,
                    level                     = $4,
                    message_count             = guild_xp.message_count + 1,
                    regular_bucks             = guild_xp.regular_bucks + $6,
                    daily_regular_bucks_count = CASE WHEN guild_xp.daily_regular_bucks_date = $8 THEN guild_xp.daily_regular_bucks_count + $7 ELSE $7 END,
                    daily_regular_bucks_date  = $8
            """, str(user.id), str(guild_id), g_new_xp, g_new_level, g_xp_gain, g_regular_to_award, g_regular_to_award, today)

        return (new_level, new_level > old_level, row["levelup_notifs"], g_new_level, g_leveled_up)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.pool.execute("""
            INSERT INTO guild_settings (guild_id, levelup_notifs)
            VALUES ($1, TRUE)
            ON CONFLICT DO NOTHING
        """, str(guild.id))
        log.info(f"Joined guild: {guild.name} ({guild.id})")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        guild_id = message.guild.id if message.guild else None
        print(f"[economy] on_message: user={message.author.id} guild={guild_id}", flush=True)
        try:
            new_level, leveled_up, user_notifs, g_level, g_leveled_up = await self.record_message(message.author, guild_id)

            # Server level is the announced/reward level (MEE6 parity); the
            # level_roles cog listens for this to swap Level N+ reward roles.
            if g_leveled_up and guild_id:
                self.bot.dispatch("peepo_guild_level_up", message.author, message.guild, g_level)
                if user_notifs:
                    guild_row = await self.pool.fetchrow(
                        "SELECT levelup_notifs FROM guild_settings WHERE guild_id = $1", str(guild_id)
                    )
                    guild_notifs = guild_row["levelup_notifs"] if guild_row else True
                    if guild_notifs:
                        await message.channel.send(
                            f"⬆️ {message.author.mention} leveled up to **Level {g_level}**!"
                        )

            await _api("POST", "/api/bot/game/sync-level", json={
                "discordId": str(message.author.id),
                "newLevel": new_level,
            })
        except Exception as e:
            print(f"[economy] on_message error: {e}", flush=True)

    # ── /balance ──────────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="Check your Peepo Bucks and level.")
    @app_commands.describe(user="Check another user's balance (optional)")
    async def balance(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        row = await self.get_or_create(target)
        today = date.today()
        is_today = row["daily_bucks_date"] == today
        daily_peepo = row["daily_bucks_count"] if is_today else 0

        guild_row = None
        if interaction.guild_id:
            guild_row = await self.pool.fetchrow(
                "SELECT xp, level, regular_bucks, daily_regular_bucks_count, daily_regular_bucks_date FROM guild_xp WHERE discord_id = $1 AND guild_id = $2",
                str(target.id), str(interaction.guild_id)
            )

        embed = discord.Embed(title=f"{target.display_name}'s Stats", color=0xF4C430)
        embed.add_field(name="💰 Peepo Bucks", value=f"{row['peepo_bucks']:,}", inline=True)

        if guild_row:
            g_xp    = guild_row["xp"]
            # MEE6 curve; never show lower than the stored level (import policy)
            g_level = max(guild_row["level"], mee6_level_from_xp(g_xp))
            g_ct    = mee6_xp_for_level(g_level)
            g_prog  = max(g_xp - g_ct, 0)
            g_next  = mee6_xp_for_level(g_level + 1) - g_ct
            g_is_today   = guild_row["daily_regular_bucks_date"] == today
            g_daily_reg  = guild_row["daily_regular_bucks_count"] if g_is_today else 0
            embed.add_field(name="💵 Server Bucks",  value=f"{guild_row['regular_bucks']:,}", inline=True)
            embed.add_field(name="⭐ Server Level",   value=str(g_level),                      inline=True)
            embed.add_field(name="📈 Server XP",      value=f"{g_prog:,} / {g_next:,}",        inline=True)
            embed.add_field(name="🌍 Global Level",   value=str(row["level"]),                  inline=True)
            embed.add_field(name="📨 Messages",       value=f"{row['message_count']:,}",         inline=True)
            embed.add_field(name="📅 Daily 💰",       value=f"{daily_peepo:,} / {DAILY_CAP:,}", inline=True)
            embed.add_field(name="📅 Server 💵",      value=f"{g_daily_reg:,} / {DAILY_CAP:,}", inline=True)
        else:
            xp       = row["xp"]
            level    = level_from_xp(xp)
            g_ct     = 0 if level == 1 else xp_for_level(level)
            progress = xp - g_ct
            next_xp  = xp_for_level(level + 1) - g_ct
            daily_regular = row["daily_regular_bucks_count"] if is_today else 0
            embed.add_field(name="💵 Regular Bucks",  value=f"{row['regular_bucks']:,}",        inline=True)
            embed.add_field(name="⭐ Level",           value=str(level),                          inline=True)
            embed.add_field(name="📨 Messages",        value=f"{row['message_count']:,}",         inline=True)
            embed.add_field(name="📈 XP Progress",     value=f"{progress:,} / {next_xp:,}",      inline=True)
            embed.add_field(name="📅 Daily 💰",        value=f"{daily_peepo:,} / {DAILY_CAP:,}", inline=True)
            embed.add_field(name="📅 Daily 💵",        value=f"{daily_regular:,} / {DAILY_CAP:,}", inline=True)
        await interaction.response.send_message(embed=embed)

    # ── /rank ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="rank", description="Server rank, level, total XP, and XP needed for the next level.")
    @app_commands.describe(user="Check another member's rank (optional)")
    async def rank(self, interaction: discord.Interaction, user: discord.Member = None):
        if not interaction.guild_id:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        target = user or interaction.user
        if target.bot:
            await interaction.response.send_message("Bots don't earn XP.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        row = await self.pool.fetchrow(
            "SELECT xp, level, message_count FROM guild_xp WHERE discord_id = $1 AND guild_id = $2",
            str(target.id), guild_id
        )
        if not row:
            await interaction.response.send_message(
                f"**{target.display_name}** hasn't earned any XP in this server yet.", ephemeral=True
            )
            return

        xp = row["xp"]
        # MEE6 curve; never show lower than the stored level (import policy)
        level    = max(row["level"], mee6_level_from_xp(xp))
        floor_xp = mee6_xp_for_level(level)
        needed   = mee6_xp_for_level(level + 1) - floor_xp
        prog     = min(max(xp - floor_xp, 0), needed)

        # Rank among current non-bot members — same population as /chat-levels local
        member_ids = [str(m.id) for m in interaction.guild.members if not m.bot]
        ahead = await self.pool.fetchval(
            "SELECT COUNT(*) FROM guild_xp WHERE guild_id = $1 AND discord_id = ANY($2) AND xp > $3",
            guild_id, member_ids, xp
        )
        ranked = await self.pool.fetchval(
            "SELECT COUNT(*) FROM guild_xp WHERE guild_id = $1 AND discord_id = ANY($2)",
            guild_id, member_ids
        )

        filled = 0 if needed <= 0 else round(10 * prog / needed)
        bar = "▰" * filled + "▱" * (10 - filled)

        embed = discord.Embed(
            title=f"⭐ {target.display_name} — Rank #{ahead + 1:,} of {ranked:,}",
            color=0x7289DA,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Level",     value=str(level),                        inline=True)
        embed.add_field(name="Total XP",  value=f"{xp:,}",                          inline=True)
        embed.add_field(name="Messages",  value=f"{(row['message_count'] or 0):,}", inline=True)
        embed.add_field(
            name=f"Progress to Level {level + 1}",
            value=f"{bar}\n{prog:,} / {needed:,} XP — **{needed - prog:,} XP to go**",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # ── /store ────────────────────────────────────────────────────────────────
    @app_commands.command(name="store", description="Browse the Peepo Bucks store.")
    async def store(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🛒 Peepo Bucks Store",
            description=f"Earn up to **{DAILY_CAP:,} 💰/day** by chatting.\nUse `/redeem <item>` to claim a reward — staff will fulfill it manually.",
            color=0xF4C430
        )
        for item in STORE_ITEMS:
            embed.add_field(
                name=f"{item['emoji']} {item['name']} — {item['price']:,} 💰",
                value=item["description"],
                inline=False
            )
        await interaction.response.send_message(embed=embed)

    # ── /redeem ───────────────────────────────────────────────────────────────
    @app_commands.command(name="redeem", description="Redeem a store item with your Peepo Bucks.")
    @app_commands.describe(item="Item ID to redeem (e.g. nitro, nitro_basic, robux_800)")
    async def redeem(self, interaction: discord.Interaction, item: str):
        item = item.lower().strip()
        store_item = next((i for i in STORE_ITEMS if i["id"] == item), None)
        if not store_item:
            ids = ", ".join(f"`{i['id']}`" for i in STORE_ITEMS)
            await interaction.response.send_message(
                f"Unknown item. Valid items: {ids}", ephemeral=True
            )
            return

        await self.get_or_create(interaction.user)  # ensure the row exists

        # Atomic check-and-debit — the balance guard lives in the UPDATE itself,
        # so concurrent redeems can't both pass a stale balance check.
        debited = await self.pool.fetchrow(
            "UPDATE discord_users SET peepo_bucks = peepo_bucks - $1 "
            "WHERE discord_id = $2 AND peepo_bucks >= $1 RETURNING peepo_bucks",
            store_item["price"], str(interaction.user.id)
        )
        if debited is None:
            row = await self.get_or_create(interaction.user)
            needed = max(store_item["price"] - row["peepo_bucks"], 0)
            await interaction.response.send_message(
                f"You need **{needed:,} more 💰** to redeem {store_item['emoji']} **{store_item['name']}**.",
                ephemeral=True
            )
            return

        # Notify staff in the home guild's mod-chat (works from DMs/other guilds too)
        notify_guild = interaction.guild or self.bot.get_guild(PEEPOS_GUILD_ID)
        log_channel = discord.utils.get(notify_guild.text_channels, name="mod-chat") if notify_guild else None
        notified = False
        if log_channel:
            try:
                await log_channel.send(
                    f"🛒 **Redemption Request**\n"
                    f"User: {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"Item: {store_item['emoji']} **{store_item['name']}**\n"
                    f"Cost: {store_item['price']:,} 💰 (new balance: {debited['peepo_bucks']:,})"
                )
                notified = True
            except discord.HTTPException as e:
                log.error(f"/redeem staff notify failed: {e}")

        msg = (
            f"✅ Redeemed {store_item['emoji']} **{store_item['name']}** for {store_item['price']:,} 💰!\n"
            f"A staff member will fulfill your reward shortly."
        )
        if not notified:
            msg += "\n⚠️ Couldn't reach staff automatically — please ping a mod with this message."
        await interaction.response.send_message(msg, ephemeral=True)

    # ── /levels ───────────────────────────────────────────────────────────────
    @app_commands.command(name="chat-levels", description="Top 10 members by chat level, Peepo Bucks, and Regular Bucks.")
    @app_commands.describe(scope="global = all servers, local = this server only (default)")
    @app_commands.choices(scope=[
        app_commands.Choice(name="local (this server)", value="local"),
        app_commands.Choice(name="global (all servers)", value="global"),
    ])
    async def levels(self, interaction: discord.Interaction, scope: str = "local"):
        medals = ["🥇", "🥈", "🥉"]

        if scope == "global":
            rows = await self.pool.fetch(
                "SELECT discord_id, level, peepo_bucks, regular_bucks FROM discord_users ORDER BY xp DESC LIMIT 10"
            )
            title = "⭐ Leaderboard — Global"
            lines = [
                f"{medals[i] if i < 3 else f'{i+1}.'} <@{r['discord_id']}> — Lv.{r['level']} | {r['peepo_bucks']:,} 💰 | {r['regular_bucks']:,} 💵"
                for i, r in enumerate(rows)
            ]
        else:
            guild_id = str(interaction.guild_id)
            member_ids = [str(m.id) for m in interaction.guild.members if not m.bot]
            # Pull bucks from discord_users, levels from guild_xp
            rows = await self.pool.fetch("""
                SELECT g.discord_id, g.level, g.xp,
                       COALESCE(u.peepo_bucks, 0)  AS peepo_bucks,
                       COALESCE(g.regular_bucks, 0) AS regular_bucks
                FROM guild_xp g
                LEFT JOIN discord_users u ON u.discord_id = g.discord_id
                WHERE g.guild_id = $1 AND g.discord_id = ANY($2)
                ORDER BY g.xp DESC LIMIT 10
            """, guild_id, member_ids)
            title = f"⭐ Leaderboard — {interaction.guild.name}"
            lines = [
                f"{medals[i] if i < 3 else f'{i+1}.'} <@{r['discord_id']}> — Lv.{r['level']} | {r['peepo_bucks']:,} 💰 | {r['regular_bucks']:,} 💵"
                for i, r in enumerate(rows)
            ]
        embed = discord.Embed(title=title, description="\n".join(lines) or "No data yet.", color=0x7289DA)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ── /rpg-leaderboard ──────────────────────────────────────────────────────
    @app_commands.command(name="rpg-leaderboard", description="Top 10 Torvex RPG players by level, coins, and kills.")
    @app_commands.describe(scope="global = all servers, local = this server only (default)")
    @app_commands.choices(scope=[
        app_commands.Choice(name="local (this server)", value="local"),
        app_commands.Choice(name="global (all servers)", value="global"),
    ])
    async def rpg_leaderboard(self, interaction: discord.Interaction, scope: str = "local"):
        await interaction.response.defer()
        medals = ["🥇", "🥈", "🥉"]

        if scope == "global":
            _, data = await _api("POST", "/api/bot/game/leaderboard", json={"discordIds": []})
            title = "⚔️ RPG Leaderboard — Global"
        else:
            member_ids = [str(m.id) for m in interaction.guild.members if not m.bot]
            _, data = await _api("POST", "/api/bot/game/leaderboard", json={"discordIds": member_ids})
            title = f"⚔️ RPG Leaderboard — {interaction.guild.name}"

        if not isinstance(data, list):
            await interaction.followup.send("❌ Could not reach the RPG server.", ephemeral=True)
            return
        if not data:
            await interaction.followup.send("No RPG players found.", ephemeral=True)
            return

        lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} <@{r['discordId']}> — Lv.{r['level']} | {r['xp']:,} XP | {r['kills']:,} ⚔️ | {r['coins']:,} 🪙"
            if r.get('discordId') else
            f"{medals[i] if i < 3 else f'{i+1}.'} **{r['name']}** — Lv.{r['level']} | {r['xp']:,} XP | {r['kills']:,} ⚔️ | {r['coins']:,} 🪙"
            for i, r in enumerate(data)
        ]
        embed = discord.Embed(title=title, description="\n".join(lines) or "No data yet.", color=0xE74C3C)
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ── /backfill-chat-levels ─────────────────────────────────────────────────
    @app_commands.command(name="backfill-chat-levels", description="[Admin] Sync all chat levels to RPG XP multipliers.")
    @app_commands.checks.has_permissions(administrator=True)
    async def backfill_chat_levels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await self.pool.fetch("SELECT discord_id, level FROM discord_users WHERE level > 0")
        if not rows:
            await interaction.followup.send("No users found.", ephemeral=True)
            return
        payload = [{"discordId": r["discord_id"], "newLevel": r["level"]} for r in rows]
        status, data = await _api("POST", "/api/bot/game/sync-level-bulk", json=payload)
        if status == 200:
            await interaction.followup.send(
                f"✅ Backfilled **{data.get('synced', 0)}** / {len(rows)} users.",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Backfill failed.", ephemeral=True)

    @backfill_chat_levels.error
    async def backfill_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)

    # ── /server-notifications ─────────────────────────────────────────────────
    @app_commands.command(name="server-notifications", description="[Admin] Toggle level-up notifications for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_notifications(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        row = await self.pool.fetchrow(
            "SELECT levelup_notifs FROM guild_settings WHERE guild_id = $1", guild_id
        )
        current = row["levelup_notifs"] if row else True
        new_val = not current
        await self.pool.execute("""
            INSERT INTO guild_settings (guild_id, levelup_notifs)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET levelup_notifs = $2
        """, guild_id, new_val)
        state = "**on** ⬆️" if new_val else "**off** 🔕"
        await interaction.response.send_message(
            f"Server level-up notifications turned {state}.", ephemeral=True
        )

    @server_notifications.error
    async def server_notifications_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)

    # ── /notifications ────────────────────────────────────────────────────────
    @app_commands.command(name="notifications", description="Toggle level-up notifications on or off.")
    async def notifications(self, interaction: discord.Interaction):
        row = await self.get_or_create(interaction.user)
        current = row["levelup_notifs"]
        new_val = not current
        await self.pool.execute(
            "UPDATE discord_users SET levelup_notifs = $1 WHERE discord_id = $2",
            new_val, str(interaction.user.id)
        )
        state = "**on** ⬆️" if new_val else "**off** 🔕"
        await interaction.response.send_message(
            f"Level-up notifications turned {state}.", ephemeral=True
        )


    # ── /check-perms ──────────────────────────────────────────────────────────
    @app_commands.command(name="check-perms", description="[Admin] Show and fix channels the bot can't read.")
    @app_commands.describe(fix="Show a picker to fix channel permissions")
    @app_commands.checks.has_permissions(administrator=True)
    async def check_perms(self, interaction: discord.Interaction, fix: bool = False):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.edit_original_response(content="Must be used in a server.")
            return

        me = guild.me
        blocked = []
        for channel in guild.text_channels:
            perms = channel.permissions_for(me)
            if not perms.view_channel or not perms.read_message_history:
                blocked.append(channel)

        if not blocked:
            await interaction.edit_original_response(
                content=f"✅ Bot can read all {len(guild.text_channels)} text channels in this server."
            )
            return

        readable = sum(1 for c in guild.text_channels if c.permissions_for(me).view_channel)
        summary = (
            f"**{len(blocked)} channel(s) the bot can't fully read:**\n"
            + "\n".join(f"• #{c.name}" for c in blocked[:20])
            + (f"\n… and {len(blocked) - 20} more" if len(blocked) > 20 else "")
            + f"\n\n📊 {readable}/{len(guild.text_channels)} visible."
        )

        if not fix:
            summary += "\n\nRun `/check-perms fix:True` to fix these."
            await interaction.edit_original_response(content=summary)
            return

        # Need manage_roles at server level to write channel overwrites
        if not guild.me.guild_permissions.manage_roles:
            summary += (
                "\n\n❌ **Can't fix automatically** — the bot needs the **Manage Roles** permission.\n"
                "Go to **Server Settings → Roles → [Bot's role] → enable Manage Roles**, then try again."
            )
            await interaction.edit_original_response(content=summary)
            return

        # Show exclusion picker — cap at 25 options (Discord select limit)
        view = PermsFixView(me, blocked[:25])
        summary += "\n\n**Select any channels to keep blocked** (e.g. mod channels), then click Fix."
        await interaction.edit_original_response(content=summary, view=view)

    @check_perms.error
    async def check_perms_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Economy(bot))
