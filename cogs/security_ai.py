"""Security AI — the paid, sealed second opinion on AltGuard verdicts.

Design: altguard/docs/MULTI-SERVER-DESIGN.md §6-§7; seals + tier orchestration
live in utils/security_ai.py (pure, tested). This cog is the Discord half:

- operator entitlement commands (grant / revoke / status), home-guild only —
  same fulfilment stance as Logging Pro: /Contact lead in, operator grant out;
- `schedule_review()` — the hook cogs/altguard.py calls on a flagged case.
  It answers instantly (tier check only) and does the model work in a task,
  so the results poll never waits on a provider.

The reviewer drafts, never decides: nothing here touches a role. The output
is an embed in the same log channel that carries the case.
"""

import asyncio
import logging
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import security_ai as sai  # noqa: E402
from utils.ai_provider import build_provider  # noqa: E402

log = logging.getLogger("security_ai")

HOME_GUILD_ID = int(os.environ.get("AI_HOME_GUILD_ID", os.environ.get("ALTGUARD_GUILD_ID", "1215140346800119868")))

# Key precedence: a dedicated key if the operator set one, else the paid-pool
# key (its provider-side monthly cap bounds the damage of any bug here too).
_KEY_ENV = "SECURITY_AI_API_KEY" if os.environ.get("SECURITY_AI_API_KEY") else "AI_API_KEY_PAID"

TIER_LABEL = {"standard": "Standard", "advanced": "Advanced", "elite": "Elite"}


class SecurityAI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.provider = build_provider(key_env=_KEY_ENV)
        self._inflight = set()

    # ── the hook the altguard cog calls ──────────────────────────────────
    def schedule_review(self, guild, uid, signals, verdict, local_facts, channel):
        """Fire-and-forget. Cheap unless the guild is entitled."""
        if self.provider is None or channel is None:
            return
        tier = sai.tier_for(guild.id)
        if tier is None:
            return
        key = (guild.id, str(uid))
        if key in self._inflight:
            return
        self._inflight.add(key)
        task = asyncio.create_task(
            self._review(guild, tier, str(uid), signals or [], verdict, local_facts or {}, channel))
        task.add_done_callback(lambda _t: self._inflight.discard(key))

    async def _review(self, guild, tier, uid, signals, verdict, local_facts, channel):
        if not sai.take_case(guild.id, tier):
            log.info("security-ai: %s at monthly case cap (%s)", guild.id, tier)
            return
        case_text, mapping = sai.build_case(uid, signals, verdict, local_facts)
        model = sai.MODELS[tier]
        tokens = {"in": 0, "out": 0}

        async def chat(system, user_content):
            r = await self.provider.chat(model, system, user_content, sai.MAX_TOKENS)
            tokens["in"] += r.tokens_in
            tokens["out"] += r.tokens_out
            if r.refusal or not r.text:
                raise RuntimeError("provider returned no assessment")
            return r.text

        try:
            text, passes, rec = await sai.assess(chat, tier, case_text)
        except Exception as e:
            log.warning("security-ai review failed for %s in %s: %s", uid, guild.id, e)
            return
        clean, flagged = sai.lint_output(text, mapping)
        e = discord.Embed(
            title=f"🔎 Security AI — reviewed verdict ({TIER_LABEL[tier]})",
            description=clean[:4000],
            color=0xE67E22 if rec == "HOLD" else 0x2ECC71,
        )
        e.add_field(name="Subject", value=f"<@{uid}> (`{uid}`)", inline=True)
        if rec:
            e.add_field(name="Recommendation", value=rec, inline=True)
        note = ("Sealed review: the model saw signal classes and placeholders only — "
                "no identities, no raw telemetry. It drafts; your mods decide.")
        if flagged:
            note += " Output lint removed content the model should not have produced."
        e.set_footer(text=note)
        try:
            await channel.send(embed=e)
        except (discord.Forbidden, discord.HTTPException) as err:
            log.info("security-ai: could not post review in %s: %s", guild.id, err)
        log.info("security-ai: %s case for %s in %s — %d passes, %d in / %d out tokens",
                 tier, uid, guild.id, passes, tokens["in"], tokens["out"])

    # ── operator commands ────────────────────────────────────────────────
    # One GROUP, not three top-level commands: the global tree sits at
    # Discord's 100-command cap (adding three loose commands knocked the
    # economy cog out of the tree on 2026-08-24 — a group costs one slot).
    group = app_commands.Group(
        name="security-ai",
        description="Security AI — sealed reviewed verdicts on flagged members",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True)

    def _home_admin(self, interaction) -> bool:
        return interaction.guild_id == HOME_GUILD_ID

    @group.command(
        name="grant",
        description="[Operator] Grant a Security AI tier to a server")
    @app_commands.describe(guild_id="Server to entitle",
                           tier="Review tier",
                           days="Days until it lapses (0 = no expiry)",
                           note="Why — order ref, comp, trial")
    @app_commands.choices(tier=[
        app_commands.Choice(name="Standard", value="standard"),
        app_commands.Choice(name="Advanced", value="advanced"),
        app_commands.Choice(name="Elite", value="elite"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def grant(self, interaction: discord.Interaction, guild_id: str,
                    tier: app_commands.Choice[str], days: int = 0, note: str = ""):
        if not self._home_admin(interaction):
            await interaction.response.send_message("Home-server only.", ephemeral=True)
            return
        try:
            gid = int(guild_id.strip())
        except ValueError:
            await interaction.response.send_message("That's not a guild id.", ephemeral=True)
            return
        row = sai.grant(gid, tier.value, days, note)
        g = self.bot.get_guild(gid)
        until = (f"until <t:{row['expires_ts']}:D>" if row["expires_ts"] else "no expiry")
        await interaction.response.send_message(
            f"✅ **{TIER_LABEL[tier.value]}** review → **{g.name if g else gid}** ({until}). "
            f"Flagged cases there now get a sealed written assessment.", ephemeral=True)

    @group.command(
        name="revoke",
        description="[Operator] Remove a server's Security AI tier")
    @app_commands.describe(guild_id="Server to revoke")
    @app_commands.checks.has_permissions(administrator=True)
    async def revoke(self, interaction: discord.Interaction, guild_id: str):
        if not self._home_admin(interaction):
            await interaction.response.send_message("Home-server only.", ephemeral=True)
            return
        try:
            gid = int(guild_id.strip())
        except ValueError:
            await interaction.response.send_message("That's not a guild id.", ephemeral=True)
            return
        sai.revoke(gid)
        await interaction.response.send_message(f"Removed Security AI from `{gid}`.", ephemeral=True)

    @group.command(
        name="status",
        description="Security AI tier and monthly review usage for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        row = sai.entitlement(gid)
        tier = sai.tier_for(gid)
        if tier is None:
            lapsed = row is not None and row.get("expires_ts") and row["expires_ts"] < time.time()
            msg = ("Security AI isn't active here"
                   + (" — the previous grant has lapsed" if lapsed else "")
                   + ". Flagged members still get the free case file; the paid tiers add a "
                     "sealed written assessment. Pricing lives on the Packages board.")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        used = sai.cases_used(gid)
        cap = sai.CASE_CAPS[tier]
        until = (f"until <t:{row['expires_ts']}:D>" if row.get("expires_ts") else "no expiry")
        await interaction.response.send_message(
            f"🔎 **Security AI — {TIER_LABEL[tier]}** is active ({until}).\n"
            f"Reviews this month: **{used}/{cap}** (fair-use). Every flagged case gets a "
            f"sealed written assessment in your security log.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(SecurityAI(bot))
