"""Invite tracking + lockdown for peepos-reclaimer.

Goal: make the bot the SOLE source of invites, so attribution is deterministic
(the bot knows exactly which code it handed to whom) and every join is sourced.

Two kinds of tracked invite:
  * PER-MEMBER  — `/invite` mints a unique permanent link owned by the runner →
    exact "who invited whom".
  * PER-SOURCE  — `/tracked-invite label:<x>` mints a labeled permanent link for a
    public source (Disboard / Reddit / a website) → joins attribute to the source,
    not an individual.

Tracking works even for non-bot invites (admin-made, Disboard, pre-existing) via
the classic cache-and-diff on join; bot-minted codes resolve deterministically
from the `invites_meta` table.

LOCKDOWN (`/invite-lockdown`) is the deliberate Phase-2 flip: deny `@everyone`
Create-Invite, sweep channel overwrites so none re-grant it, and purge native
invites — PRESERVING bot-tracked codes + any in INVITE_KEEP. Dry-run by default
(`confirm:True` to act); self-maintains new-channel drift like quarantine_lock.
UNLOCK (`/invite-unlock`) reverses it: re-grant `@everyone` Create-Invite and
clear the channel denies — native invites come back, and the cache-diff
attribution keeps crediting joins (kind=native, inviter from the API). Native
UI invites default to 7-day expiry / optional max-uses; a maxed-out invite is
DELETED by Discord the instant it's consumed, so attribution also watches for
codes that vanish between snapshots (see `pick_used_invite`).

Guild-scoped to ALTGUARD_GUILD_ID. Bot needs **Manage Server** (list/delete
invites) + **Create Invite**, and **Manage Roles/Channels** for lockdown.
"""
import os
import time
import hmac
import hashlib
import sqlite3
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("invites")


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
INVITE_CHANNEL_ID = _env_int("INVITE_CHANNEL_ID")          # where minted invites point (else system channel)
# codes to never purge during lockdown (e.g. the Disboard listing invite), comma/space separated
INVITE_KEEP = {c.strip() for c in os.environ.get("INVITE_KEEP", "").replace(",", " ").split() if c.strip()}
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "invites.db"))

# the permission we strip from @everyone in lockdown
NO_INVITE = dict(create_instant_invite=False)

# gate access for /invite-intel (fuse invite attribution with device/verdict)
GATE_URL = os.environ.get("ALTGUARD_GATE_URL", "").rstrip("/")
SECRET = os.environ.get("ALTGUARD_SECRET", "")

# how long a gateway invite-delete stays eligible for join attribution (seconds).
# A max-uses invite is deleted the moment it's consumed; the INVITE_DELETE event
# can land before or after the GUILD_MEMBER_ADD it belongs to.
VANISH_WINDOW = 20.0


def pick_used_invite(before, after, recent_gone, now):
    """Decide which invite code a just-joined member used.

    before:      {code: (uses, inviter_id)} cache snapshot from before the join
    after:       {code: (uses, inviter_id)} fresh snapshot taken after the join
    recent_gone: {code: (inviter_id, deleted_at)} codes the gateway said were
                 deleted moments ago
    Returns (code, inviter_id) or (None, None) if nothing matches unambiguously.
    """
    # a use-count that went up is definitive
    for code, (uses, inviter) in after.items():
        if uses > before.get(code, (0, None))[0]:
            return code, inviter
    # no count moved → the invite may have vanished on use (hit max uses, or was
    # deleted/expired mid-join). A gateway-confirmed deletion is the strongest
    # signal; mere absence from the list is weaker (could be lazy expiry).
    recent = {c: inv for c, (inv, ts) in recent_gone.items()
              if now - ts <= VANISH_WINDOW and c not in after}
    if len(recent) == 1:
        return next(iter(recent.items()))
    vanished = {c: inv for c, (_u, inv) in before.items() if c not in after}
    if len(vanished) == 1:
        return next(iter(vanished.items()))
    return None, None


def _hmac_headers():
    ts = str(time.time())
    sig = hmac.new(SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return {"X-AltGuard-TS": ts, "X-AltGuard-Auth": sig}


class Invites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None       # aiohttp, for gate lookups
        self._cache = {}          # guild_id -> {code: (uses, inviter_id)}
        self._vanity = {}         # guild_id -> vanity uses
        self._recent_gone = {}    # guild_id -> {code: (inviter_id, deleted_at)}
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS invites_meta (
                       code       TEXT PRIMARY KEY,
                       guild_id   TEXT,
                       owner_id   TEXT,     -- member who minted it (per-member) or admin
                       label      TEXT,     -- source label, NULL for per-member
                       kind       TEXT,     -- 'member' | 'source'
                       created_at REAL
                   )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS invite_attributions (
                       uid              TEXT,
                       guild_id         TEXT,
                       code             TEXT,
                       inviter_id       TEXT,   -- owner/creator of the code
                       label            TEXT,   -- source label if any
                       kind             TEXT,   -- member|source|native|vanity|unknown
                       account_age_days INTEGER,
                       joined_at        REAL
                   )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_attr_inviter ON invite_attributions(inviter_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_attr_uid ON invite_attributions(uid)")

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ---------------------------------------------------------------- cache
    async def _refresh_cache(self, guild):
        """Snapshot current invite use-counts. Needs Manage Server; silent no-op if not."""
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            log.warning("missing Manage Server in %s — invite tracking disabled", guild.id)
            return None
        except discord.HTTPException:
            return None
        self._cache[guild.id] = {
            inv.code: (inv.uses or 0, str(inv.inviter.id) if inv.inviter else None)
            for inv in invites
        }
        if "VANITY_URL" in guild.features:
            try:
                v = await guild.vanity_invite()
                self._vanity[guild.id] = (v.uses or 0) if v else 0
            except discord.HTTPException:
                pass
        return invites

    @commands.Cog.listener()
    async def on_ready(self):
        g = self.bot.get_guild(GUILD_ID)
        if g:
            await self._refresh_cache(g)
            log.info("invite cache primed for %s (%d codes)", g.id, len(self._cache.get(g.id, {})))

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        if invite.guild and invite.guild.id == GUILD_ID:
            self._cache.setdefault(invite.guild.id, {})[invite.code] = (
                invite.uses or 0, str(invite.inviter.id) if invite.inviter else None)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        if invite.guild and invite.guild.id == GUILD_ID:
            cached = self._cache.get(invite.guild.id, {}).pop(invite.code, None)
            inviter = cached[1] if cached else (
                str(invite.inviter.id) if getattr(invite, "inviter", None) else None)
            # remember it briefly: a max-uses invite is deleted the instant it's
            # consumed, and this event can beat the member-join to us
            self._recent_gone.setdefault(invite.guild.id, {})[invite.code] = (inviter, time.time())

    # ---------------------------------------------------------------- attribution
    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.guild.id != GUILD_ID or member.bot:
            return
        guild = member.guild
        before = dict(self._cache.get(guild.id, {}))
        vbefore = self._vanity.get(guild.id, 0)
        invites = await self._refresh_cache(guild)

        code = inviter_id = label = None
        kind = "unknown"
        if invites is not None:
            now = time.time()
            gone = self._recent_gone.setdefault(guild.id, {})
            code, inviter_id = pick_used_invite(before, self._cache.get(guild.id, {}), gone, now)
            for c in [c for c, (_i, ts) in gone.items() if now - ts > VANISH_WINDOW]:
                gone.pop(c, None)

        if code is not None:
            meta = self._meta(code)
            if meta:                       # bot-minted → deterministic
                inviter_id = meta["owner_id"]
                label = meta["label"]
                kind = meta["kind"]
            else:                          # native/admin/Disboard → API inviter (cached)
                kind = "native"
        elif "VANITY_URL" in guild.features and self._vanity.get(guild.id, 0) > vbefore:
            kind, label = "vanity", "vanity-url"          # vanity use-count ticked up
        elif invites is not None:
            # no invite code matched and vanity didn't move → Server Discovery /
            # widget / other inviteless path. Untracked source, but still gate-verified.
            kind, label = "discovery", "discovery"

        age_days = int((time.time() - member.created_at.timestamp()) / 86400)
        with self._conn() as c:
            c.execute(
                "INSERT INTO invite_attributions(uid, guild_id, code, inviter_id, label, kind, account_age_days, joined_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (str(member.id), str(guild.id), code, inviter_id, label, kind, age_days, time.time()),
            )
        log.info("join %s via %s (kind=%s inviter=%s label=%s)", member.id, code, kind, inviter_id, label)

    def _meta(self, code):
        with self._conn() as c:
            r = c.execute("SELECT * FROM invites_meta WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None

    def _invite_target(self, guild):
        ch = guild.get_channel(INVITE_CHANNEL_ID) if INVITE_CHANNEL_ID else None
        if ch is None:
            ch = guild.system_channel
        if ch is None:
            me = guild.me
            ch = next((c for c in guild.text_channels
                       if c.permissions_for(me).create_instant_invite), None)
        return ch

    async def _mint(self, guild, owner_id, label, kind, reason):
        ch = self._invite_target(guild)
        if ch is None:
            return None
        invite = await ch.create_invite(max_age=0, max_uses=0, unique=True, reason=reason)
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO invites_meta(code, guild_id, owner_id, label, kind, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (invite.code, str(guild.id), str(owner_id) if owner_id else None, label, kind, time.time()),
            )
        self._cache.setdefault(guild.id, {})[invite.code] = (invite.uses or 0, str(self.bot.user.id))
        return invite

    # ---------------------------------------------------------------- commands
    @app_commands.command(name="invite", description="Get your personal tracked invite link.")
    @app_commands.guild_only()
    async def invite(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild.me.guild_permissions.create_instant_invite:
            await interaction.followup.send("❌ I can't create invites — I'm missing **Create Invite**.", ephemeral=True)
            return
        # reuse the member's existing personal invite if it still exists
        with self._conn() as c:
            row = c.execute(
                "SELECT code FROM invites_meta WHERE guild_id=? AND owner_id=? AND kind='member' ORDER BY created_at DESC LIMIT 1",
                (str(guild.id), str(interaction.user.id))).fetchone()
        existing = row["code"] if row else None
        if existing and existing in self._cache.get(guild.id, {}):
            await interaction.followup.send(
                f"🔗 Your tracked invite: https://discord.gg/{existing}\nEveryone who joins through it is credited to you.",
                ephemeral=True)
            return
        invite = await self._mint(guild, interaction.user.id, None, "member",
                                  f"/invite for {interaction.user} ({interaction.user.id})")
        if invite is None:
            await interaction.followup.send("❌ Couldn't find a channel I'm allowed to create an invite in.", ephemeral=True)
            return
        await interaction.followup.send(
            f"🔗 Your tracked invite: {invite.url}\nEveryone who joins through it is credited to you.", ephemeral=True)

    @app_commands.command(name="tracked-invite", description="Mint a labeled invite for a public source (Disboard, Reddit, a site).")
    @app_commands.describe(label="A source label, e.g. 'disboard', 'reddit', 'website'")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def tracked_invite(self, interaction: discord.Interaction, label: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild.me.guild_permissions.create_instant_invite:
            await interaction.followup.send("❌ I'm missing **Create Invite**.", ephemeral=True)
            return
        invite = await self._mint(guild, interaction.user.id, label.strip().lower(), "source",
                                  f"tracked-invite '{label}' by {interaction.user}")
        if invite is None:
            await interaction.followup.send("❌ No channel I can create an invite in.", ephemeral=True)
            return
        await interaction.followup.send(
            f"🏷️ Source invite for **{label}**: {invite.url}\n"
            f"Joins through it attribute to source `{label}`. Add `{invite.code}` to `INVITE_KEEP` so lockdown won't purge it.",
            ephemeral=True)

    @app_commands.command(name="invite-stats", description="Top inviters and join sources (admin).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def invite_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        with self._conn() as c:
            top = c.execute(
                "SELECT inviter_id, COUNT(*) n FROM invite_attributions "
                "WHERE guild_id=? AND kind IN ('member','native') AND inviter_id IS NOT NULL "
                "GROUP BY inviter_id ORDER BY n DESC LIMIT 10",
                (str(interaction.guild.id),)).fetchall()
            src = c.execute(
                "SELECT COALESCE(label, kind) src, COUNT(*) n FROM invite_attributions "
                "WHERE guild_id=? GROUP BY src ORDER BY n DESC", (str(interaction.guild.id),)).fetchall()
            total = c.execute("SELECT COUNT(*) n FROM invite_attributions WHERE guild_id=?",
                              (str(interaction.guild.id),)).fetchone()["n"]
        e = discord.Embed(title="📨 Invite attribution", color=0x5865F2,
                          description=f"{total} attributed joins on record.")
        if top:
            e.add_field(name="Top inviters",
                        value="\n".join(f"<@{r['inviter_id']}> — {r['n']}" for r in top) or "—", inline=False)
        if src:
            e.add_field(name="By source",
                        value="\n".join(f"`{r['src']}` — {r['n']}" for r in src) or "—", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="invite-intel",
                          description="Full join dossier: invite source + device/verdict fused by uid (mod).")
    @app_commands.describe(user="Member to inspect", user_id="...or a raw Discord ID")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def invite_intel(self, interaction: discord.Interaction,
                           user: discord.User = None, user_id: str = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(user.id) if user else (user_id or "").strip()
        if not uid.isdigit():
            await interaction.followup.send("Give me a member or a numeric `user_id`.", ephemeral=True)
            return
        e = discord.Embed(title=f"🧾 Invite intel — {uid}", color=0x5865F2)

        # 1) invite attribution (our own db)
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM invite_attributions WHERE uid=? AND guild_id=? ORDER BY joined_at DESC LIMIT 1",
                (uid, str(interaction.guild.id))).fetchone()
        if row:
            src = row["label"] or (f"<@{row['inviter_id']}>" if row["inviter_id"] else "—")
            e.add_field(name="Joined via",
                        value=f"`{row['kind']}` · invite `{row['code'] or '—'}` · source: {src}", inline=False)
            e.add_field(name="Account age at join", value=f"{row['account_age_days']} days", inline=True)
            e.add_field(name="Joined", value=f"<t:{int(row['joined_at'])}:R>", inline=True)
        else:
            e.add_field(name="Joined via",
                        value="no invite attribution on record (joined before tracking, or bot was offline)", inline=False)

        # 2) device / verdict from the gate (captured at verify, not at join)
        dev = "gate not configured"
        if GATE_URL and self.session:
            try:
                async with self.session.get(f"{GATE_URL}/api/lookup", params={"uid": uid},
                                            headers=_hmac_headers(), timeout=10) as r:
                    data = await r.json()
                if data.get("found"):
                    res = data["result"]; a = res.get("attrs") or {}
                    dev = (f"**{res.get('verdict','?')}** (risk {res.get('match_pct',0)}%) · "
                           f"{a.get('glRenderer','?')} · {a.get('screen','?')} · "
                           f"{res.get('country','?')}/{res.get('isp','?')} `{res.get('ip','?')}` · env {res.get('environment','?')}")
                else:
                    dev = "no verification on file — never completed the gate, so **no device captured**"
            except Exception as ex:
                dev = f"gate lookup failed: {ex}"
        e.add_field(name="🖥️ Device / verdict (at verify)", value=dev[:1024], inline=False)
        e.set_footer(text="invite = who/what brought them · device = captured only at verify")
        await interaction.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="invite-lockdown",
                          description="Phase 2: block native invites + purge untracked links (dry-run unless confirm).")
    @app_commands.describe(confirm="Actually apply it. Without this it only reports what WOULD change.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def invite_lockdown(self, interaction: discord.Interaction, confirm: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        me = guild.me
        if not (me.guild_permissions.manage_roles and me.guild_permissions.manage_guild):
            await interaction.followup.send("❌ I need **Manage Roles** + **Manage Server** for lockdown.", ephemeral=True)
            return

        # what would be purged: every live invite NOT bot-tracked and NOT in INVITE_KEEP
        tracked = set(self._cache.get(guild.id, {}))  # ensure fresh
        await self._refresh_cache(guild)
        try:
            live = await guild.invites()
        except discord.HTTPException:
            live = []
        with self._conn() as c:
            bot_codes = {r["code"] for r in c.execute("SELECT code FROM invites_meta WHERE guild_id=?", (str(guild.id),))}
        keep = bot_codes | INVITE_KEEP
        to_purge = [inv for inv in live if inv.code not in keep]
        everyone_has = guild.default_role.permissions.create_instant_invite

        if not confirm:
            lines = [f"**Dry run** — nothing changed.",
                     f"- `@everyone` Create-Invite currently: **{'ON' if everyone_has else 'off'}** → would be **denied**",
                     f"- Channels swept to deny Create-Invite for `@everyone`",
                     f"- Invites that would be **purged**: {len(to_purge)} "
                     f"(keeping {len(bot_codes)} tracked + {len(INVITE_KEEP)} in INVITE_KEEP)"]
            if to_purge:
                lines.append("  purge: " + ", ".join(f"`{i.code}`" for i in to_purge[:15]) + ("…" if len(to_purge) > 15 else ""))
            lines.append("\nRun again with `confirm:True` to apply.")
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return

        # APPLY
        # 1) deny @everyone at the role level
        perms = guild.default_role.permissions
        perms.update(create_instant_invite=False)
        await guild.default_role.edit(permissions=perms, reason="invite-lockdown: route invites through the bot")
        # 2) sweep channel overwrites so none re-grant it
        swept = 0
        for ch in guild.channels:
            ov = ch.overwrites_for(guild.default_role)
            if ov.create_instant_invite is not False:
                ov.update(create_instant_invite=False)
                try:
                    await ch.set_permissions(guild.default_role, overwrite=ov, reason="invite-lockdown sweep")
                    swept += 1
                except discord.HTTPException:
                    pass
        # 3) purge untracked invites
        purged = 0
        for inv in to_purge:
            try:
                await inv.delete(reason="invite-lockdown: replaced by tracked invites")
                self._cache.get(guild.id, {}).pop(inv.code, None)
                purged += 1
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            f"🔒 **Invite lockdown applied.** `@everyone` can no longer create invites "
            f"({swept} channel overwrites corrected), {purged} untracked invites purged "
            f"(kept {len(bot_codes)} tracked + {len(INVITE_KEEP)} pinned). Members invite via `/invite` now.\n"
            f"⚠️ Admin/Administrator roles still bypass this, and the vanity URL (if any) is unaffected.",
            ephemeral=True)

    @app_commands.command(name="invite-unlock",
                          description="Reverse the lockdown: let members create native invites again (dry-run unless confirm).")
    @app_commands.describe(confirm="Actually apply it. Without this it only reports what WOULD change.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def invite_unlock(self, interaction: discord.Interaction, confirm: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        me = guild.me
        if not (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels):
            await interaction.followup.send("❌ I need **Manage Roles** + **Manage Channels** to unlock.", ephemeral=True)
            return

        everyone_has = guild.default_role.permissions.create_instant_invite
        denied = [ch for ch in guild.channels
                  if ch.overwrites_for(guild.default_role).create_instant_invite is False]

        if not confirm:
            lines = [f"**Dry run** — nothing changed.",
                     f"- `@everyone` Create-Invite currently: **{'ON' if everyone_has else 'off'}** → would be **granted**",
                     f"- Channel overwrites denying it that would be cleared: {len(denied)}"]
            if denied:
                lines.append("  " + ", ".join(f"{ch.mention}" for ch in denied[:20]) + ("…" if len(denied) > 20 else ""))
            lines.append("\nJoins through native invites still get tracked (cache-diff, `kind=native`). "
                         "Run again with `confirm:True` to apply.")
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return

        # APPLY
        # 1) grant @everyone at the role level — role perms are additive, so this
        #    covers every member regardless of per-role strips from the lockdown
        perms = guild.default_role.permissions
        perms.update(create_instant_invite=True)
        await guild.default_role.edit(permissions=perms, reason="invite-unlock: native invites re-enabled")
        # 2) clear the channel-overwrite denies the lockdown sweep planted
        cleared = 0
        for ch in denied:
            ov = ch.overwrites_for(guild.default_role)
            ov.update(create_instant_invite=None)
            try:
                await ch.set_permissions(guild.default_role, overwrite=ov, reason="invite-unlock sweep")
                cleared += 1
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            f"🔓 **Invite lockdown reversed.** `@everyone` can create invites again "
            f"({cleared} channel denies cleared). Native joins are tracked via cache-diff; "
            f"`/invite` still works for deterministic personal links.",
            ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        """Keep new channels from re-opening native invites once locked down."""
        guild = channel.guild
        if guild.id != GUILD_ID or guild.default_role.permissions.create_instant_invite:
            return  # only maintain drift if lockdown is in effect
        ov = channel.overwrites_for(guild.default_role)
        if ov.create_instant_invite is not False:
            ov.update(create_instant_invite=False)
            try:
                await channel.set_permissions(guild.default_role, overwrite=ov, reason="invite-lockdown (new channel)")
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(Invites(bot))
