"""AI commands for peepos-reclaimer — /ask with metered usage and characters.

Provider-agnostic: the model backend is an .env choice (see utils/ai_provider.py)
— Anthropic or any OpenAI-compatible API (OpenRouter, Groq, Gemini, DeepSeek).
Swapping providers is a config change plus optionally AI_PRICES for the meter.

Cost model (see utils/ai_meter.py): every request's real microdollar cost is
recorded in ai_usage.db. Users get DAILY_FREE_ENERGY per day; past that a
request costs flat peepo bucks (atomic debit via the Economy cog's pool, same
pattern as /redeem, refunded if the API call fails). A monthly global budget
(AI_MONTHLY_BUDGET_USD) is the hard kill switch on top of everything.

Data scoping: the model only ever sees the question plus recent messages from
the invoking channel, and ONLY when that channel is visible to @everyone —
private/staff channels contribute no context. No database access, no archive,
no tools. What the code doesn't fetch, no prompt injection can leak.
"""
import os
import sys
import time
import sqlite3
import logging

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.ai_meter import (
    BUCKS_PRICE, DAILY_FREE_ENERGY,
    cost_micro, month_key, budget_micro, remaining_energy,
)
from utils.ai_provider import build_provider

log = logging.getLogger("ai")

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ai_usage.db"))

MODEL_SMART = os.getenv("AI_MODEL_SMART", "claude-sonnet-5")
MODEL_QUICK = os.getenv("AI_MODEL_QUICK", "claude-haiku-4-5")
MAX_OUT_TOKENS = 1400
CONTEXT_MESSAGES = 12
CONTEXT_SNIPPET = 200  # chars per context message

MONTHLY_BUDGET_USD = float(os.getenv("AI_MONTHLY_BUDGET_USD", "20"))
AI_GUILD_IDS = {
    int(g) for g in os.getenv("AI_GUILD_IDS", "1215140346800119868").split(",") if g.strip()
}

# Rules appended to EVERY persona — the character never overrides these.
BASE_RULES = (
    "Hard rules that survive any persona: keep everything PG-13 — server members "
    "include minors. Be funny at the situation, never genuinely cruel to a member. "
    "Never invent facts about server members. Answers must still be genuinely "
    "helpful and correct underneath the character. Use Discord markdown. Keep "
    "answers under 300 words unless the question truly needs more (like a code "
    "solution). If recent channel messages are provided, you may use them for "
    "context."
)

PERSONAS = {
    "peepo": (
        "You are Peepo's Reclaimer, the resident bot of a Discord server. You are "
        "helpful, a little playful, and concise. Answer questions directly, help "
        "with code (use code blocks), and settle chat arguments fairly."
    ),
    "wizard": (
        "You are Grimbeard the Unfathomable, an ancient and extremely dramatic "
        "archmage haunting a Discord server. Everything is portents, forbidden "
        "tomes, and 'the old magicks' — even mundane questions get a "
        "prophecy-flavored (but genuinely correct) answer. You address members "
        "as 'young apprentice' and treat Google as a rival wizard."
    ),
    "gremlin": (
        "You are Grub, the server gremlin: feral, chaotic, types in lowercase "
        "with barely any punctuation, loves shiny objects and stirring harmless "
        "chaos. Your answers are still correct — you just deliver them like a "
        "raccoon that found espresso."
    ),
    "butler": (
        "You are Reginald, an impossibly posh, long-suffering butler who has — "
        "through circumstances he would rather not discuss — ended up in service "
        "to an entire Discord server. Address members as 'sir or madam', answer "
        "with immaculate manners, and let the faintest dry judgment show."
    ),
}


class AI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.provider = None  # built in cog_load from .env (see utils/ai_provider)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_usage (
                       ts         INTEGER,
                       day        TEXT,
                       guild_id   TEXT,
                       user_id    TEXT,
                       model      TEXT,
                       tokens_in  INTEGER,
                       tokens_out INTEGER,
                       micro      INTEGER,
                       bucks      INTEGER DEFAULT 0
                   )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_ai_user_day ON ai_usage(user_id, day)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ai_day ON ai_usage(day)")
            c.execute("CREATE TABLE IF NOT EXISTS ai_optout (user_id TEXT PRIMARY KEY)")
        self._optout = self._load_optout()

    def _load_optout(self) -> set:
        with self._conn() as c:
            return {r[0] for r in c.execute("SELECT user_id FROM ai_optout").fetchall()}

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def cog_load(self):
        self.provider = build_provider()  # None (with a log line) if unconfigured

    # ── accounting helpers ────────────────────────────────────────────────────

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _user_spent_today(self, user_id: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(micro),0) FROM ai_usage WHERE user_id=? AND day=?",
                (user_id, self._today()),
            ).fetchone()
        return row[0]

    def _month_spent(self) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(micro),0) FROM ai_usage WHERE day LIKE ?",
                (month_key(self._today()) + "-%",),
            ).fetchone()
        return row[0]

    def _record(self, guild_id, user_id, model, tokens_in, tokens_out, micro, bucks=0):
        with self._conn() as c:
            c.execute(
                "INSERT INTO ai_usage(ts, day, guild_id, user_id, model, tokens_in, tokens_out, micro, bucks) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (int(time.time()), self._today(), str(guild_id), str(user_id),
                 model, tokens_in, tokens_out, micro, bucks),
            )

    # ── peepo bucks (Economy cog's Postgres pool, /redeem debit pattern) ─────

    async def _debit_bucks(self, user: discord.abc.User, amount: int):
        """Atomically charge bucks. Returns new balance, or None if broke/unavailable."""
        eco = self.bot.get_cog("Economy")
        if eco is None or getattr(eco, "pool", None) is None:
            return None
        await eco.get_or_create(user)
        return await eco.pool.fetchrow(
            "UPDATE discord_users SET peepo_bucks = peepo_bucks - $1 "
            "WHERE discord_id = $2 AND peepo_bucks >= $1 RETURNING peepo_bucks",
            amount, str(user.id),
        )

    async def _refund_bucks(self, user: discord.abc.User, amount: int):
        eco = self.bot.get_cog("Economy")
        if eco is None or getattr(eco, "pool", None) is None:
            return
        await eco.pool.execute(
            "UPDATE discord_users SET peepo_bucks = peepo_bucks + $1 WHERE discord_id = $2",
            amount, str(user.id),
        )

    # ── context ───────────────────────────────────────────────────────────────

    async def _channel_context(self, channel, guild) -> str:
        """Recent messages from the invoking channel, but ONLY if @everyone can
        see it — private/staff channels never feed the prompt."""
        try:
            if not channel.permissions_for(guild.default_role).view_channel:
                return ""
            lines = []
            async for m in channel.history(limit=CONTEXT_MESSAGES):
                if m.author.bot or not m.content:
                    continue
                if str(m.author.id) in self._optout:
                    continue  # user opted out of ever appearing in AI context
                snippet = m.content[:CONTEXT_SNIPPET]
                lines.append(f"{m.author.display_name}: {snippet}")
            lines.reverse()
            return "\n".join(lines)
        except discord.HTTPException:
            return ""

    # ── /ask ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="ask", description="Ask the AI — it knows the server 🤖")
    @app_commands.describe(
        question="What do you want to know?",
        quick="Quick mode — faster and cheaper, good for simple questions",
        character="Who answers — the bot, a wizard, a gremlin, or the butler",
    )
    @app_commands.choices(character=[
        app_commands.Choice(name="Peepo (default)", value="peepo"),
        app_commands.Choice(name="Grimbeard the wizard 🧙", value="wizard"),
        app_commands.Choice(name="Grub the gremlin 🦝", value="gremlin"),
        app_commands.Choice(name="Reginald the butler 🎩", value="butler"),
    ])
    @app_commands.checks.cooldown(1, 15.0)
    @app_commands.guild_only()
    async def ask(self, interaction: discord.Interaction, question: str,
                  quick: bool = False, character: str = "peepo"):
        if interaction.guild.id not in AI_GUILD_IDS:
            await interaction.response.send_message("AI isn't enabled in this server.", ephemeral=True)
            return
        if self.provider is None:
            await interaction.response.send_message("AI isn't configured yet — poke the owner.", ephemeral=True)
            return

        # global monthly kill switch
        if self._month_spent() >= budget_micro(MONTHLY_BUDGET_USD):
            await interaction.response.send_message(
                "🧯 The AI hit this month's server-wide budget. Back on the 1st!", ephemeral=True)
            return

        tier = "quick" if quick else "smart"
        model = MODEL_QUICK if quick else MODEL_SMART
        user_id = str(interaction.user.id)
        energy_left = remaining_energy(self._user_spent_today(user_id))

        # past the free allowance → flat bucks price, debited up front
        bucks_charged = 0
        if energy_left <= 0:
            price = BUCKS_PRICE[tier]
            debited = await self._debit_bucks(interaction.user, price)
            if debited is None:
                await interaction.response.send_message(
                    f"⚡ You're out of AI energy for today (resets at midnight UTC).\n"
                    f"Extra uses cost **{BUCKS_PRICE['smart']} 💰** (or **{BUCKS_PRICE['quick']} 💰** "
                    f"with `quick: True`) — you don't have enough bucks right now.",
                    ephemeral=True)
                return
            bucks_charged = price

        await interaction.response.defer(thinking=True)

        context = await self._channel_context(interaction.channel, interaction.guild)
        user_content = (
            (f"Recent messages in #{interaction.channel.name}:\n{context}\n\n" if context else "")
            + f"{interaction.user.display_name} asks: {question}"
        )

        system = PERSONAS.get(character, PERSONAS["peepo"]) + "\n\n" + BASE_RULES

        try:
            result = await self.provider.chat(
                model=model, system=system,
                user_content=user_content, max_tokens=MAX_OUT_TOKENS)
        except Exception as e:
            log.error("/ask API call failed: %s", e)
            if bucks_charged:
                await self._refund_bucks(interaction.user, bucks_charged)
            await interaction.followup.send(
                "😵 The AI didn't answer — try again in a minute."
                + (f" Your {bucks_charged} 💰 was refunded." if bucks_charged else ""))
            return

        micro = cost_micro(model, result.tokens_in, result.tokens_out)
        self._record(interaction.guild.id, user_id, model,
                     result.tokens_in, result.tokens_out,
                     micro, bucks_charged)

        if result.refusal:
            await interaction.followup.send("🙅 That's not something I'll answer. Try something else.")
            return

        text = result.text
        if not text:
            text = "…I've got nothing. Try rephrasing?"
        if len(text) > 4000:
            text = text[:4000] + "…"

        left_after = remaining_energy(self._user_spent_today(user_id))
        footer = (f"paid {bucks_charged} 💰" if bucks_charged
                  else f"⚡ {max(left_after, 0)}/{DAILY_FREE_ENERGY} energy left today")
        mode = ("quick" if quick else "smart") + (f" · {character}" if character != "peepo" else "")
        embed = discord.Embed(description=text, color=0x8FCE00)
        embed.set_footer(text=f"{mode} · {footer}")
        await interaction.followup.send(embed=embed)

    # ── /ai-usage ────────────────────────────────────────────────────────────

    @app_commands.command(name="ai-usage", description="Check your AI energy for today ⚡")
    async def ai_usage(self, interaction: discord.Interaction):
        left = remaining_energy(self._user_spent_today(str(interaction.user.id)))
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(bucks),0) FROM ai_usage WHERE user_id=? AND day=?",
                (str(interaction.user.id), self._today()),
            ).fetchone()
        await interaction.response.send_message(
            f"⚡ **{max(left, 0)}/{DAILY_FREE_ENERGY}** energy left today (resets midnight UTC)\n"
            f"Questions asked today: **{row[0]}**"
            + (f" · bucks spent: **{row[1]} 💰**" if row[1] else "")
            + f"\nOut of energy? Extra asks cost **{BUCKS_PRICE['smart']} 💰** "
              f"(**{BUCKS_PRICE['quick']} 💰** quick).",
            ephemeral=True)

    # ── /ai-privacy ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="ai-privacy",
        description="Toggle whether your messages can appear as AI context 🔒")
    async def ai_privacy(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        with self._conn() as c:
            if uid in self._optout:
                c.execute("DELETE FROM ai_optout WHERE user_id=?", (uid,))
                self._optout.discard(uid)
                msg = ("🔓 Opted back in — your recent public messages may appear as context "
                       "when someone uses /ask in the same channel.")
            else:
                c.execute("INSERT OR IGNORE INTO ai_optout(user_id) VALUES (?)", (uid,))
                self._optout.add(uid)
                msg = ("🔒 Opted out — your messages will never be shown to the AI, even as "
                       "channel context. (The AI has no memory of anyone either way — "
                       "this removes you from the last-few-messages context too.) "
                       "You can still use /ask yourself.")
        await interaction.response.send_message(msg, ephemeral=True)

    # ── /ai-status (admin) ───────────────────────────────────────────────────

    @app_commands.command(name="ai-status", description="AI spend + usage this month (admin)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def ai_status(self, interaction: discord.Interaction):
        month = month_key(self._today())
        with self._conn() as c:
            totals = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(micro),0), COALESCE(SUM(tokens_in),0), "
                "COALESCE(SUM(tokens_out),0), COALESCE(SUM(bucks),0) "
                "FROM ai_usage WHERE day LIKE ?", (month + "-%",)).fetchone()
            top = c.execute(
                "SELECT user_id, COUNT(*) AS n, SUM(micro) AS m FROM ai_usage "
                "WHERE day LIKE ? GROUP BY user_id ORDER BY m DESC LIMIT 5",
                (month + "-%",)).fetchall()
        spent_usd = totals[1] / 1_000_000
        lines = [f"• <@{r['user_id']}> — {r['n']} asks, ${r['m'] / 1_000_000:.2f}" for r in top]
        await interaction.response.send_message(
            f"🤖 **AI — {month}**\n"
            f"Spend: **${spent_usd:.2f} / ${MONTHLY_BUDGET_USD:.0f}** budget\n"
            f"Requests: {totals[0]:,} · tokens in/out: {totals[2]:,}/{totals[3]:,} · bucks sunk: {totals[4]:,} 💰\n"
            + ("**Top users:**\n" + "\n".join(lines) if lines else "No usage yet."),
            ephemeral=True)

    @ask.error
    async def ask_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Slow down — try again in {error.retry_after:.0f}s.", ephemeral=True)
        else:
            raise error


async def setup(bot):
    await bot.add_cog(AI(bot))
