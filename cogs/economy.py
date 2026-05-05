import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import aiohttp
import os
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

DAILY_CAP = 200
BUCKS_PER_MESSAGE = 1
XP_PER_MESSAGE = 10

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


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pool: asyncpg.Pool | None = None

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
        # Add daily cap columns if upgrading from old schema
        await self.pool.execute("""
            ALTER TABLE discord_users
                ADD COLUMN IF NOT EXISTS daily_bucks_date  DATE,
                ADD COLUMN IF NOT EXISTS daily_bucks_count INT NOT NULL DEFAULT 0
        """)

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

    async def record_message(self, user: discord.User | discord.Member) -> int | None:
        """Award bucks (up to daily cap) + XP. Returns new level if leveled up, else None."""
        row = await self.get_or_create(user)
        today = date.today()

        # Reset daily counter if new day
        is_new_day = row["daily_bucks_date"] != today
        if is_new_day:
            daily_count = 0
        else:
            daily_count = row["daily_bucks_count"]

        # Award daily coin bonus on first message of the day
        if is_new_day:
            await _award_daily_coins(str(user.id))

        bucks_to_award = BUCKS_PER_MESSAGE if daily_count < DAILY_CAP else 0

        old_level = row["level"]
        new_xp = row["xp"] + XP_PER_MESSAGE
        new_level = level_from_xp(new_xp)

        await self.pool.execute("""
            UPDATE discord_users SET
                peepo_bucks       = peepo_bucks + $1,
                xp                = $2,
                level             = $3,
                message_count     = message_count + 1,
                daily_bucks_date  = $4,
                daily_bucks_count = CASE WHEN daily_bucks_date = $4 THEN daily_bucks_count + $1 ELSE $1 END,
                username          = $5,
                updated_at        = NOW()
            WHERE discord_id = $6
        """, bucks_to_award, new_xp, new_level, today, user.display_name, str(user.id))

        return new_level if new_level > old_level else None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        new_level = await self.record_message(message.author)
        if new_level is not None:
            await message.channel.send(
                f"⬆️ {message.author.mention} leveled up to **Level {new_level}**!"
            )
            # Sync the new chat level to the linked RPG character
            await _api("POST", "/api/bot/game/sync-level", json={
                "discordId": str(message.author.id),
                "newLevel": new_level,
            })

    # ── /balance ──────────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="Check your Peepo Bucks and level.")
    @app_commands.describe(user="Check another user's balance (optional)")
    async def balance(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        row = await self.get_or_create(target)
        xp = row["xp"]
        level = row["level"]
        next_xp = xp_for_level(level + 1)
        spent_on_level = sum(xp_for_level(i) for i in range(1, level))
        progress = xp - spent_on_level

        today = date.today()
        daily_used = row["daily_bucks_count"] if row["daily_bucks_date"] == today else 0

        embed = discord.Embed(title=f"{target.display_name}'s Stats", color=0xF4C430)
        embed.add_field(name="💰 Peepo Bucks", value=f"{row['peepo_bucks']:,}", inline=True)
        embed.add_field(name="⭐ Level", value=str(level), inline=True)
        embed.add_field(name="📨 Messages", value=f"{row['message_count']:,}", inline=True)
        embed.add_field(name="📈 XP Progress", value=f"{progress:,} / {next_xp:,}", inline=True)
        embed.add_field(name="📅 Daily Bucks", value=f"{daily_used:,} / {DAILY_CAP:,}", inline=True)
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

        row = await self.get_or_create(interaction.user)
        if row["peepo_bucks"] < store_item["price"]:
            needed = store_item["price"] - row["peepo_bucks"]
            await interaction.response.send_message(
                f"You need **{needed:,} more 💰** to redeem {store_item['emoji']} **{store_item['name']}**.",
                ephemeral=True
            )
            return

        await self.pool.execute(
            "UPDATE discord_users SET peepo_bucks = peepo_bucks - $1 WHERE discord_id = $2",
            store_item["price"], str(interaction.user.id)
        )

        # Notify staff
        log_channel = discord.utils.get(interaction.guild.text_channels, name="mod-chat")
        if log_channel:
            await log_channel.send(
                f"🛒 **Redemption Request**\n"
                f"User: {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"Item: {store_item['emoji']} **{store_item['name']}**\n"
                f"Cost: {store_item['price']:,} 💰"
            )

        await interaction.response.send_message(
            f"✅ Redeemed {store_item['emoji']} **{store_item['name']}** for {store_item['price']:,} 💰!\n"
            f"A staff member will fulfill your reward shortly.",
            ephemeral=True
        )

    # ── /richest ──────────────────────────────────────────────────────────────
    @app_commands.command(name="richest", description="Top 10 richest members.")
    async def richest(self, interaction: discord.Interaction):
        rows = await self.pool.fetch(
            "SELECT username, peepo_bucks, level FROM discord_users ORDER BY peepo_bucks DESC LIMIT 10"
        )
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{r['username']}** — {r['peepo_bucks']:,} 💰 | Lv.{r['level']}"
            for i, r in enumerate(rows)
        ]
        embed = discord.Embed(title="💰 Peepo Bucks Leaderboard", description="\n".join(lines) or "No data yet.", color=0xF4C430)
        await interaction.response.send_message(embed=embed)

    # ── /levels ───────────────────────────────────────────────────────────────
    @app_commands.command(name="levels", description="Top 10 highest level members.")
    async def levels(self, interaction: discord.Interaction):
        rows = await self.pool.fetch(
            "SELECT username, level, xp FROM discord_users ORDER BY xp DESC LIMIT 10"
        )
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{r['username']}** — Lv.{r['level']} | {r['xp']:,} XP"
            for i, r in enumerate(rows)
        ]
        embed = discord.Embed(title="⭐ Level Leaderboard", description="\n".join(lines) or "No data yet.", color=0x7289DA)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Economy(bot))
