"""Automation rules — the engine behind the dashboard's rule builder.

A server admin builds rules at dashboard.torvex.app: WHEN something happens, IF
some conditions hold (all of them, or any of them, each optionally inverted),
THEN do these things. The builder validates and stores them in the guild's
security config under `rules`; this cog is the half that actually runs.

Read this before changing anything here — it is the only feature in the bot
where a non-programmer can, from a web form, make it ban people. Everything
below is shaped around that:

  * FAIL CLOSED. A condition type this file doesn't recognise evaluates to
    False, never True. An action type it doesn't recognise is skipped. So the
    dashboard can ship a new condition before the bot understands it and the
    worst case is a rule that doesn't fire — never one that fires wrongly.
  * A RULE WITH NO CONDITIONS NEVER RUNS. The builder refuses to save one, and
    this refuses to run one, because the two together are what stands between
    "a message is sent → ban" and an empty server. Two independent checks,
    because only one of them is in the process holding the ban permission.
  * THE BREAKER. Kicks, bans and timeouts are counted per guild in a sliding
    window. Past the cap the rest are refused and the mod-log is told once.
    A rule that matches far more than its author expected costs a handful of
    members, not the server.
  * LOOP GUARD. Anything this cog does that would produce another event — its
    own messages, its own role changes — is ignored on the way back in. Without
    it, "on role added → add role" is an infinite loop with a rate limit at the
    end of it.
  * HIERARCHY + OWNER + WHITELIST. Punitive actions never touch the guild owner,
    this bot, anyone on the anti-nuke whitelist, or anyone whose top role sits at
    or above ours.

Nothing here runs until `rules_enabled` is set for the guild, so adding the bot
to a server never changes its behaviour on its own.
"""
import datetime
import os
import re
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config, set_config  # noqa: E402
from utils.quiet_removals import is_quiet  # noqa: E402

MAX_RULES = 25              # mirrors the dashboard's cap; re-applied here because
MAX_ACTIONS = 5             # this process is the one holding the permissions
PUNITIVE = {"kick", "ban", "timeout"}

BREAKER_MAX = 5             # punitive actions per guild...
BREAKER_WINDOW = 60         # ...per this many seconds
SELF_EVENT_TTL = 20         # how long we remember a role change we caused
COOL_MAX = 20000            # cooldown entries kept before a sweep

_URL = re.compile(r"https?://\S+", re.I)
_BARE_URL = re.compile(
    r"(?<![\w@./])(?:[\w-]+\.)+(?:com|net|org|io|gg|co|me|tv|app|dev|xyz|ru|link|site|shop|top|cc)"
    r"(?:/\S*)?", re.I)
_INVITE = re.compile(
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|dsc\.gg|invite\.gg)/[\w-]+", re.I)
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")


# ───────────────────────────────────────────────────── pure evaluation (testable)
def _ids(v):
    """A stored id list as strings. Ids arrive from JSON as strings and from
    discord.py as ints, so everything is compared as str — one int/str mismatch
    here is a condition that silently never matches."""
    return {str(x) for x in (v or [])}


def _words(v):
    return [str(x).strip().lower() for x in (v or []) if str(x).strip()]


def _emoji_eq(want, got):
    """A stored emoji against the one on the event. Standard emoji compare
    exactly; a custom one is stored by name, and `got` is its name too."""
    want, got = str(want or "").strip(), str(got or "").strip()
    return want == got or want.strip(":").lower() == got.strip(":").lower()


CHECKS = {
    "has_role":         lambda v, c: bool(_ids(v) & c["roles"]),
    "is_user":          lambda v, c: str(c["user_id"]) in _ids(v),
    "is_bot":           lambda v, c: c["is_bot"],
    "account_new":      lambda v, c: (c["account_days"] is not None
                                      and c["account_days"] < int(v)),
    "in_channel":       lambda v, c: str(c["channel_id"]) in _ids(v),
    "in_voice":         lambda v, c: str(c["channel_id"]) in _ids(v),
    "content_contains": lambda v, c: any(w in c["lc"] for w in _words(v)),
    "content_equals":   lambda v, c: c["lc"].strip() in _words(v),
    "content_starts":   lambda v, c: any(c["lc"].startswith(w) for w in _words(v)),
    "has_attachment":   lambda v, c: c["attachments"] > 0,
    "has_image":        lambda v, c: c["images"] > 0,
    "has_link":         lambda v, c: bool(_URL.search(c["content"])
                                          or _BARE_URL.search(c["content"])),
    "has_invite":       lambda v, c: bool(_INVITE.search(c["content"])),
    "mentions_gte":     lambda v, c: c["mentions"] >= int(v),
    "length_gte":       lambda v, c: len(c["content"]) >= int(v),
    "role_is":          lambda v, c: c["role_id"] is not None and str(c["role_id"]) in _ids(v),
    "emoji_is":         lambda v, c: (c["emoji"] is not None
                                      and any(_emoji_eq(w, c["emoji"]) for w in (v or []))),
}


def check_condition(cond, ctx) -> bool:
    """One condition against the event. Total: any unknown type, missing value or
    bad stored data is False, so a broken row can only ever make a rule fire
    LESS. Inversion is applied last, deliberately — NOT(unrecognised) would
    otherwise be a way to get True out of something we don't understand."""
    fn = CHECKS.get(cond.get("t"))
    if fn is None:
        return False
    try:
        ok = bool(fn(cond.get("v"), ctx))
    except (TypeError, ValueError, AttributeError, KeyError):
        return False
    return (not ok) if cond.get("not") else ok


def rule_matches(rule, ctx) -> bool:
    conds = rule.get("conditions") or []
    if not conds:
        return False              # see module docstring — never "matches everything"
    results = [check_condition(c, ctx) for c in conds]
    return any(results) if rule.get("match") == "any" else all(results)


def make_ctx(*, user_id, is_bot, roles, account_days=None, content="",
             channel_id=None, attachments=0, images=0, mentions=0,
             role_id=None, emoji=None):
    """Every key always present, so a CHECKS lambda can't KeyError on an event
    type that doesn't carry its field."""
    content = content or ""
    return {"user_id": user_id, "is_bot": bool(is_bot), "roles": _ids(roles),
            "account_days": account_days, "content": content, "lc": content.lower(),
            "channel_id": channel_id, "attachments": attachments, "images": images,
            "mentions": mentions, "role_id": role_id, "emoji": emoji}


def ctx_from_message(message) -> dict:
    a = message.attachments or []
    images = sum(1 for x in a if (x.content_type or "").startswith("image/")
                 or str(x.filename).lower().endswith(_IMAGE_EXT))
    return make_ctx(
        user_id=message.author.id, is_bot=message.author.bot,
        roles=[r.id for r in getattr(message.author, "roles", [])],
        account_days=_account_days(message.author),
        content=message.content or "", channel_id=message.channel.id,
        attachments=len(a), images=images,
        mentions=len(message.mentions) + len(message.role_mentions))


def _account_days(user):
    created = getattr(user, "created_at", None)
    if not created:
        return None
    return (discord.utils.utcnow() - created).total_seconds() / 86400.0


def render(template, member, guild, channel=None, message=None) -> str:
    """Token substitution. Plain str.replace, never format()/f-strings — a
    template an admin typed must not be able to reach an attribute."""
    text = str(template or "")
    body = (message.content if message is not None else "") or ""
    return (text
            .replace("{mention}", getattr(member, "mention", ""))
            .replace("{user}", getattr(member, "display_name", str(member)))
            .replace("{username}", getattr(member, "name", ""))
            .replace("{server}", guild.name if guild else "")
            .replace("{count}", str((guild.member_count or 0) if guild else 0))
            .replace("{channel}", getattr(channel, "mention", ""))
            .replace("{content}", body[:200]))[:2000]


SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)


# ────────────────────────────────────────────────────────────────────────── cog
class AutoRules(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cool = {}        # (guild, rule, member) -> expiry
        self._punish = {}      # guild -> [timestamps of punitive actions]
        self._noted = {}       # guild -> expiry, so a tripped breaker logs once
        self._mine = {}        # (guild, member, role) -> expiry (role changes we made)

    # ── config ──────────────────────────────────────────────────────────────
    def _rules(self, guild, trigger):
        cfg = get_config(guild.id)
        if not cfg.get("rules_enabled"):
            return cfg, []
        out = []
        for r in (cfg.get("rules") or [])[:MAX_RULES]:
            if isinstance(r, dict) and r.get("on") and r.get("trigger") == trigger:
                out.append(r)
        return cfg, out

    # ── rails ───────────────────────────────────────────────────────────────
    def _protected(self, guild, cfg, member) -> bool:
        """Nobody a rule may kick, ban or time out."""
        if member.id == guild.owner_id or member.id == self.bot.user.id:
            return True
        if str(member.id) in _ids(cfg.get("whitelist")):
            return True
        me = guild.me
        top = getattr(member, "top_role", None)
        if me is not None and top is not None and top >= me.top_role:
            return True
        return False

    def _breaker_ok(self, guild) -> bool:
        now = time.time()
        hits = [t for t in self._punish.get(guild.id, []) if t > now - BREAKER_WINDOW]
        self._punish[guild.id] = hits
        return len(hits) < BREAKER_MAX

    def _breaker_hit(self, guild):
        self._punish.setdefault(guild.id, []).append(time.time())

    def _cooled(self, guild_id, rule, member_id) -> bool:
        """True when this rule is still on cooldown for this member. Only called
        AFTER the conditions matched, so a rule nobody triggered never burns its
        own cooldown."""
        secs = int(rule.get("cooldown") or 0)
        if secs <= 0:
            return False
        now = time.time()
        if len(self._cool) > COOL_MAX:
            self._cool = {k: v for k, v in self._cool.items() if v > now}
        key = (guild_id, str(rule.get("id")), member_id)
        if self._cool.get(key, 0) > now:
            return True
        self._cool[key] = now + secs
        return False

    def _mark_mine(self, guild_id, member_id, role_id):
        now = time.time()
        if len(self._mine) > 4096:
            self._mine = {k: v for k, v in self._mine.items() if v > now}
        self._mine[(guild_id, member_id, int(role_id))] = now + SELF_EVENT_TTL

    def _is_mine(self, guild_id, member_id, role_id) -> bool:
        return self._mine.pop((guild_id, member_id, int(role_id)), 0) > time.time()

    def _modlog(self, guild, cfg):
        mid = cfg.get("modlog_channel_id")
        return guild.get_channel(int(mid)) if mid else None

    async def _say(self, channel, content, *, reference=None):
        if not channel or not content:
            return
        try:
            await channel.send(content, allowed_mentions=SAFE_MENTIONS,
                               reference=reference)
        except (discord.Forbidden, discord.HTTPException, TypeError):
            pass

    # ── the pass ────────────────────────────────────────────────────────────
    async def _fire(self, guild, trigger, member, ctx, *, message=None, channel=None):
        """Run every enabled rule for this trigger, top to bottom."""
        if guild is None or member is None:
            return
        # Loop guard, first and unconditional: our own messages and actions
        # produce the same events we listen for.
        if member.id == self.bot.user.id:
            return
        cfg, rules = self._rules(guild, trigger)
        if not rules:
            return
        for rule in rules:
            try:
                if not rule_matches(rule, ctx):
                    continue
                if self._cooled(guild.id, rule, member.id):
                    continue
                await self._run(guild, cfg, rule, member, message, channel)
                if rule.get("stop"):
                    break
            except Exception as e:                     # one bad rule must not
                print(f"[auto_rules] rule {rule.get('id')} in {guild.id}: {e}")

    async def _run(self, guild, cfg, rule, member, message, channel):
        for act in (rule.get("actions") or [])[:MAX_ACTIONS]:
            t = act.get("t")
            if t in PUNITIVE:
                if self._protected(guild, cfg, member):
                    continue
                if not self._breaker_ok(guild):
                    await self._note_breaker(guild, cfg, rule)
                    continue
                self._breaker_hit(guild)
            try:
                await self._do(t, act, guild, cfg, rule, member, message, channel)
            except (discord.Forbidden, discord.HTTPException, ValueError, TypeError):
                pass                                   # a permission we don't have
                                                       # is not worth a traceback

    async def _do(self, t, act, guild, cfg, rule, member, message, channel):
        v, w = act.get("v"), act.get("w")

        if t == "delete" and message is not None:
            await message.delete()

        elif t == "react" and message is not None:
            await message.add_reaction(str(v))

        elif t == "reply" and message is not None:
            await self._say(message.channel, render(v, member, guild, message.channel, message),
                            reference=message)

        elif t == "send":
            dest = guild.get_channel(int(w)) if w else None
            await self._say(dest, render(v, member, guild, dest, message))

        elif t == "dm":
            try:
                await member.send(render(v, member, guild, channel, message),
                                  allowed_mentions=SAFE_MENTIONS)
            except (discord.Forbidden, discord.HTTPException):
                pass                                   # closed DMs are the norm

        elif t == "pin" and message is not None:
            await message.pin(reason=f"Automation: {rule.get('name')}")

        elif t in ("add_role", "remove_role"):
            await self._roles(t, v, guild, member, rule)

        elif t == "timeout":
            mins = max(1, min(40320, int(v or 1)))
            await member.timeout(discord.utils.utcnow() + datetime.timedelta(minutes=mins),
                                 reason=f"Automation: {rule.get('name')}")
            await self._log_action(guild, cfg, rule, member, f"timed out for {mins}m")

        elif t == "kick":
            await member.kick(reason=f"Automation: {rule.get('name')}")
            await self._log_action(guild, cfg, rule, member, "kicked")

        elif t == "ban":
            days = max(0, min(7, int(v or 0)))
            await member.ban(delete_message_days=days,
                             reason=f"Automation: {rule.get('name')}")
            await self._log_action(guild, cfg, rule, member, f"banned ({days}d purge)")

        # anything else: an action this build doesn't know. Skipped, not guessed.

    async def _roles(self, t, ids, guild, member, rule):
        """Add or remove roles, then remember we did it so the resulting
        on_member_update doesn't come back round as a role_add/role_remove
        trigger and run this same rule again."""
        me = guild.me
        held = {r.id for r in member.roles}
        targets = []
        for rid in (ids or []):
            r = guild.get_role(int(rid))
            if not r or r.managed or (me and r >= me.top_role):
                continue
            if (t == "add_role") == (r.id in held):
                continue                               # already in the wanted state
            targets.append(r)
        if not targets:
            return
        reason = f"Automation: {rule.get('name')}"
        for r in targets:
            self._mark_mine(guild.id, member.id, r.id)
        if t == "add_role":
            await member.add_roles(*targets, reason=reason)
        else:
            await member.remove_roles(*targets, reason=reason)

    async def _log_action(self, guild, cfg, rule, member, what):
        ch = self._modlog(guild, cfg)
        if not ch:
            return
        e = discord.Embed(
            title="⚙️ Automation acted on a member",
            description=f"{member.mention} (`{member.id}`) was **{what}**.",
            color=0xFAA61A, timestamp=discord.utils.utcnow())
        e.add_field(name="Rule", value=str(rule.get("name") or rule.get("id")), inline=True)
        e.add_field(name="Trigger", value=str(rule.get("trigger")), inline=True)
        e.set_footer(text="Built at dashboard.torvex.app · /automation status")
        try:
            await ch.send(embed=e)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _note_breaker(self, guild, cfg, rule):
        """Say once per window that the cap stopped something. Silence here would
        make a runaway rule look like a rule that simply didn't work."""
        now = time.time()
        if self._noted.get(guild.id, 0) > now:
            return
        self._noted[guild.id] = now + BREAKER_WINDOW
        ch = self._modlog(guild, cfg)
        if not ch:
            return
        e = discord.Embed(
            title="🛑 Automation rate limit hit",
            description=(
                f"A rule tried to kick, ban or time out more than **{BREAKER_MAX} "
                f"people in {BREAKER_WINDOW}s**, so the rest were refused.\n\n"
                f"Rule: **{rule.get('name') or rule.get('id')}**\n\n"
                "This is the safety cap, not a Discord error. Either a rule is "
                "matching far more than intended, or you're being raided — check "
                "the rule at dashboard.torvex.app before turning anything back on."),
            color=0xED4245, timestamp=discord.utils.utcnow())
        try:
            await ch.send(embed=e)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── listeners ───────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.webhook_id:
            return
        await self._fire(message.guild, "message", message.author,
                         ctx_from_message(message),
                         message=message, channel=message.channel)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        # The edited text is what matters — an edit that sneaks a link in is the
        # whole reason this trigger exists.
        if not after.guild or after.webhook_id or before.content == after.content:
            return
        await self._fire(after.guild, "message_edit", after.author,
                         ctx_from_message(after), message=after, channel=after.channel)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self._fire(member.guild, "join", member, make_ctx(
            user_id=member.id, is_bot=member.bot, roles=[r.id for r in member.roles],
            account_days=_account_days(member)))

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if is_quiet(member.id):
            return                      # silenced removal — no leave rules fire
        await self._fire(member.guild, "leave", member, make_ctx(
            user_id=member.id, is_bot=member.bot, roles=[r.id for r in member.roles],
            account_days=_account_days(member)))

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        gained = {r.id for r in after.roles} - {r.id for r in before.roles}
        lost = {r.id for r in before.roles} - {r.id for r in after.roles}
        for trigger, changed in (("role_add", gained), ("role_remove", lost)):
            for rid in changed:
                if self._is_mine(after.guild.id, after.id, rid):
                    continue                           # we did this one — no loop
                await self._fire(after.guild, trigger, after, make_ctx(
                    user_id=after.id, is_bot=after.bot,
                    roles=[r.id for r in after.roles],
                    account_days=_account_days(after), role_id=rid))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        """Raw, so a reaction on a message that predates the cache still counts.
        The message itself is only fetched when an action actually needs it."""
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member or guild.get_member(payload.user_id)
        if member is None or member.id == self.bot.user.id:
            return
        cfg, rules = self._rules(guild, "reaction")
        if not rules:
            return
        ctx = make_ctx(user_id=member.id, is_bot=member.bot,
                       roles=[r.id for r in member.roles],
                       account_days=_account_days(member),
                       channel_id=payload.channel_id,
                       emoji=payload.emoji.name)
        message = None
        if any(a.get("t") in ("delete", "react", "reply", "pin")
               for r in rules for a in (r.get("actions") or [])):
            channel = guild.get_channel(payload.channel_id)
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException, AttributeError):
                message = None
        await self._fire(guild, "reaction", member, ctx, message=message,
                         channel=guild.get_channel(payload.channel_id))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # Moving between channels counts as joining the new one — that is what
        # "joins a voice channel" means to the person who wrote the rule.
        if after.channel is None or before.channel == after.channel:
            return
        await self._fire(member.guild, "voice_join", member, make_ctx(
            user_id=member.id, is_bot=member.bot, roles=[r.id for r in member.roles],
            account_days=_account_days(member), channel_id=after.channel.id))

    # ── commands ────────────────────────────────────────────────────────────
    group = app_commands.Group(
        name="automation", description="Custom automation rules (Admin only)",
        default_permissions=discord.Permissions(administrator=True))

    @group.command(name="status", description="Show this server's automation rules.")
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild.id)
        rules = [r for r in (cfg.get("rules") or []) if isinstance(r, dict)]
        on = bool(cfg.get("rules_enabled"))
        e = discord.Embed(
            title="⚙️ Automation",
            description=("🟢 **On**" if on else "🔴 **Off** — no rule runs"),
            color=0x5B8CFF)
        if not rules:
            e.add_field(name="Rules", value="None yet.", inline=False)
        else:
            lines = []
            for r in rules[:MAX_RULES]:
                acts = ", ".join(str(a.get("t")) for a in (r.get("actions") or []))
                lines.append(f"{'🟢' if r.get('on') else '⚫'} **{r.get('name')}** — "
                             f"on `{r.get('trigger')}` → {acts or '—'}")
            e.add_field(name=f"Rules ({len(rules)})", value="\n".join(lines)[:1024],
                        inline=False)
        e.set_footer(text="Build and edit them at dashboard.torvex.app")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @group.command(name="enable", description="Turn ALL automation rules on or off.")
    @app_commands.checks.has_permissions(administrator=True)
    async def enable(self, interaction: discord.Interaction, on: bool):
        set_config(interaction.guild.id, rules_enabled=1 if on else 0)
        await interaction.response.send_message(
            f"Automation rules are now **{'on' if on else 'off'}**."
            + ("" if on else " Every rule stays saved."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoRules(bot))
