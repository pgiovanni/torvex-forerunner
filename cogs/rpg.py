import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import logging
from cogs.pvp import ChallengeView

log = logging.getLogger("rpg")

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")

HEADERS = {"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}


async def _api(method: str, path: str, **kwargs):
    url = f"{TORVEX_API_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=HEADERS, **kwargs) as r:
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


async def _add_coins(discord_id: str, amount: int, reason: str):
    """Award coins to a player's CoinBalance via the API."""
    await _api("POST", "/api/bot/game/add-coins", json={
        "discordId": discord_id,
        "amount": amount,
        "reason": reason
    })


def _embed(title: str, description: str = "", color: int = 0x5B8CDB) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


_ELEMENT_ICON = {
    "None": "⚪", "Fire": "🔥", "Ice": "❄️",
    "Lightning": "⚡", "Earth": "🌿", "Dark": "🌑", "Holy": "✨",
}

def _hp_bar(current: int, maximum: int, length: int = 10) -> str:
    if maximum <= 0:
        return "░" * length
    filled = round(length * current / maximum)
    return "█" * filled + "░" * (length - filled)


def _game_embed(response: dict) -> discord.Embed | None:
    """Convert a Torvex GameResponse payload into a Discord embed."""
    rtype = response.get("type", "")
    p = response.get("payload", {})

    if rtype == "error":
        return _embed("Error", p.get("message", "Unknown error."), color=0xFF4444)

    if rtype == "help":
        lines = p.get("lines") or []
        return _embed("📖 RPG Commands", "\n".join(lines) if lines else str(p))

    if rtype == "stats":
        desc = (
            f"**{p.get('characterName')}** — {p.get('class')} Lv.**{p.get('level')}**\n"
            f"XP: {p.get('xp'):,}/{p.get('xpToNext'):,}\n\n"
            f"❤️ HP `{_hp_bar(p.get('currentHp',0), p.get('maxHp',1))}` {p.get('currentHp')}/{p.get('maxHp')}\n"
            f"💧 MP `{_hp_bar(p.get('currentMp',0), p.get('maxMp',1))}` {p.get('currentMp')}/{p.get('maxMp')}\n\n"
            f"⚔️ STR **{p.get('str')}**  🛡️ DEF **{p.get('def')}**  🔮 INT **{p.get('int')}**\n"
            f"💨 DEX **{p.get('dex')}**  💚 VIT **{p.get('vit')}**  🍀 LUK **{p.get('luk')}**\n\n"
            f"🗡️ Kills: **{p.get('kills')}**  💀 Deaths: **{p.get('deaths')}**\n"
            f"🪙 Coins: **{p.get('coinBalance', 0):,}**"
        )
        skills = p.get("skills") or []
        skill_icons = {"Combat": "⚔️", "Mining": "⛏️", "Smithing": "🔨", "Woodcutting": "🪓",
                       "Alchemy": "⚗️", "Fishing": "🎣", "Cooking": "🍳", "Enchanting": "✨"}
        if skills:
            skill_lines = "  ".join(
                f"{skill_icons.get(s['skill'], '📊')} {s['skill']} **{s['level']}**"
                for s in skills
            )
            desc += f"\n\n{skill_lines}"
        return _embed("🧙 Character Stats", desc)

    if rtype == "combat_start":
        icon    = p.get("monsterIcon", "👹")
        name    = p.get("monsterName", "???")
        level   = p.get("monsterLevel", "?")
        zone    = p.get("monsterZone", "")
        el      = p.get("monsterElement", "None")
        el_icon = _ELEMENT_ICON.get(el, "⚪")
        m_hp    = p.get("monsterHp", 0)
        m_max   = p.get("monsterMaxHp", 0)
        p_name  = p.get("playerName", "You")
        p_hp    = p.get("playerHp", 0)
        p_max   = p.get("playerMaxHp", 0)
        desc = (
            f"{icon} **{name}** (Lv.{level}) appears!\n"
            f"📍 {zone}  {el_icon} {el}\n\n"
            f"{icon} {name}\n"
            f"❤️ `{_hp_bar(m_hp, m_max)}` {m_hp}/{m_max}\n\n"
            f"🧙 {p_name}\n"
            f"❤️ `{_hp_bar(p_hp, p_max)}` {p_hp}/{p_max}\n\n"
            f"*Use* `/rpg attack` *·* `/rpg defend` *·* `/rpg magic` *·* `/rpg flee`"
        )
        return _embed("⚔️ Combat — Battle Start!", desc, color=0xFF6600)

    if rtype in ("combat_turn", "combat_end"):
        log          = p.get("log") or []
        state        = p.get("state", "")
        m_name       = p.get("monsterName", "???")
        m_hp         = p.get("monsterHp", 0)
        m_max        = p.get("monsterMaxHp", 0)
        p_name       = p.get("playerName", "You")
        p_hp         = p.get("playerHp", 0)
        p_max        = p.get("playerMaxHp", 0)
        p_mp         = p.get("playerMp", 0)
        p_mp_max     = p.get("playerMaxMp", 0)
        turn         = p.get("turn", 0)
        result       = p.get("combatResult") or {}

        log_text = "\n".join(f"▸ {line}" for line in log) if log else "…"

        if state == "Victory":
            xp        = result.get("xpGained", 0)
            coins     = result.get("coinsGained", 0)
            loot      = result.get("loot") or []
            leveled   = result.get("leveledUp", False)
            new_lvl   = result.get("newLevel")
            loot_str  = ("  🎒 " + "  ".join(
                f"**{i['name']}** x{i['quantity']} *[{i['rarity']}]*" for i in loot
            )) if loot else ""
            lvl_str   = f"\n⬆️ **LEVEL UP! Now Level {new_lvl}!**" if leveled else ""
            desc = (
                f"{log_text}\n\n"
                f"✅ **Victory!**\n"
                f"✨ +**{xp:,} XP**  🪙 +**{coins:,} Coins**"
                f"{loot_str}{lvl_str}"
            )
            return _embed("⚔️ Combat — Victory!", desc, color=0x00FF88)

        if state == "Defeat":
            lost = result.get("coinsLost", 0)
            desc = (
                f"{log_text}\n\n"
                f"💀 **Defeated!**"
                + (f"  You lost **{lost:,} Coins**." if lost else "")
                + "\n*Respawned at 25% HP — get back out there!*"
            )
            return _embed("⚔️ Combat — Defeated", desc, color=0xFF4444)

        if state == "Fled":
            desc = f"{log_text}\n\n💨 **Fled from battle!**"
            return _embed("⚔️ Combat — Fled", desc, color=0xFFAA00)

        # Mid-battle turn
        desc = (
            f"{log_text}\n\n"
            f"👹 {m_name}\n"
            f"❤️ `{_hp_bar(m_hp, m_max)}` {m_hp}/{m_max}\n\n"
            f"🧙 {p_name}\n"
            f"❤️ `{_hp_bar(p_hp, p_max)}` {p_hp}/{p_max}  "
            f"💧 `{_hp_bar(p_mp, p_mp_max)}` {p_mp}/{p_mp_max}\n\n"
            f"*Turn {turn} — your move*"
        )
        return _embed("⚔️ Combat", desc, color=0xFF6600)

    if rtype == "inventory":
        items = p.get("items") or []
        if not items:
            desc = "Your inventory is empty."
        else:
            lines = []
            for item in items:
                equipped = " *(equipped)*" if item.get("isEquipped") else ""
                lines.append(
                    f"{'✅' if item.get('isEquipped') else '•'} "
                    f"**{item['name']}** x{item.get('quantity', 1)} "
                    f"`{item.get('rarity')}`{equipped}"
                )
            desc = "\n".join(lines)
        return _embed("🎒 Inventory", desc)

    if rtype == "leaderboard":
        entries = p.get("entries") or []
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{e['characterName']}** — "
            f"Lv.{e['level']} | {e['totalMonstersKilled']} kills"
            for i, e in enumerate(entries)
        ]
        return _embed("🏆 Leaderboard", "\n".join(lines) if lines else "No players yet.")

    if rtype == "gather":
        action_icons = {"mine": "⛏️", "fish": "🎣", "chop": "🪓"}
        icon      = action_icons.get(p.get("action", ""), "⚒️")
        item      = p.get("item", "something")
        qty       = p.get("quantity", 1)
        xp        = p.get("xpGained", 0)
        slvl      = p.get("skillLevel", 1)
        sxp       = p.get("skillXp", 0)
        snext     = p.get("skillXpToNext", 100)
        bonus_gem = p.get("bonusGem")
        gem_line  = f"\n💎 Bonus drop: **{bonus_gem}**!" if bonus_gem else ""
        desc = (
            f"{icon} You gathered **{item}** x**{qty}**!{gem_line}\n\n"
            f"✨ +**{xp} skill XP**\n"
            f"📊 Skill Level **{slvl}** — `{_hp_bar(sxp, snext)}` {sxp}/{snext} XP"
        )
        return _embed(f"{icon} Gathering", desc, color=0x66BB6A)

    if rtype == "craft":
        success = p.get("success", False)
        recipe  = p.get("recipe", "")
        xp      = p.get("xpGained", 0)
        slvl    = p.get("skillLevel", 1)
        if success:
            out  = p.get("outputItem", "item")
            qty  = p.get("outputQty", 1)
            desc = f"🔨 Crafted **{out}** x**{qty}**!\n✨ +**{xp} XP**  📊 Crafting Lv.**{slvl}**"
            return _embed("🔨 Crafting — Success!", desc, color=0x66BB6A)
        else:
            desc = f"❌ Crafting **{recipe}** failed!\n✨ +**{xp} XP** (partial)  📊 Crafting Lv.**{slvl}**"
            return _embed("🔨 Crafting — Failed", desc, color=0xFF9800)

    if rtype == "recipes":
        recipes = p.get("recipes") or []
        if not recipes:
            return _embed("🔨 Recipes", "No recipes available yet.")
        lines = []
        for r in recipes:
            ings = ", ".join(f"{i['qty']}x {i['name']}" for i in r.get("ingredients", []))
            orb  = f" + {r['orbCost']} orbs" if r.get("orbCost") else ""
            lines.append(
                f"**{r['name']}** → {r['output']}\n"
                f"  `{r['skill']}` Lv.{r['skillLevel']} required  |  {ings}{orb}"
            )
        return _embed("🔨 Crafting Recipes", "\n\n".join(lines))

    if rtype == "equip":
        item  = p.get("item", "item")
        slot  = p.get("slot", "")
        prev  = p.get("unequipped")
        desc  = f"✅ Equipped **{item}** in `{slot}`"
        if prev:
            desc += f"\n*(replaced **{prev}**)*"
        return _embed("⚔️ Equipped", desc, color=0x66BB6A)

    if rtype == "unequip":
        item = p.get("item", "item")
        slot = p.get("slot", "")
        return _embed("⚔️ Unequipped", f"Removed **{item}** from `{slot}`.", color=0xFFAA00)

    if rtype in ("trade", "market"):
        msg = p.get("message") or str(p)
        return _embed("✅ Action", msg)

    # Fallback — show message if present, otherwise raw JSON in a code block
    msg = p.get("message")
    if msg:
        return _embed("📜 RPG", msg)
    return _embed("📜 RPG", f"```json\n{str(p)[:1800]}\n```")


class RPG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /link ────────────────────────────────────────────────────────────────
    @app_commands.command(name="link", description="Link your Discord account to your Torvex account.")
    @app_commands.describe(torvex_username="Your Torvex username (case-sensitive)")
    async def link(self, interaction: discord.Interaction, torvex_username: str):
        await interaction.response.defer(ephemeral=True)
        status, data = await _api("POST", "/api/bot/link", json={
            "discordUserId": str(interaction.user.id),
            "torvexUsername": torvex_username
        })
        if status == 200:
            display = data.get("displayName") or data.get("username")
            await interaction.followup.send(
                f"✅ Linked to Torvex account **{display}**! You can now use `/rpg` commands.",
                ephemeral=True
            )
        elif status == 404:
            await interaction.followup.send(
                f"❌ No Torvex account found with username **{torvex_username}**. "
                "Make sure it matches exactly at torvex.app.",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Something went wrong. Try again later.", ephemeral=True)

    @app_commands.command(name="unlink", description="Unlink your Discord account from Torvex.")
    async def unlink(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        status, _ = await _api("DELETE", f"/api/bot/link/{interaction.user.id}")
        if status == 200:
            await interaction.followup.send("✅ Your Discord account has been unlinked from Torvex.", ephemeral=True)
        else:
            await interaction.followup.send("You don't have a linked account.", ephemeral=True)

    # ── /rpg group ───────────────────────────────────────────────────────────
    rpg = app_commands.Group(name="rpg", description="Torvex RPG commands")

    async def _ensure_linked(self, user: discord.User | discord.Member) -> bool:
        """Auto-create and link a Torvex account if one doesn't exist. Returns True on success."""
        status, _ = await _api("POST", "/api/bot/auto-link", json={
            "discordUserId": str(user.id),
            "discordUsername": user.display_name
        })
        return status == 200

    async def _game_command(self, interaction: discord.Interaction, command: str):
        await interaction.response.defer()

        if not await self._ensure_linked(interaction.user):
            await interaction.followup.send("❌ Could not connect to Torvex. Try again later.", ephemeral=True)
            return

        status, data = await _api("POST", "/api/bot/game/command", json={
            "discordUserId": str(interaction.user.id),
            "command": command
        })
        if status == 404:
            await interaction.followup.send("❌ Could not connect to Torvex. Try again later.", ephemeral=True)
            return
        if status != 200:
            await interaction.followup.send("❌ Something went wrong. Try again later.", ephemeral=True)
            return

        result = data  # GameCommandResult
        responses = result.get("responses") or []
        for resp in responses:
            embed = _game_embed(resp)
            if embed:
                await interaction.followup.send(embed=embed)

    @rpg.command(name="stats", description="View your character stats.")
    async def stats(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/stats")

    @rpg.command(name="fight", description="Start a fight with a monster, or challenge a player to PvP.")
    @app_commands.describe(
        monster="Monster name — start typing to get suggestions (leave blank for random)",
        opponent="Challenge a player to PvP instead of fighting a monster",
    )
    async def fight(self, interaction: discord.Interaction, monster: str = "", opponent: discord.Member = None):
        if opponent is not None:
            if opponent == interaction.user:
                await interaction.response.send_message("You can't challenge yourself.", ephemeral=True)
                return
            if opponent.bot:
                await interaction.response.send_message("You can't challenge a bot.", ephemeral=True)
                return
            view = ChallengeView(interaction.user, opponent, self.bot)
            embed = discord.Embed(
                title="⚔️ PvP Challenge!",
                description=(
                    f"{interaction.user.mention} challenges {opponent.mention} to a battle!\n\n"
                    f"**XP on the line** — winner earns XP based on opponent's level.\n"
                    f"{opponent.mention}, do you accept?"
                ),
                color=0xFF6600,
            )
            await interaction.response.send_message(embed=embed, view=view)
            return

        cmd = "/fight" if not monster else f"/fight {monster}"
        await self._game_command(interaction, cmd)

    @fight.autocomplete("monster")
    async def fight_monster_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", "/api/bot/monsters")
        if status != 200 or not isinstance(data, list):
            return []
        matches = [m for m in data if current.lower() in m["name"].lower()][:25]
        return [
            app_commands.Choice(
                name=f"{m['icon']} {m['name']} Lv.{m['level']} · {m['zone']}",
                value=m["name"],
            )
            for m in matches
        ]

    @rpg.command(name="attack", description="Attack during combat.")
    async def attack(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/attack")

    @rpg.command(name="defend", description="Defend during combat (halves incoming damage).")
    async def defend(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/defend")

    @rpg.command(name="magic", description="Cast a spell during combat.")
    @app_commands.describe(spell="Spell name")
    async def magic(self, interaction: discord.Interaction, spell: str = ""):
        cmd = "/magic" if not spell else f"/magic {spell}"
        await self._game_command(interaction, cmd)

    @rpg.command(name="flee", description="Attempt to flee from combat.")
    async def flee(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/flee")

    @rpg.command(name="inventory", description="View your inventory.")
    async def inventory(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/inventory")

    @rpg.command(name="equip", description="Equip an item.")
    @app_commands.describe(item="Item name")
    async def equip(self, interaction: discord.Interaction, item: str):
        await self._game_command(interaction, f"/equip {item}")

    @rpg.command(name="unequip", description="Unequip an item slot.")
    @app_commands.describe(slot="Slot name (e.g. MainHand, Head, Chest)")
    async def unequip(self, interaction: discord.Interaction, slot: str):
        await self._game_command(interaction, f"/unequip {slot}")

    @rpg.command(name="leaderboard", description="View the top players.")
    async def leaderboard(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/leaderboard")

    async def _gather_command(self, interaction: discord.Interaction, command: str):
        """Run a gather command and award bonus coins based on skill level."""
        await interaction.response.defer()

        if not await self._ensure_linked(interaction.user):
            await interaction.followup.send("❌ Could not connect to Torvex. Try again later.", ephemeral=True)
            return

        status, data = await _api("POST", "/api/bot/game/command", json={
            "discordUserId": str(interaction.user.id),
            "command": command
        })
        if status == 404:
            await interaction.followup.send("❌ Could not connect to Torvex. Try again later.", ephemeral=True)
            return
        if status != 200:
            await interaction.followup.send("❌ Something went wrong. Try again later.", ephemeral=True)
            return

        responses = data.get("responses") or []
        skill_level = 1
        gather_success = False
        for resp in responses:
            embed = _game_embed(resp)
            if embed:
                await interaction.followup.send(embed=embed)
            if resp.get("type") == "gather":
                gather_success = True
                skill_level = resp.get("payload", {}).get("skillLevel", 1)

        if gather_success:
            coins = 1 + (skill_level // 5)
            await _add_coins(str(interaction.user.id), coins, f"gathering:{command.lstrip('/')}")

    @rpg.command(name="mine", description="Mine for ore. Higher Mining level unlocks better ores. 30s cooldown.")
    async def mine(self, interaction: discord.Interaction):
        await self._gather_command(interaction, "/mine")

    @rpg.command(name="fish", description="Go fishing. Higher Fishing level unlocks better fish. 30s cooldown.")
    async def fish(self, interaction: discord.Interaction):
        await self._gather_command(interaction, "/fish")

    @rpg.command(name="chop", description="Chop wood. Higher Woodcutting level unlocks better logs. 30s cooldown.")
    async def chop(self, interaction: discord.Interaction):
        await self._gather_command(interaction, "/chop")

    @rpg.command(name="recipes", description="View available crafting recipes.")
    async def recipes(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/recipes")

    @rpg.command(name="craft", description="Craft an item.")
    @app_commands.describe(recipe="Recipe name")
    async def craft(self, interaction: discord.Interaction, recipe: str):
        await self._game_command(interaction, f"/craft {recipe}")

    @rpg.command(name="help", description="How to play the Torvex RPG.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📖 Torvex RPG — How to Play",
            description="Your account is created automatically when you use any `/rpg` command. No setup needed.",
            color=0x5B8CDB
        )

        embed.add_field(name="⚔️ Combat — PvE", value=(
            "**`/rpg fight [monster]`** — Start a fight (random monster if none specified)\n"
            "**`/rpg attack`** — Deal damage to the enemy\n"
            "**`/rpg defend`** — Halve incoming damage this turn\n"
            "**`/rpg magic [spell]`** — Cast a spell (costs MP)\n"
            "**`/rpg flee`** — Attempt to escape combat\n"
            "Winning gives **XP + Coins**. Losing costs 10% of your coins — get back up and fight!"
        ), inline=False)

        embed.add_field(name="🎒 Inventory & Gear", value=(
            "**`/rpg inventory`** — View your items\n"
            "**`/rpg equip <item>`** — Equip an item (must meet level/class req)\n"
            "**`/rpg unequip <slot>`** — Remove gear from a slot\n"
            "Slots: `MainHand` `OffHand` `Head` `Chest` `Legs` `Feet` `Ring` `Amulet`"
        ), inline=False)

        embed.add_field(name="⛏️ Gathering", value=(
            "**`/rpg mine`** — Mine for ore (higher Mining level = better ores)\n"
            "**`/rpg fish`** — Go fishing (higher Fishing level = better fish)\n"
            "**`/rpg chop`** — Chop wood (higher Woodcutting level = better logs)\n"
            "All gathering has a **30 second cooldown**."
        ), inline=False)

        embed.add_field(name="🔨 Crafting", value=(
            "**`/rpg recipes`** — See what you can craft\n"
            "**`/rpg craft <recipe>`** — Craft an item (requires materials + skill level)"
        ), inline=False)

        embed.add_field(name="📊 Progress", value=(
            "**`/rpg stats`** — View your level, HP/MP, and all stats\n"
            "**`/rpg leaderboard`** — Top 10 players by level and kills\n"
            "Classes: **Warrior · Mage · Ranger · Cleric · Rogue**\n"
            "Stats: STR · DEF · INT · DEX · VIT · LUK"
        ), inline=False)

        embed.add_field(name="💰 Peepo Bucks", value=(
            "Earn **1 Peepo Buck per message** (cap: 200/day)\n"
            "**`/balance`** — Check your bucks, level & XP\n"
            "**`/store`** — Browse rewards (Nitro, Robux & more)\n"
            "**`/redeem <item>`** — Claim a reward"
        ), inline=False)

        embed.set_footer(text="Torvex RPG • torvex.app")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(RPG(bot))
