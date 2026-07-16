"""Self-assign role panels for peepos-reclaimer — replaces MEE6 / carl-bot
reaction roles with persistent BUTTON panels.

An admin creates a panel and adds role-buttons (or runs /rolemenu bootstrap to
recreate the whole reaction-roles set at once); members click a button to toggle
the role on themselves. Buttons survive restarts: on ready we re-register a
persistent View for every stored panel (custom_id = "rm:<panel>:<role>").

Why buttons over reactions: they don't get lost on bot downtime, need no Manage
Emoji, and the grant is done by THIS bot — which anti-nuke exempts — so a burst
of members self-assigning never trips the nuke detector (the carl-bot problem).
"""
import os
import sys
import sqlite3
import logging

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

log = logging.getLogger("role_menu")
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "role_menus.db"))

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


_init()


class RoleButton(discord.ui.Button):
    def __init__(self, panel_id, role_id, label, emoji):
        super().__init__(style=discord.ButtonStyle.secondary, label=label or None,
                         emoji=emoji or None, custom_id=f"rm:{panel_id}:{role_id}")
        self.role_id = int(role_id)

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
                # mutually-exclusive group (e.g. age band): drop the others first
                group = _exclusive_group(self.role_id)
                if group:
                    others = [r for r in member.roles if r.id in group and r.id != self.role_id]
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
                roles = c.execute("SELECT * FROM panel_roles WHERE panel_id=? ORDER BY pos", (p["panel_id"],)).fetchall()
                if roles:
                    self.bot.add_view(_build_view(p["panel_id"], roles))
                    n += 1
        print(f"role menus: registered {n} persistent panels")

    async def _render(self, guild, panel_id, notify):
        """(Re)post or edit a panel's message. `notify` is a coroutine factory for
        the ephemeral confirmation (so callers control response vs followup)."""
        with _conn() as c:
            p = c.execute("SELECT * FROM panels WHERE panel_id=?", (panel_id,)).fetchone()
            roles = c.execute("SELECT * FROM panel_roles WHERE panel_id=? ORDER BY pos", (panel_id,)).fetchall()
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
    @app_commands.describe(panel="panel number", role="role to hand out", label="button text (defaults to role name)", emoji="optional emoji")
    async def addrole(self, interaction: discord.Interaction, panel: int, role: discord.Role,
                      label: str = None, emoji: str = None):
        await interaction.response.defer(ephemeral=True)
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
                    f"{role.mention} is above my top role — move **Peepo's Reclaimer** higher or I can't assign it.", ephemeral=True)
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
