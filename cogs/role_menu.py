"""Reaction roles for peepos-reclaimer — persistent BUTTON panels that replace
MEE6 / carl-bot reaction roles.

Its own feature, deliberately separate from the automation cog: automation is
what the bot does TO a member (roles on join, welcome messages), this is what a
member picks FOR THEMSELVES. They share nothing but the word "role".

An admin builds a panel three ways — `/rolemenu template` for a ready-made set
(and it creates any missing roles), `/rolemenu create` + `addrole` to hand-build
one, or `/rolemenu bootstrap` for this guild's legacy MEE6 migration. Members
click a button to toggle the role. Buttons survive restarts: at load we
re-register a persistent View for every stored panel (custom_id "rm:<panel>:<role>").

Panels can be pick-one (`exclusive`), which is how age bands and name colours
stay mutually exclusive in ANY guild — that used to be a hardcoded list of this
server's role ids.

Why buttons over reactions: they don't get lost on bot downtime, need no Manage
Emoji, and the grant is done by THIS bot — which anti-nuke exempts — so a burst
of members self-assigning never trips the nuke detector (the carl-bot problem).
"""
import asyncio
import json
import os
import re
import sys
import sqlite3
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.role_templates import TEMPLATES, template_choices, as_json  # noqa: E402

log = logging.getLogger("role_menu")
# Relocatable like security_config: the web dashboard edits panels, and it runs
# as its own user that deliberately cannot read the bot directory. Env wins; the
# in-repo path stays as the fallback so an un-migrated deployment still works.
DB_PATH = os.environ.get("TORVEX_ROLEMENUS_DB") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "role_menus.db"))

# How the dashboard hands work back to us. It only ever writes rows — every
# Discord call still happens here, through the same _render() the commands use,
# so there is exactly one place that posts a panel.
#   'dirty'  -> (re)post or edit the message, then re-register its buttons
#   'delete' -> remove the message and the rows
SYNC_SECONDS = 8

# One-shot migration map: the current MEE6 reaction-roles set, by category.
# (role_id, label, emoji|None). Bootstrap resolves each id in the live guild and
# skips any that no longer exist. Guild-specific by design.
BOOTSTRAP = {
    "🔔 Notifications, Age & Pronouns": [
        (1355906692230942801, "Updates", "<:PR_Hello:1215156803013189702>"),
        (1355906592981389505, "Giveaways", "<a:PR_peepoMoney:1220527134758539437>"),
        (1259518414314279072, "Minecraft", "<:PR_PepeMC:1258425267676778687>"),
        (1355909151552966677, "Partnerships", "<:PR_partner:1355907896269078729>"),
        (1231420352555515924, "Bump", "<a:PR_HappyPat:1215670898309201960>"),
        (1367583655526137996, "Movie Night", "<:PR_WatchingStreamHigh:1215307620932526080>"),
        (1393212887161372802, "Game Nights", "<a:PR_peepoPhasmophobia:1350114563617587261>"),
        (1526745472838926436, "13-15", "🐣"),
        (1526745473434517535, "16-17", "🌱"),
        (1526745474055409736, "18-21", "✨"),
        (1526745474743140423, "22-27", "🍷"),
        (1526745475456434186, "28+", "🧭"),
        (1401003907597205726, "She/Her", "<:PR_PeepoBlush:1215605210509090846>"),
        (1401009394946146475, "He/Him", "<:PR_peepoPoop:1219078988996018278>"),
        (1401009462478897284, "They/Them", "<:PR_PepeDuck:1216010584818974892>"),
    ],
    "🌍 Regions": [
        (1510408219854639195, "North America", "<a:PR_HmmSip:1218012108109647882>"),
        (1510408288699810063, "South America", "<:PR_gigglesmcpepe:1379260034042691659>"),
        (1510408353720041594, "Europe", "<:PR_grumpy:1402014971142996070>"),
        (1510408420640161842, "Africa", "<:PR_Pausetime:1215709682149367818>"),
        (1510408482485043303, "Asia", "<:PR_FeelsOldMan:1219184846459637860>"),
        (1510408544133054515, "Oceania", "<:PR_PeepoBlush:1215605210509090846>"),
    ],
    "❓ Sexuality": [
        (1510411007519101078, "Straight", "<:Straight:1510417293774160042>"),
        (1510411150435553500, "Gay", "🏳️‍🌈"),
        (1510411279788146879, "Bisexual", "<:Bisek:1510417626868744252>"),
        (1510639254382841999, "Lesbian", "<:Lesbian:1510419311129399396>"),
        (1510411483140456518, "Trans", "<:Trans:1510419253294006342>"),
        (1510411552518307951, "Other", "❔"),
    ],
    "❓ Colours": [
        (1510716001468153998, "red nose day", "❤️"),
        (1510716074637656184, "orangutang", "🧡"),
        (1510716133165240421, "highlighter", "💛"),
        (1510718554813501480, "blue tac", "🩵"),
        (1510718623428247573, "Porpel", "💜"),
        (1510718457933463763, "Shrek", "💚"),
        (1510718707888947430, "Ponk", "🩷"),
    ],
}
BOOTSTRAP_BLURB = {
    "🔔 Notifications, Age & Pronouns": (
        "**Hey there, notification squad!** Pick one or all of the buttons below to stay in the loop.\n\n"
        "📢 **Updates** — general server updates\n"
        "🎉 **Giveaways** — giveaways & competitions (often Nitro & more!)\n"
        "⛏️ **Minecraft** — Minecraft server updates & news\n"
        "🤝 **Partnerships** — get pinged when we get a new partner\n"
        "👋 **Bump** — get pinged when it's time to bump the server\n"
        "🎬 **Movie Night** — pings for our movie nights\n"
        "🎮 **Game Nights** — game nights run by Banjo (usually Roblox — suggestions welcome!)\n\n"
        "**Please also pick an age role** — this is an all-age server and we want to keep everyone safe. 🙏\n"
        "And grab your **pronouns** while you're here!"
    ),
    "🌍 Regions": "React to the region you live in!",
    "❓ Sexuality": "Pick your sexuality preference — totally optional.",
    "❓ Colours": "Pick a name colour below! 🎨",
}


# Age bands are mutually exclusive — a member has exactly one age. Clicking a band
# button removes any other band they hold (done server-side, no page/UI removal).
# The legacy binary 18+/under-18 roles are included so an existing member picking a
# band also sheds their old binary role (go-forward migration).
AGE_BAND_ROLE_IDS = {
    1526745472838926436, 1526745473434517535, 1526745474055409736,
    1526745474743140423, 1526745475456434186,
    1355942945060294867, 1355943018611347618,  # legacy 18+ / under-18
}
EXCLUSIVE_GROUPS = [AGE_BAND_ROLE_IDS]


def _exclusive_group(role_id):
    """The mutually-exclusive group a role belongs to, or None."""
    for g in EXCLUSIVE_GROUPS:
        if int(role_id) in g:
            return g
    return None


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS panels(
                       panel_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                       guild_id   TEXT, channel_id TEXT, message_id TEXT,
                       title      TEXT, description TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS panel_roles(
                       panel_id INTEGER, role_id TEXT, label TEXT, emoji TEXT, pos INTEGER)""")
        # Exclusivity used to be a hardcoded set of THIS guild's age-role ids, so
        # no other server could have a pick-one panel. It's a per-panel property
        # now; the legacy id set below still runs so existing members keep
        # shedding the old binary 18+/under-18 roles.
        cols = {r[1] for r in c.execute("PRAGMA table_info(panels)")}
        if "exclusive" not in cols:
            c.execute("ALTER TABLE panels ADD COLUMN exclusive INTEGER DEFAULT 0")
        if "state" not in cols:
            # work queue for panels edited outside the bot (the dashboard)
            c.execute("ALTER TABLE panels ADD COLUMN state TEXT")
        rcols = {r[1] for r in c.execute("PRAGMA table_info(panel_roles)")}
        if "colour" not in rcols:
            # A row the dashboard saves with NO role_id is a role that doesn't
            # exist yet: panel_sync creates it (name = label, this colour) before
            # posting. That is what lets a template be edited freely on the web
            # BEFORE any role is made, instead of creating the whole set and
            # deleting the leftovers.
            c.execute("ALTER TABLE panel_roles ADD COLUMN colour INTEGER")


def _button_rows(c, panel_id):
    """The rows that can actually be buttons — a role that hasn't been created
    yet (role_id NULL) has nothing to grant, so it is never rendered."""
    return c.execute("SELECT * FROM panel_roles WHERE panel_id=? AND role_id IS NOT NULL "
                     "ORDER BY pos", (panel_id,)).fetchall()


_init()


def parse_emoji(raw, resolve):
    """Normalise whatever an admin typed into something a Button can render.

    Returns (value, error). `value` is the string we store — None means "no
    emoji", which is always valid. `resolve` takes a custom-emoji id and returns
    the emoji if this bot can actually use it.

    Any standard emoji works. A CUSTOM emoji only renders if the bot shares a
    server with it, and Discord rejects the whole message component when it
    doesn't — meaning one bad emoji takes down the entire panel, not just its
    button. So it's checked here, at the point somebody can still fix it.
    """
    if raw is None:
        return None, None
    raw = raw.strip()
    if not raw:
        return None, None

    # <:name:id> / <a:name:id>, or a bare id pasted from Discord's dev mode
    m = re.fullmatch(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>", raw)
    eid = m.group(3) if m else (raw if raw.isdigit() and 15 <= len(raw) <= 25 else None)
    if eid:
        emoji = resolve(int(eid))
        if emoji is None:
            return None, ("I can't use that custom emoji — I'm not in the server it comes from. "
                          "Use a standard emoji, or upload the image to THIS server with "
                          "`/steal-emoji` and then use it here.")
        return str(emoji), None

    if raw.startswith("<") or raw.startswith(":"):
        return None, ("That doesn't look like an emoji I can read. Paste the emoji itself, "
                      "or its `<:name:id>` form.")
    # Anything else: a real emoji character (possibly multi-codepoint, e.g. a
    # flag or a skin-tone sequence). Reject long text so a typo isn't stored as
    # an "emoji" that fails at render time.
    if len(raw) > 16 or raw.isascii():
        return None, ("That doesn't look like an emoji. Paste the emoji itself, or upload an "
                      "image with `/steal-emoji` to make it a server emoji first.")
    return raw, None


def plan_template_role(existing, bot_top):
    """What to do with one template entry: 'create', 'reuse' or 'blocked'.

    A role that already exists but sits at or above the bot's top role is the
    trap worth naming: Discord lets us put it on a panel but never lets us
    assign it, so including it would ship a button that always errors.
    """
    if existing is None:
        return "create"
    if existing >= bot_top:
        return "blocked"
    return "reuse"


def _panel_exclusive_roles(panel_id):
    """Every role on `panel_id` if that panel is pick-one, else empty.

    Read at click time rather than baked into the button: an admin can flip a
    panel to exclusive, or add a role to it, without the live buttons going
    stale (they're persistent views that outlive restarts).
    """
    try:
        with _conn() as c:
            p = c.execute("SELECT exclusive FROM panels WHERE panel_id=?", (panel_id,)).fetchone()
            if not p or not p["exclusive"]:
                return set()
            return {int(r["role_id"]) for r in
                    c.execute("SELECT role_id FROM panel_roles WHERE panel_id=? "
                              "AND role_id IS NOT NULL", (panel_id,))}
    except sqlite3.Error:
        return set()   # never block a click on a config read


class RoleButton(discord.ui.Button):
    def __init__(self, panel_id, role_id, label, emoji):
        super().__init__(style=discord.ButtonStyle.secondary, label=label or None,
                         emoji=emoji or None, custom_id=f"rm:{panel_id}:{role_id}")
        self.role_id = int(role_id)
        self.panel_id = int(panel_id)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        role = guild.get_role(self.role_id) if guild else None
        if role is None:
            await interaction.response.send_message("That role no longer exists — tell an admin.", ephemeral=True)
            return
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="self-assign role menu")
                await interaction.response.send_message(f"Removed {role.mention}.", ephemeral=True)
            else:
                # mutually-exclusive: drop whatever else they hold from the same
                # group. Two sources — this panel being pick-one (any guild), and
                # the legacy hardcoded age-band ids (this guild's old roles).
                drop = set(_exclusive_group(self.role_id) or ())
                drop |= _panel_exclusive_roles(self.panel_id)
                others = [r for r in member.roles if r.id in drop and r.id != self.role_id]
                if others:
                    await member.remove_roles(*others, reason="self-assign role menu (exclusive)")
                await member.add_roles(role, reason="self-assign role menu")
                await interaction.response.send_message(f"Added {role.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I can't assign {role.mention} — my role must sit **above** it and I need **Manage Roles**.",
                ephemeral=True)


def _build_view(panel_id, roles):
    v = discord.ui.View(timeout=None)
    for r in roles:
        v.add_item(RoleButton(panel_id, r["role_id"], r["label"], r["emoji"]))
    return v


class RoleMenu(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # register persistent views at load time (cog_load, not on_ready — the
        # bot loads cogs in setup_hook and add_view needs no guild cache, so
        # buttons are live before the gateway can deliver a single click)
        n = 0
        with _conn() as c:
            for p in c.execute("SELECT panel_id FROM panels").fetchall():
                roles = _button_rows(c, p["panel_id"])
                if roles:
                    self.bot.add_view(_build_view(p["panel_id"], roles))
                    n += 1
        print(f"role menus: registered {n} persistent panels")
        self._export_templates()
        self.panel_sync.start()

    def _export_templates(self):
        """Publish the template set for the dashboard's "start from a template"
        picker — written from the code on every start, never hand-maintained,
        so the web and `/rolemenu template` can't disagree. Best-effort: a docs
        artifact must never stop the cog loading."""
        path = os.getenv("TORVEX_ROLE_TEMPLATES_JSON") or os.path.join(
            os.path.dirname(DB_PATH), "role_templates.json")
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(as_json(), f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("role templates export failed: %s", e)

    async def cog_unload(self):
        self.panel_sync.cancel()

    @tasks.loop(seconds=SYNC_SECONDS)
    async def panel_sync(self):
        """Publish panels the dashboard has edited.

        A row written by the dashboard is invisible to Discord until the bot
        posts the message AND registers a persistent view for it — without the
        view, every click dies as "This interaction failed" (the bug that killed
        panels across restarts in July). So the dashboard only ever marks work,
        and all Discord writes stay here.
        """
        try:
            with _conn() as c:
                todo = c.execute(
                    "SELECT panel_id, guild_id, channel_id, message_id, state FROM panels "
                    "WHERE state IN ('dirty','delete')").fetchall()
        except sqlite3.Error:
            return
        for p in todo:
            guild = self.bot.get_guild(int(p["guild_id"])) if p["guild_id"] else None
            if guild is None:
                continue          # not our guild / not cached yet — try again next tick
            try:
                if p["state"] == "delete":
                    await self._delete_panel(guild, p)
                else:
                    await self._publish_panel(guild, p["panel_id"])
            except discord.Forbidden:
                log.warning("panel %s: missing permissions in %s", p["panel_id"], guild.id)
                self._clear_state(p["panel_id"])      # don't spin on a permission problem
            except discord.HTTPException as e:
                log.warning("panel %s: %s", p["panel_id"], e)

    @panel_sync.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()

    def _clear_state(self, panel_id):
        with _conn() as c:
            c.execute("UPDATE panels SET state=NULL WHERE panel_id=?", (panel_id,))

    async def _publish_panel(self, guild, panel_id):
        """Render, then refresh the button registration.

        _render only registers on a fresh post; an EDIT that changed the roles
        would otherwise leave the old buttons bound, so re-register explicitly.
        """
        await self._materialise_roles(guild, panel_id)
        await self._render(guild, panel_id, lambda m: None)
        with _conn() as c:
            p = c.execute("SELECT message_id FROM panels WHERE panel_id=?", (panel_id,)).fetchone()
            roles = _button_rows(c, panel_id)
        if p and p["message_id"] and roles:
            self.bot.add_view(_build_view(panel_id, roles), message_id=int(p["message_id"]))
        self._clear_state(panel_id)

    async def _materialise_roles(self, guild, panel_id):
        """Create the roles a dashboard-saved panel still lacks.

        A row with no role_id names a role by its label. An existing role of
        that name is adopted (unless it sits at or above my own role — that
        button could never be fulfilled); otherwise it is created permissionless,
        exactly as `/rolemenu template` would. A row that can't be resolved stays
        unresolved — it is simply not rendered, and the next save retries it —
        rather than failing the whole panel.
        """
        with _conn() as c:
            pending = c.execute("SELECT rowid, label, colour FROM panel_roles "
                                "WHERE panel_id=? AND role_id IS NULL", (panel_id,)).fetchall()
        if not pending:
            return
        me = guild.me
        by_name = {r.name.casefold(): r for r in guild.roles}
        for row in pending:
            name = (row["label"] or "").strip()[:100]
            if not name:
                continue
            role = by_name.get(name.casefold())
            action = plan_template_role(role, me.top_role)
            if action == "blocked":
                log.warning("panel %s: role %r sits above me, skipped", panel_id, name)
                continue
            if action == "create":
                if not me.guild_permissions.manage_roles:
                    log.warning("panel %s: no Manage Roles, can't create %r", panel_id, name)
                    return
                try:
                    role = await guild.create_role(
                        name=name, colour=discord.Colour(int(row["colour"] or 0)),
                        permissions=discord.Permissions.none(),
                        hoist=False, mentionable=False,
                        reason=f"role menu panel #{panel_id} (dashboard)")
                    by_name[name.casefold()] = role
                    await asyncio.sleep(0.4)   # role creation is a tight bucket
                except discord.HTTPException as e:
                    log.warning("panel %s: couldn't create role %r: %s", panel_id, name, e)
                    continue
            with _conn() as c:
                c.execute("UPDATE panel_roles SET role_id=? WHERE rowid=?", (str(role.id), row["rowid"]))

    async def _delete_panel(self, guild, p):
        channel = guild.get_channel(int(p["channel_id"])) if p["channel_id"] else None
        if channel is not None and p["message_id"]:
            try:
                msg = await channel.fetch_message(int(p["message_id"]))
                await msg.delete()
            except discord.HTTPException:
                pass          # already gone by hand — the rows still need clearing
        with _conn() as c:
            c.execute("DELETE FROM panel_roles WHERE panel_id=?", (p["panel_id"],))
            c.execute("DELETE FROM panels WHERE panel_id=?", (p["panel_id"],))

    async def _render(self, guild, panel_id, notify):
        """(Re)post or edit a panel's message. `notify` is a coroutine factory for
        the ephemeral confirmation (so callers control response vs followup)."""
        with _conn() as c:
            p = c.execute("SELECT * FROM panels WHERE panel_id=?", (panel_id,)).fetchone()
            roles = _button_rows(c, panel_id)
        channel = guild.get_channel(int(p["channel_id"]))

        async def _tell(text):
            r = notify(text)
            if r is not None:
                await r

        if channel is None:
            await _tell("⚠️ That panel's channel is gone.")
            return
        embed = discord.Embed(title=p["title"], description=p["description"] or None, color=0x5B8CFF)
        view = _build_view(panel_id, roles)
        msg = None
        if p["message_id"]:
            try:
                msg = await channel.fetch_message(int(p["message_id"]))
            except discord.HTTPException:
                msg = None
        if msg:
            await msg.edit(embed=embed, view=view)
        else:
            sent = await channel.send(embed=embed, view=view)
            with _conn() as c:
                c.execute("UPDATE panels SET message_id=? WHERE panel_id=?", (str(sent.id), panel_id))
            if roles:
                self.bot.add_view(view, message_id=sent.id)
        await _tell(f"✅ Panel **#{panel_id}** ({len(roles)} role{'s' if len(roles) != 1 else ''}) posted in {channel.mention}.")

    group = app_commands.Group(
        name="rolemenu", description="Self-assign role panels (admin)",
        default_permissions=discord.Permissions(manage_roles=True), guild_only=True)

    @group.command(name="create", description="Create an empty role panel for a channel")
    @app_commands.describe(channel="where the panel is posted", title="panel heading", description="optional blurb")
    async def create(self, interaction: discord.Interaction, channel: discord.TextChannel,
                     title: str, description: str = None):
        with _conn() as c:
            cur = c.execute("INSERT INTO panels(guild_id,channel_id,message_id,title,description) VALUES(?,?,?,?,?)",
                            (str(interaction.guild.id), str(channel.id), None, title, description or ""))
            pid = cur.lastrowid
        await interaction.response.send_message(
            f"✅ Panel **#{pid}** created for {channel.mention}. Add roles with "
            f"`/rolemenu addrole panel:{pid} role:@Role` — it posts once it has one.", ephemeral=True)

    @group.command(name="addrole", description="Add a role button to a panel")
    @app_commands.describe(panel="panel number", role="role to hand out",
                           label="button text (defaults to role name)",
                           emoji="any emoji — standard, or a custom one from a server I'm in")
    async def addrole(self, interaction: discord.Interaction, panel: int, role: discord.Role,
                      label: str = None, emoji: str = None):
        await interaction.response.defer(ephemeral=True)
        emoji, err = parse_emoji(emoji, self.bot.get_emoji)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        with _conn() as c:
            p = c.execute("SELECT 1 FROM panels WHERE panel_id=? AND guild_id=?", (panel, str(interaction.guild.id))).fetchone()
            if not p:
                await interaction.followup.send(f"No panel #{panel} here.", ephemeral=True)
                return
            n = c.execute("SELECT COUNT(*) FROM panel_roles WHERE panel_id=?", (panel,)).fetchone()[0]
            if n >= 25:
                await interaction.followup.send("Panel is full (25 buttons max) — make a second panel.", ephemeral=True)
                return
            if c.execute("SELECT 1 FROM panel_roles WHERE panel_id=? AND role_id=?", (panel, str(role.id))).fetchone():
                await interaction.followup.send(f"{role.mention} is already on panel #{panel}.", ephemeral=True)
                return
            if role >= interaction.guild.me.top_role:
                await interaction.followup.send(
                    f"{role.mention} is above my top role — move **Torvex Forerunner** higher or I can't assign it.", ephemeral=True)
                return
            c.execute("INSERT INTO panel_roles(panel_id,role_id,label,emoji,pos) VALUES(?,?,?,?,?)",
                      (panel, str(role.id), label or role.name, emoji, n))
        await self._render(interaction.guild, panel, lambda m: interaction.followup.send(m, ephemeral=True))

    @group.command(name="removerole", description="Remove a role button from a panel")
    async def removerole(self, interaction: discord.Interaction, panel: int, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        with _conn() as c:
            cur = c.execute("DELETE FROM panel_roles WHERE panel_id=? AND role_id=?", (panel, str(role.id)))
            if cur.rowcount == 0:
                await interaction.followup.send(f"{role.mention} isn't on panel #{panel}.", ephemeral=True)
                return
        await self._render(interaction.guild, panel, lambda m: interaction.followup.send(m, ephemeral=True))

    @group.command(name="list", description="List this server's role panels")
    async def list_panels(self, interaction: discord.Interaction):
        with _conn() as c:
            panels = c.execute("SELECT * FROM panels WHERE guild_id=? ORDER BY panel_id", (str(interaction.guild.id),)).fetchall()
            if not panels:
                await interaction.response.send_message("No panels yet. `/rolemenu bootstrap` or `/rolemenu create`.", ephemeral=True)
                return
            lines = []
            for p in panels:
                roles = c.execute("SELECT label FROM panel_roles WHERE panel_id=? ORDER BY pos", (p["panel_id"],)).fetchall()
                lines.append(f"**#{p['panel_id']}** {p['title']} — <#{p['channel_id']}> · {len(roles)} roles: "
                             f"{', '.join(r['label'] for r in roles) or '(empty)'}")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @group.command(name="delete", description="Delete a panel (and its message)")
    async def delete(self, interaction: discord.Interaction, panel: int):
        await interaction.response.defer(ephemeral=True)
        with _conn() as c:
            p = c.execute("SELECT * FROM panels WHERE panel_id=? AND guild_id=?", (panel, str(interaction.guild.id))).fetchone()
            if not p:
                await interaction.followup.send(f"No panel #{panel} here.", ephemeral=True)
                return
            if p["message_id"]:
                ch = interaction.guild.get_channel(int(p["channel_id"]))
                if ch:
                    try:
                        m = await ch.fetch_message(int(p["message_id"]))
                        await m.delete()
                    except discord.HTTPException:
                        pass
            c.execute("DELETE FROM panel_roles WHERE panel_id=?", (panel,))
            c.execute("DELETE FROM panels WHERE panel_id=?", (panel,))
        await interaction.followup.send(f"🗑️ Panel #{panel} deleted.", ephemeral=True)

    @group.command(name="set-emoji", description="Change (or clear) the emoji on a role button")
    @app_commands.describe(panel="panel number", role="which button",
                           emoji="any emoji, or leave blank to remove it",
                           label="optional: also change the button text")
    async def set_emoji(self, interaction: discord.Interaction, panel: int, role: discord.Role,
                        emoji: str = None, label: str = None):
        await interaction.response.defer(ephemeral=True)
        value, err = parse_emoji(emoji, self.bot.get_emoji)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        with _conn() as c:
            owned = c.execute("SELECT 1 FROM panels WHERE panel_id=? AND guild_id=?",
                              (panel, str(interaction.guild.id))).fetchone()
            if not owned:
                await interaction.followup.send(f"No panel #{panel} here.", ephemeral=True)
                return
            fields, args = ["emoji=?"], [value]
            if label:
                fields.append("label=?")
                args.append(label)
            args += [panel, str(role.id)]
            changed = c.execute(f"UPDATE panel_roles SET {', '.join(fields)} "
                                "WHERE panel_id=? AND role_id=?", args).rowcount
        if not changed:
            await interaction.followup.send(
                f"{role.mention} isn't on panel #{panel} — add it with `/rolemenu addrole`.",
                ephemeral=True)
            return
        await self._render(interaction.guild, panel,
                           lambda m: interaction.followup.send(
                               (f"✅ {role.mention} now shows {value}" if value
                                else f"✅ Emoji removed from {role.mention}") + f" · {m}",
                               ephemeral=True))

    @group.command(name="template",
                   description="Post a ready-made role panel — creates any roles you don't have yet")
    @app_commands.describe(template="Which set to build", channel="Where to post the panel")
    @app_commands.choices(template=[
        app_commands.Choice(name=title, value=key) for key, title in template_choices()])
    async def template_cmd(self, interaction: discord.Interaction,
                           template: app_commands.Choice[str],
                           channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        guild, spec = interaction.guild, TEMPLATES[template.value]

        me = guild.me
        if not me.guild_permissions.manage_roles:
            await interaction.followup.send(
                "❌ I need **Manage Roles** to create and hand out these roles.", ephemeral=True)
            return

        by_name = {r.name.casefold(): r for r in guild.roles}
        entries, created, reused, blocked = [], [], [], []
        for name, emoji, colour in spec["roles"]:
            role = by_name.get(name.casefold())
            action = plan_template_role(role, me.top_role)
            if action == "blocked":
                blocked.append(f"{name} (sits above my role)")
                continue
            if action == "create":
                try:
                    role = await guild.create_role(
                        name=name, colour=discord.Colour(colour),
                        permissions=discord.Permissions.none(),
                        hoist=False, mentionable=False,
                        reason=f"role menu template: {template.value}")
                    created.append(role)
                    await asyncio.sleep(0.4)   # role creation is a tight bucket
                except discord.Forbidden:
                    blocked.append(f"{name} (no permission to create)")
                    continue
                except discord.HTTPException as e:
                    # 30005 = max roles reached; anything else is transient
                    why = ("this server is at Discord's 250-role limit"
                           if getattr(e, "code", 0) == 30005 else "creation failed")
                    blocked.append(f"{name} ({why})")
                    continue
            else:
                reused.append(role)
            entries.append((role, name, emoji))

        if not entries:
            await interaction.followup.send(
                "❌ Couldn't set up any of those roles.\n" + "\n".join(f"• {b}" for b in blocked[:10]),
                ephemeral=True)
            return

        with _conn() as c:
            pid = c.execute(
                "INSERT INTO panels(guild_id,channel_id,message_id,title,description,exclusive) "
                "VALUES(?,?,?,?,?,?)",
                (str(guild.id), str(channel.id), None, spec["title"], spec["blurb"],
                 1 if spec["exclusive"] else 0)).lastrowid
            for pos, (role, label, emoji) in enumerate(entries):
                c.execute("INSERT INTO panel_roles(panel_id,role_id,label,emoji,pos) VALUES(?,?,?,?,?)",
                          (pid, str(role.id), label, emoji, pos))
        await self._render(guild, pid, lambda m: None)

        lines = [f"✅ **{spec['title']}** posted in {channel.mention} as panel **#{pid}**"
                 + (" · members can pick **one**." if spec["exclusive"] else ".")]
        if created:
            lines.append(f"🆕 Created {len(created)} role(s): " + ", ".join(r.mention for r in created))
        if reused:
            lines.append(f"♻️ Reused {len(reused)} existing role(s): " + ", ".join(r.mention for r in reused))
        if blocked:
            lines.append("⚠️ Skipped: " + ", ".join(blocked))
        if created:
            lines.append("-# New roles have no permissions and sit at the bottom — "
                         "move them where you like, just keep them below my role.")
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    @group.command(name="bootstrap", description="Recreate the full MEE6 reaction-roles set as panels in a channel")
    @app_commands.describe(channel="channel to post the panels in (e.g. #reaction-roles)")
    async def bootstrap(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        made, skipped, missing = [], [], []
        me_top = guild.me.top_role
        with _conn() as c:
            existing = {r["title"] for r in c.execute("SELECT title FROM panels WHERE guild_id=?", (str(guild.id),)).fetchall()}
        for title, entries in BOOTSTRAP.items():
            if title in existing:
                skipped.append(title)
                continue
            live = []
            for rid, label, emoji in entries:
                role = guild.get_role(rid)
                if role is None:
                    missing.append(f"{label} ({rid})")
                elif role >= me_top:
                    missing.append(f"{label} (above my role)")
                else:
                    live.append((role, label, emoji))
            if not live:
                continue
            with _conn() as c:
                pid = c.execute("INSERT INTO panels(guild_id,channel_id,message_id,title,description) VALUES(?,?,?,?,?)",
                                (str(guild.id), str(channel.id), None, title, BOOTSTRAP_BLURB.get(title, ""))).lastrowid
                for pos, (role, label, emoji) in enumerate(live):
                    c.execute("INSERT INTO panel_roles(panel_id,role_id,label,emoji,pos) VALUES(?,?,?,?,?)",
                              (pid, str(role.id), label, emoji, pos))
            await self._render(guild, pid, lambda m: None)  # post silently; summary sent below
            made.append(f"#{pid} {title} ({len(live)})")
        msg = "✅ Bootstrap done.\n**Created:** " + (", ".join(made) or "none")
        if skipped:
            msg += f"\n**Skipped (already exist):** {', '.join(skipped)}"
        if missing:
            msg += f"\n⚠️ **Couldn't add:** {', '.join(missing)}"
        msg += "\n\nCheck the panels look right, then it's safe to kick MEE6."
        await interaction.followup.send(msg[:1900], ephemeral=True)


async def setup(bot):
    await bot.add_cog(RoleMenu(bot))
