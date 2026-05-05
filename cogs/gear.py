import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import math

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")
HEADERS = {"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}

RARITY_COLORS = {
    "Common":    0xAAAAAA,
    "Uncommon":  0x55FF55,
    "Rare":      0x5599FF,
    "Epic":      0xAA44FF,
    "Legendary": 0xFFAA00,
}

ELEMENT_INFO = {
    "None":      ("⚪", "Physical — no elemental affinity."),
    "Fire":      ("🔥", "Strong vs Ice. Weak to Lightning."),
    "Ice":       ("❄️", "Strong vs Lightning. Weak to Fire."),
    "Lightning": ("⚡", "Strong vs Earth. Weak to Ice."),
    "Earth":     ("🌿", "Strong vs Fire. Weak to Lightning."),
    "Dark":      ("🌑", "Strong vs Holy. Weak to Holy."),
    "Holy":      ("✨", "Strong vs Dark. Weak to Dark."),
}

PAGES_PER_EMBED = 8  # items per embed page


async def _fetch(path: str) -> list | None:
    url = f"{TORVEX_API_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=HEADERS) as r:
                if r.status == 200:
                    return await r.json()
    except Exception:
        pass
    return None


def _rarity_stars(rarity: str) -> str:
    return {"Common": "★", "Uncommon": "★★", "Rare": "★★★",
            "Epic": "★★★★", "Legendary": "★★★★★"}.get(rarity, "")


def _stat_line(item: dict) -> str:
    parts = []
    if item.get("minDamage"):
        parts.append(f"DMG {item['minDamage']}–{item['maxDamage']}")
    for stat, label in [("bonusSTR", "STR"), ("bonusDEF", "DEF"), ("bonusINT", "INT"),
                        ("bonusDEX", "DEX"), ("bonusVIT", "VIT"), ("bonusLUK", "LUK")]:
        if item.get(stat):
            parts.append(f"+{item[stat]} {label}")
    el = item.get("element", "None")
    if el and el != "None":
        parts.append(f"{ELEMENT_INFO.get(el, ('',''))[0]} {el}")
    return " | ".join(parts) if parts else "No bonuses"


def _weapon_pages(weapons: list) -> list[discord.Embed]:
    pages = []
    total = len(weapons)
    for i in range(0, total, PAGES_PER_EMBED):
        chunk = weapons[i:i + PAGES_PER_EMBED]
        page_num = i // PAGES_PER_EMBED + 1
        total_pages = math.ceil(total / PAGES_PER_EMBED)
        embed = discord.Embed(
            title=f"⚔️ Weapon Dictionary  (page {page_num}/{total_pages})",
            color=0xE8B84B
        )
        for w in chunk:
            stars = _rarity_stars(w["rarity"])
            embed.add_field(
                name=f"{w['icon']} {w['name']}  Lv.{w['levelReq']}  {stars}",
                value=_stat_line(w),
                inline=False
            )
        embed.set_footer(text="Use /rpg equip <name> to equip · /rpg inventory to check your gear")
        pages.append(embed)
    return pages


def _armor_pages(armor: list) -> list[discord.Embed]:
    # Group by set (level band)
    sets: dict[str, list] = {}
    for a in armor:
        band = f"Lv. {a['levelReq']}"
        sets.setdefault(band, []).append(a)

    pages = []
    items = list(sets.items())
    for i in range(0, len(items), 3):
        chunk = items[i:i + 3]
        page_num = i // 3 + 1
        total_pages = math.ceil(len(items) / 3)
        embed = discord.Embed(
            title=f"🛡️ Armor Dictionary  (page {page_num}/{total_pages})",
            color=0x6B8CFF
        )
        for band, pieces in chunk:
            lines = []
            for p in sorted(pieces, key=lambda x: x["equipSlot"] or ""):
                slot = (p.get("equipSlot") or "").replace("OffHand", "Off Hand")
                stars = _rarity_stars(p["rarity"])
                bonuses = []
                for stat, label in [("bonusDEF", "DEF"), ("bonusSTR", "STR"), ("bonusINT", "INT"),
                                    ("bonusDEX", "DEX"), ("bonusVIT", "VIT"), ("bonusLUK", "LUK")]:
                    if p.get(stat):
                        bonuses.append(f"+{p[stat]} {label}")
                bonus_str = " ".join(bonuses) or "—"
                lines.append(f"{p['icon']} **{p['name']}** `{slot}` {stars}\n  ↳ {bonus_str}")
            embed.add_field(name=band, value="\n".join(lines), inline=False)
        embed.set_footer(text="Use /rpg equip <name> to equip")
        pages.append(embed)
    return pages


def _element_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🌀 Element Guide",
        description=(
            "Elements determine attack bonuses and weaknesses in combat.\n"
            "**1.25× damage** when strong against an enemy's element.\n"
            "Weapon element matters for your attacks — monster element matters for incoming damage.\n\n"
            "*Detailed weakness/resistance system coming soon.*"
        ),
        color=0xFF9900
    )
    cycle = "🔥 Fire  →  ❄️ Ice  →  ⚡ Lightning  →  🌿 Earth  →  🔥 Fire"
    embed.add_field(name="Elemental Cycle", value=cycle, inline=False)
    embed.add_field(name="Light vs Dark", value="✨ Holy  ⟷  🌑 Dark  (each strong against the other)", inline=False)
    embed.add_field(name="⚪ Physical (None)", value="No elemental bonus or penalty.", inline=False)

    for el, (icon, desc) in ELEMENT_INFO.items():
        if el == "None":
            continue
        embed.add_field(name=f"{icon} {el}", value=desc, inline=True)

    embed.set_footer(text="Full elemental strength chart coming in a future update")
    return embed


class GearPageView(discord.ui.View):
    """Simple prev/next paginator for multi-page embeds."""

    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=120)
        self.pages = pages
        self.current = 0
        if len(pages) <= 1:
            self.prev_btn.disabled = True
            self.next_btn.disabled = True

    def _sync_buttons(self):
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = max(0, self.current - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = min(len(self.pages) - 1, self.current + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)


class Gear(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    gear = app_commands.Group(name="gear", description="Browse weapons, armor, and elements.")

    @gear.command(name="weapons", description="Browse all weapons sorted by level.")
    async def weapons(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await _fetch("/api/bot/items?type=Weapon")
        if not data:
            await interaction.followup.send("❌ Could not load weapon data.", ephemeral=True)
            return
        pages = _weapon_pages(data)
        view = GearPageView(pages)
        view.prev_btn.disabled = True
        if len(pages) <= 1:
            view.next_btn.disabled = True
        await interaction.followup.send(embed=pages[0], view=view)

    @gear.command(name="armor", description="Browse all armor sets sorted by level.")
    async def armor(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await _fetch("/api/bot/items?type=Armor")
        if not data:
            await interaction.followup.send("❌ Could not load armor data.", ephemeral=True)
            return
        pages = _armor_pages(data)
        view = GearPageView(pages)
        view.prev_btn.disabled = True
        if len(pages) <= 1:
            view.next_btn.disabled = True
        await interaction.followup.send(embed=pages[0], view=view)

    @gear.command(name="elements", description="Learn about the elemental system.")
    async def elements(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_element_embed())

    @gear.command(name="monsters", description="Browse monsters by zone.")
    @app_commands.describe(zone="Filter by zone (Plains, Forest, Mountains, Dungeon, Volcano, Abyss)")
    @app_commands.choices(zone=[
        app_commands.Choice(name="Plains",    value="Plains"),
        app_commands.Choice(name="Forest",    value="Forest"),
        app_commands.Choice(name="Mountains", value="Mountains"),
        app_commands.Choice(name="Dungeon",   value="Dungeon"),
        app_commands.Choice(name="Volcano",   value="Volcano"),
        app_commands.Choice(name="Abyss",     value="Abyss"),
    ])
    async def monsters(self, interaction: discord.Interaction, zone: str = ""):
        await interaction.response.defer()
        path = f"/api/bot/monsters?zone={zone}" if zone else "/api/bot/monsters"
        data = await _fetch(path)
        if not data:
            await interaction.followup.send("❌ Could not load monster data.", ephemeral=True)
            return

        pages = []
        for i in range(0, len(data), 10):
            chunk = data[i:i + 10]
            page_num = i // 10 + 1
            total_pages = math.ceil(len(data) / 10)
            title = f"👹 Monsters — {zone or 'All Zones'}  (page {page_num}/{total_pages})"
            embed = discord.Embed(title=title, color=0xFF4444)
            for m in chunk:
                el = m.get("element", "None")
                el_icon = ELEMENT_INFO.get(el, ("⚪", ""))[0]
                embed.add_field(
                    name=f"{m['icon']} {m['name']}  Lv.{m['level']}",
                    value=f"Zone: {m['zone']}  {el_icon} {el}  |  ❤️ {m['maxHp']} HP  |  ✨ {m['xp']} XP",
                    inline=False
                )
            embed.set_footer(text="Use /rpg fight <name> to fight a specific monster")
            pages.append(embed)

        view = GearPageView(pages)
        view.prev_btn.disabled = True
        if len(pages) <= 1:
            view.next_btn.disabled = True
        await interaction.followup.send(embed=pages[0], view=view)


async def setup(bot):
    await bot.add_cog(Gear(bot))
