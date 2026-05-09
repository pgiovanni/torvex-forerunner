import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import logging
from cogs.pvp import ChallengeView

log = logging.getLogger("rpg")

# ── Spell list — (name, min_level, mp_cost, emoji) ───────────────────────────
# Tier 1=7mp lv1 | Tier 2=25mp lv10 | Tier 3=55mp lv25 | Tier 4=90mp lv50
# Utility (out-of-combat only): heal/barrier/revitalize/regen/ward/cleanse/resurrection
SPELLS = [
    # ── Fire ────────────────────────────────
    ("fire",       1,   7,  "🔥"), ("fira",       10, 25, "🔥"),
    ("firaga",     25, 55,  "🔥"), ("firaja",     50, 90, "🔥"),
    # ── Ice ─────────────────────────────────
    ("blizzard",   1,   7,  "🧊"), ("blizzara",   10, 25, "🧊"),
    ("blizzaga",   25, 55,  "🧊"), ("blizzaja",   50, 90, "🧊"),
    # ── Lightning ───────────────────────────
    ("thunder",    1,   7,  "⚡"), ("thundera",   10, 25, "⚡"),
    ("thunderga",  25, 55,  "⚡"), ("thunderja",  50, 90, "⚡"),
    # ── Earth ───────────────────────────────
    ("quake",      1,   7,  "🌍"), ("quakera",    10, 25, "🌍"),
    ("quakega",    25, 55,  "🌍"), ("quakeja",    50, 90, "🌍"),
    # ── Water ───────────────────────────────
    ("water",      1,   7,  "💧"), ("watera",     10, 25, "💧"),
    ("waterga",    25, 55,  "💧"), ("waterja",    50, 90, "💧"),
    # ── Wind ────────────────────────────────
    ("aero",       1,   7,  "🌪️"), ("aerora",     10, 25, "🌪️"),
    ("aeroga",     25, 55,  "🌪️"), ("aeroja",     50, 90, "🌪️"),
    # ── Dark ────────────────────────────────
    ("dark",       1,   7,  "🌑"), ("darkra",     10, 25, "🌑"),
    ("darkga",     25, 55,  "🌑"), ("darkja",     50, 90, "🌑"),
    # ── Holy ────────────────────────────────
    ("holy",       1,   7,  "✨"), ("holra",      10, 25, "✨"),
    ("holga",      25, 55,  "✨"), ("holja",      50, 90, "✨"),
    # ── Light ───────────────────────────────
    ("flash",      1,   7,  "🌟"), ("flashra",    10, 25, "🌟"),
    ("flashga",    25, 55,  "🌟"), ("flashja",    50, 90, "🌟"),
    # ── Shadow ──────────────────────────────
    ("shadow",     1,   7,  "👤"), ("shadowra",   10, 25, "👤"),
    ("shadowga",   25, 55,  "👤"), ("shadowja",   50, 90, "👤"),
    # ── Poison ──────────────────────────────
    ("bio",        1,   7,  "☠️"), ("biora",      10, 25, "☠️"),
    ("bioga",      25, 55,  "☠️"), ("bioja",      50, 90, "☠️"),
    # ── Void ────────────────────────────────
    ("void",       1,   7,  "🌀"), ("voidra",     10, 25, "🌀"),
    ("voidga",     25, 55,  "🌀"), ("voidja",     50, 90, "🌀"),
    # ── Healing (in-combat heals player) ────
    ("cure",       1,   8,  "💚"), ("cura",       12, 22, "💚"),
    ("curaga",     28, 50,  "💚"), ("curaja",     55, 85, "💚"),
    # ── Utility (out-of-combat only) ────────
    ("heal",       1,  15,  "💚"), ("barrier",     5, 20, "🛡️"),
    ("revitalize", 15, 30,  "💚"), ("regen",      20, 35, "💚"),
    ("ward",       25, 25,  "🛡️"), ("cleanse",    30, 20, "✨"),
    ("resurrection", 50, 100, "💚"),
]

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
        char_hp  = p.get("hp", 0)
        max_hp   = p.get("maxHp", 1)
        char_mp  = p.get("mp", 0)
        max_mp   = p.get("maxMp", 1)
        def _stat(key):
            base  = p.get(key, 0) or 0
            bonus = p.get("bonus" + key[0].upper() + key[1:], 0) or 0
            return f"**{base + bonus}**" if bonus == 0 else f"**{base + bonus}** *(+{bonus})*"
        desc = (
            f"**{p.get('name')}** — Lv.**{p.get('level')}** {p.get('className','')}\n"
            f"XP: {p.get('xp', 0):,}/{p.get('xpToNext', 0):,}\n\n"
            f"❤️ HP `{_hp_bar(char_hp, max_hp)}` {char_hp}/{max_hp}\n"
            f"💧 MP `{_hp_bar(char_mp, max_mp)}` {char_mp}/{max_mp}\n\n"
            f"⚔️ STR {_stat('str')}  🛡️ DEF {_stat('def')}  🔮 INT {_stat('int')}\n"
            f"💨 DEX {_stat('dex')}  💚 VIT {_stat('vit')}  🍀 LUK {_stat('luk')}\n\n"
            f"🗡️ Kills: **{p.get('kills', 0)}**  💀 Deaths: **{p.get('deaths', 0)}**\n"
            f"🪙 Coins: **{p.get('coinBalance', 0):,}**"
        )

        # ── Gear slots ──────────────────────────────────────────────────────
        SLOT_ICONS  = {"MainHand": "⚔️", "OffHand": "🛡️", "Head": "⛑️",
                       "Chest": "🥼", "Legs": "👖", "Feet": "👟",
                       "Ring": "💍", "Amulet": "📿"}
        SLOT_ORDER  = ["MainHand", "OffHand", "Head", "Chest", "Legs",  "Feet", "Ring", "Amulet"]
        STAT_LABELS = [("bonusStr","STR"),("bonusDef","DEF"),("bonusInt","INT"),
                       ("bonusDex","DEX"),("bonusVit","VIT"),("bonusLuk","LUK")]
        gear_map    = {g["slot"]: g for g in (p.get("gear") or [])}
        gear_lines  = []
        for slot in SLOT_ORDER:
            sicon = SLOT_ICONS.get(slot, "📦")
            if slot in gear_map:
                g = gear_map[slot]
                bonuses = [f"+{g[k]} {lbl}" for k, lbl in STAT_LABELS if g.get(k, 0) > 0]
                bonus_str = f" *({', '.join(bonuses)})*" if bonuses else ""
                gear_lines.append(f"{sicon} {g['icon']} **{g['name']}**{bonus_str}")
            else:
                gear_lines.append(f"{sicon} *empty*")
        desc += "\n\n**Gear**\n" + "\n".join(gear_lines)

        # ── Active status effects ────────────────────────────────────────────
        status_fx = p.get("statusEffects") or []
        if status_fx:
            fx_parts = [f"{e['icon']} **{e['type']}**({e['turnsLeft']}t)" for e in status_fx]
            desc += "\n\n⚠️ **Active Effects:** " + "  ".join(fx_parts)

        # ── Skills ──────────────────────────────────────────────────────────
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

        low_hp = p_max > 0 and (p_hp / p_max) < 0.30
        low_hp_warn = "\n⚠️ **HP is low!** Use `/rpg item` or `/rpg shop Potions` before your next fight." if low_hp else ""

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
                f"❤️ {p_hp}/{p_max} HP  💧 {p_mp}/{p_mp_max} MP\n"
                f"✨ +**{xp:,} XP**  🪙 +**{coins:,} Coins**"
                f"{loot_str}{lvl_str}{low_hp_warn}"
            )
            return _embed("⚔️ Combat — Victory!", desc, color=0x00FF88)

        if state == "Defeat":
            lost    = result.get("coinsLost", 0)
            xp_lost = result.get("xpLost", 0)
            penalty = []
            if lost:    penalty.append(f"**{lost:,} Coins**")
            if xp_lost: penalty.append(f"**{xp_lost:,} XP**")
            penalty_str = "  Lost " + " and ".join(penalty) + "." if penalty else ""
            desc = (
                f"{log_text}\n\n"
                f"💀 **Defeated!**{penalty_str}\n"
                f"*Respawned at 10% HP — buy potions with `/rpg shop Potions` before jumping back in!*"
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
            return _embed("🎒 Inventory", "Your inventory is empty.")

        SLOT_ICONS = {"MainHand":"⚔️","OffHand":"🛡️","Head":"⛑️","Chest":"🥼",
                      "Legs":"👖","Feet":"👟","Ring":"💍","Amulet":"📿"}
        STAT_KEYS  = [("bonusStr","STR"),("bonusDef","DEF"),("bonusInt","INT"),
                      ("bonusDex","DEX"),("bonusVit","VIT"),("bonusLuk","LUK")]

        equipped_items = [i for i in items if i.get("equipped")]
        other_items    = [i for i in items if not i.get("equipped")]

        def _item_line(item, show_slot=False):
            icon  = item.get("icon", "📦")
            name  = item["name"]
            qty   = item.get("quantity", 1)
            rar   = item.get("rarity", "")
            sell  = item.get("sellValue", 0)
            # stat bonuses
            stats = [f"+{item[k]} {lbl}" for k, lbl in STAT_KEYS if item.get(k, 0) > 0]
            # damage for weapons
            if item.get("minDmg", 0) > 0:
                stats.insert(0, f"{item['minDmg']}-{item['maxDmg']} dmg")
            # heal amount for food/potions
            if item.get("healAmount", 0) > 0:
                is_potion = item.get("subType") in ("HealthPotion","ManaPotion")
                stats.insert(0, f"+{item['healAmount']}{'% HP' if is_potion else ' HP'}")
            enchants = item.get("enchants") or []
            ench_str = "  ".join(f"{e['icon']}{e['name']}" for e in enchants) if enchants else ""
            stat_str = f" *({', '.join(stats)})*" if stats else ""
            ench_part = f"  {ench_str}" if ench_str else ""
            qty_str   = f" x{qty}" if qty > 1 else ""
            sell_str  = f"  🪙{sell:,}" if sell > 0 else ""
            slot_icon = SLOT_ICONS.get(item.get("slot",""), "") if show_slot else ""
            slot_str  = f"{slot_icon} " if slot_icon else ""
            return f"{slot_str}{icon} **{name}**{qty_str} `{rar}`{stat_str}{ench_part}{sell_str}"

        lines = []
        if equipped_items:
            lines.append("**— Equipped —**")
            lines += [_item_line(i, show_slot=True) for i in equipped_items]
        if other_items:
            if equipped_items:
                lines.append("")
            lines.append("**— Inventory —**")
            lines += [_item_line(i) for i in other_items]

        desc = "\n".join(lines)
        return _embed("🎒 Inventory", desc)

    if rtype == "leaderboard":
        medals = ["🥇", "🥈", "🥉"]
        by_level = p.get("byLevel") or []
        by_kills = p.get("byKills") or []
        level_lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{e['name']}** — Lv.{e['level']} ({e['xp']:,} XP)"
            for i, e in enumerate(by_level)
        ]
        kill_lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{e['name']}** — {e['kills']:,} kills"
            for i, e in enumerate(by_kills)
        ]
        embed = discord.Embed(title="🏆 Leaderboard", color=0xF4C430)
        embed.add_field(name="⭐ Top Level", value="\n".join(level_lines) or "No data yet.", inline=False)
        embed.add_field(name="⚔️ Top Kills", value="\n".join(kill_lines) or "No data yet.", inline=False)
        return embed

    if rtype == "gather":
        action_icons = {"mine": "⛏️", "fish": "🎣", "chop": "🪓"}
        icon       = action_icons.get(p.get("action", ""), "⚒️")
        item       = p.get("item", "something")
        qty        = p.get("quantity", 1)
        xp         = p.get("xpGained", 0)
        slvl       = p.get("skillLevel", 1)
        sxp        = p.get("skillXp", 0)
        snext      = p.get("skillXpToNext", 100)
        bonus_gem  = p.get("bonusGem")
        tool_name  = p.get("toolName")
        tool_bonus = p.get("toolBonus", 0)
        gem_line   = f"\n💎 Bonus drop: **{bonus_gem}**!" if bonus_gem else ""
        tool_line  = f"\n🔧 **{tool_name}** (+{tool_bonus} qty)" if tool_name and tool_bonus else ""
        desc = (
            f"{icon} You gathered **{item}** x**{qty}**!{gem_line}{tool_line}\n\n"
            f"✨ +**{xp} skill XP**\n"
            f"📊 Skill Level **{slvl}** — `{_hp_bar(sxp, snext)}` {sxp}/{snext} XP"
        )
        return _embed(f"{icon} Gathering", desc, color=0x66BB6A)

    if rtype == "cook":
        raw_fish  = p.get("rawFish", "fish")
        result    = p.get("result", "food")
        burnt     = p.get("burnt", False)
        xp        = p.get("xpGained", 0)
        coin      = p.get("coinBonus", 0)
        slvl      = p.get("skillLevel", 1)
        sxp       = p.get("skillXp", 0)
        snext     = p.get("skillXpToNext", 100)
        burn_pct  = p.get("burnChance", 0)
        if burnt:
            desc = (
                f"🖤 You burnt the **{raw_fish}** — received **Burnt Fish**.\n\n"
                f"✨ +**{xp} XP**  🪙 +**{coin}** coin\n"
                f"📊 Cooking Lv.**{slvl}** — `{_hp_bar(sxp, snext)}` {sxp}/{snext} XP\n"
                f"*Burn chance: {burn_pct}% — level up Cooking to reduce it!*"
            )
            return _embed("🍳 Cooking — Burnt!", desc, color=0xFF9800)
        else:
            desc = (
                f"🍳 Cooked **{raw_fish}** → **{result}**!\n\n"
                f"✨ +**{xp} XP**  🪙 +**{coin}** coin(s)\n"
                f"📊 Cooking Lv.**{slvl}** — `{_hp_bar(sxp, snext)}` {sxp}/{snext} XP"
            )
            return _embed("🍳 Cooking — Success!", desc, color=0x66BB6A)

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

    if rtype == "boss_list":
        bosses = p.get("bosses", "No bosses found.")
        embed = discord.Embed(title="💀 Boss Encounters", description=bosses, color=0xFF4444)
        embed.set_footer(text="Use /rpg boss <name> to challenge a boss")
        return embed

    if rtype == "shop":
        coins    = p.get("coins", 0)
        items    = p.get("items", [])
        category = p.get("category", "All")
        if not items:
            return _embed("🛒 Shop", "Nothing in this category.", color=0xFFAA00)
        lines = []
        for i in items:
            parts = [f"{i['icon']} **{i['name']}**"]
            if i.get("levelReq", 0) > 1:
                parts.append(f"*(Lv{i['levelReq']}+)*")
            parts.append(f"— 🪙 {i['buyPrice']:,}")
            if i.get("effect"):
                parts.append(f"*{i['effect']}*")
            if i.get("bonuses"):
                parts.append(f"`{i['bonuses']}`")
            lines.append(" ".join(parts))
        # Split into pages of 15 to avoid embed limit
        page = "\n".join(lines[:20])
        if len(lines) > 20:
            page += f"\n*...and {len(lines)-20} more. Use a category filter to narrow down.*"
        embed = discord.Embed(title=f"🛒 Shop — {category.title()}", description=page, color=0x5865F2)
        embed.set_footer(text=f"Your coins: 🪙 {coins:,}  •  /rpg buy <item> [qty]")
        return embed

    if rtype == "buy":
        name  = p.get("item", "item")
        icon  = p.get("icon", "🛒")
        qty   = p.get("qty", 1)
        total = p.get("total", 0)
        bal   = p.get("newCoinBalance", 0)
        qty_str = f"x{qty} " if qty > 1 else ""
        return _embed("🛒 Purchased", f"{icon} {qty_str}**{name}** — spent 🪙 {total:,}\nBalance: 🪙 {bal:,}", color=0x66BB6A)

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

    if rtype == "cast_spell":
        msg        = p.get("message", "Spell cast.")
        hp         = p.get("hp", 0)
        max_hp     = p.get("maxHp", 1)
        caster_mp  = p.get("casterMp", 0)
        caster_max = p.get("casterMaxMp", 1)
        is_self    = p.get("isSelf", True)
        target     = p.get("targetName", "you")
        desc = f"{msg}\n\n"
        if is_self:
            desc += (
                f"❤️ HP `{_hp_bar(hp, max_hp)}` {hp}/{max_hp}\n"
                f"💧 MP `{_hp_bar(caster_mp, caster_max)}` {caster_mp}/{caster_max}"
            )
        else:
            desc += (
                f"❤️ **{target}** HP `{_hp_bar(hp, max_hp)}` {hp}/{max_hp}\n"
                f"💧 Your MP `{_hp_bar(caster_mp, caster_max)}` {caster_mp}/{caster_max}"
            )
        return _embed("✨ Spell Cast", desc, color=0x9B59B6)

    if rtype == "use_item":
        msg   = p.get("message", "Item used.")
        hp    = p.get("hp", 0)
        max_hp = p.get("maxHp", 1)
        mp    = p.get("mp", 0)
        max_mp = p.get("maxMp", 1)
        desc  = (
            f"{msg}\n\n"
            f"❤️ HP `{_hp_bar(hp, max_hp)}` {hp}/{max_hp}\n"
            f"💧 MP `{_hp_bar(mp, max_mp)}` {mp}/{max_mp}"
        )
        return _embed("🍖 Item Used", desc, color=0x66BB6A)

    if rtype == "market_search":
        items = p.get("items") or []
        q     = p.get("query", "")
        title = f"🏪 Market — Search: {q}" if q else "🏪 Market — All Listings"
        if not items:
            return _embed(title, "No active listings found.", color=0xFFAA00)
        lines = [
            f"{i['icon']} **{i['name']}**  — 🪙 **{i['cheapest']:,}** each  ·  {i['totalQty']} avail  ·  {i['sellers']} seller(s)"
            for i in items
        ]
        desc = "\n".join(lines[:20])
        if len(items) > 20:
            desc += f"\n*…and {len(items)-20} more. Use `/market search <name>` to narrow down.*"
        return _embed(title, desc, color=0x5865F2)

    if rtype == "market_browse":
        item     = p.get("item", "?")
        listings = p.get("listings") or []
        if not listings:
            return _embed(f"🏪 Market — {item}", "No listings found.", color=0xFFAA00)
        lines = [
            f"`#{i+1}` **{l['seller']}**  — 🪙 **{l['pricePerUnit']:,}** × {l['quantity']}  = 🪙 **{l['totalPrice']:,}**"
            for i, l in enumerate(listings)
        ]
        return _embed(f"🏪 Market — {item}", "\n".join(lines[:15]), color=0x5865F2)

    if rtype == "market_listed":
        return _embed("🏪 Market", f"✅ Listed **{p['item']}** ×{p['quantity']} at 🪙 **{p['price']:,}** each.\n*5% tax applies on sale.*", color=0x00FF88)

    if rtype == "market_bought":
        return _embed("🏪 Market", f"✅ Bought **{p['item']}** ×{p['quantity']} for 🪙 **{p['cost']:,}** (incl. 🪙 {p['tax']:,} tax).", color=0x00FF88)

    if rtype == "market_listings":
        listings = p.get("listings") or []
        if not listings:
            return _embed("🏪 My Listings", "You have no active listings.", color=0xFFAA00)
        lines = [
            f"`{str(l['id'])[:8]}…`  **{l['item']}** ×{l['quantity']}  @ 🪙 **{l['pricePerUnit']:,}** each"
            for l in listings
        ]
        return _embed("🏪 My Listings", "\n".join(lines) + "\n\n*Use `/market cancel <id>` to remove a listing.*", color=0x5865F2)

    if rtype == "market_cancelled":
        return _embed("🏪 Market", f"✅ Listing cancelled. **{p['item']}** returned to your inventory.", color=0xFFAA00)

    if rtype == "sell":
        return _embed(
            "🪙 Sold",
            f"{p.get('icon','')} **{p['item']}** ×{p['qty']} → 🪙 **+{p['total']:,}** (🪙 {p['priceEach']:,} each)\n"
            f"Balance: 🪙 **{p['newCoinBalance']:,}**",
            color=0xFFD700
        )

    if rtype == "trade":
        msg = p.get("message") or str(p)
        return _embed("✅ Trade", msg)

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

    async def _game_command(self, interaction: discord.Interaction, command: str, target_discord_id: str | None = None):
        await interaction.response.defer()

        if not await self._ensure_linked(interaction.user):
            await interaction.followup.send("❌ Could not connect to Torvex. Try again later.", ephemeral=True)
            return

        body = {"discordUserId": str(interaction.user.id), "command": command}
        if target_discord_id:
            body["targetDiscordUserId"] = target_discord_id
        status, data = await _api("POST", "/api/bot/game/command", json=body)
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

    @rpg.command(name="stats", description="View your (or another player's) character stats.")
    @app_commands.describe(user="Another member to view (optional)")
    async def stats(self, interaction: discord.Interaction, user: discord.Member = None):
        if user is None or user.id == interaction.user.id:
            await self._game_command(interaction, "/stats")
            return

        await interaction.response.defer()
        status, data = await _api("GET", f"/api/bot/game/stats/{user.id}")
        if status == 404:
            msg = data.get("error", f"{user.display_name} hasn't started their adventure yet.")
            await interaction.followup.send(f"❌ {msg}", ephemeral=True)
            return
        if status != 200:
            await interaction.followup.send("❌ Something went wrong. Try again later.", ephemeral=True)
            return

        embed = _game_embed(data)
        if embed:
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
            await interaction.followup.send(embed=embed)

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
        import asyncio
        (monster_status, monster_data), (stat_status, stat_data) = await asyncio.gather(
            _api("GET", "/api/bot/monsters"),
            _api("GET", f"/api/bot/game/stats/{interaction.user.id}"),
        )
        if monster_status != 200 or not isinstance(monster_data, list):
            return []

        player_level = 1
        if stat_status == 200:
            player_level = (stat_data.get("payload") or {}).get("level", 1)

        def _diff(lvl):
            d = lvl - player_level
            if lvl > player_level * 200:  return "💀 BOSS"
            if d >=  9: return "🔴 Very Hard"
            if d >=  4: return "🟡 Hard"
            if d >= -3: return "⚔️ Normal"
            return "🟢 Easy"

        name_filter = current.lower()
        matches = [m for m in monster_data if name_filter in m["name"].lower()]
        results = sorted(matches, key=lambda m: abs(m["level"] - player_level))

        return [
            app_commands.Choice(
                name=f"{m['icon']} {m['name']}  Lv.{m['level']}  {_diff(m['level'])}  [{m['zone']}]",
                value=m["name"],
            )
            for m in results[:25]
        ]

    @rpg.command(name="boss", description="Challenge a boss encounter. Massive HP, massive rewards.")
    @app_commands.describe(name="Boss name (leave blank to see all bosses)")
    async def boss(self, interaction: discord.Interaction, name: str = ""):
        cmd = "/boss" if not name else f"/boss {name}"
        await self._game_command(interaction, cmd)

    @boss.autocomplete("name")
    async def boss_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", "/api/bot/monsters")
        if status != 200 or not isinstance(data, list):
            return []
        # Bosses have much higher HP than their level — threshold: hp > level * 200
        bosses = [m for m in data if m.get("maxHp", 0) > m.get("level", 1) * 200]
        if current:
            bosses = [m for m in bosses if current.lower() in m["name"].lower()]
        return [
            app_commands.Choice(
                name=f"{m['icon']} {m['name']} Lv.{m['level']} — ❤️ {m['maxHp']:,} HP",
                value=m["name"],
            )
            for m in bosses[:25]
        ]

    @rpg.command(name="attack", description="Attack during combat.")
    async def attack(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/attack")

    @rpg.command(name="defend", description="Defend during combat (halves incoming damage).")
    async def defend(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/defend")

    @rpg.command(name="magic", description="Cast a spell. Support spells can target another player.")
    @app_commands.describe(spell="Spell to cast (type to filter by name)", target="Player to heal/buff (optional, defaults to yourself)")
    async def magic(self, interaction: discord.Interaction, spell: str = "", target: discord.Member = None):
        cmd = "/magic" if not spell else f"/magic {spell}"
        target_id = str(target.id) if target and target.id != interaction.user.id else None
        await self._game_command(interaction, cmd, target_discord_id=target_id)

    @magic.autocomplete("spell")
    async def magic_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", f"/api/bot/game/stats/{interaction.user.id}")
        if status != 200:
            return []
        p = data.get("payload", {})
        level = p.get("level", 1)
        available = [s for s in SPELLS if level >= s[1]]
        if current:
            available = [s for s in available if current.lower() in s[0].lower()]
        return [
            app_commands.Choice(
                name=f"{s[3]} {s[0].title()}  —  MP: {s[2]}  (Lv {s[1]}+)" if s[1] > 1 else f"{s[3]} {s[0].title()}  —  MP: {s[2]}",
                value=s[0]
            )
            for s in available[:25]
        ]

    @rpg.command(name="item", description="Use a consumable item in combat (potions, food, etc.).")
    @app_commands.describe(item="Item to use (food restores HP, potions restore HP/MP)")
    async def item(self, interaction: discord.Interaction, item: str):
        await self._game_command(interaction, f"/item {item}")

    @item.autocomplete("item")
    async def item_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", f"/api/bot/game/inventory/{interaction.user.id}")
        if status != 200:
            return []
        consumables = [
            i for i in (data if isinstance(data, list) else [])
            if i.get("type") == "Consumable" and i.get("quantity", 0) > 0
        ]
        if current:
            consumables = [i for i in consumables if current.lower() in i["name"].lower()]
        return [
            app_commands.Choice(
                name=f"{i['name']} x{i['quantity']}  [{i['rarity']}]",
                value=i["name"]
            )
            for i in consumables[:25]
        ]

    _SHOP_SUBCATEGORIES = {
        "potions": ["Health", "Mana", "Elixirs"],
        "weapons": ["Swords", "Axes", "Bows", "Staves", "Daggers"],
        "armor":   ["Helmets", "Chest", "Legs", "Boots", "Shields", "Rings", "Amulets"],
    }

    @rpg.command(name="shop", description="Browse the item shop.")
    @app_commands.describe(category="Main category", subcategory="Subcategory (optional)")
    @app_commands.choices(category=[
        app_commands.Choice(name="All",     value="all"),
        app_commands.Choice(name="Potions", value="potions"),
        app_commands.Choice(name="Food",    value="food"),
        app_commands.Choice(name="Weapons", value="weapons"),
        app_commands.Choice(name="Armor",   value="armor"),
    ])
    async def shop(self, interaction: discord.Interaction, category: str = "all", subcategory: str = ""):
        if subcategory:
            cmd = f"/shop {category} {subcategory}"
        elif category and category != "all":
            cmd = f"/shop {category}"
        else:
            cmd = "/shop"
        await self._game_command(interaction, cmd)

    @shop.autocomplete("subcategory")
    async def shop_subcategory_autocomplete(self, interaction: discord.Interaction, current: str):
        cat     = (getattr(interaction.namespace, "category", "") or "").lower()
        options = self._SHOP_SUBCATEGORIES.get(cat, [])
        if not options:
            return []
        matches = [o for o in options if current.lower() in o.lower()] or options
        return [app_commands.Choice(name=o, value=o.lower()) for o in matches]

    @rpg.command(name="buy", description="Buy an item from the shop.")
    @app_commands.describe(item="Item name", quantity="How many to buy (default 1)")
    async def buy(self, interaction: discord.Interaction, item: str, quantity: int = 1):
        qty_str = f" {quantity}" if quantity > 1 else ""
        await self._game_command(interaction, f"/buy {item}{qty_str}")

    @buy.autocomplete("item")
    async def buy_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("POST", "/api/bot/game/command", json={
            "discordUserId": str(interaction.user.id),
            "command": "/shop"
        })
        if status != 200:
            return []
        items = []
        for resp in (data.get("responses") or []):
            if resp.get("type") == "shop":
                items = resp.get("payload", {}).get("items", [])
                break
        if current:
            items = [i for i in items if current.lower() in i["name"].lower()]
        return [
            app_commands.Choice(
                name=f"{i['icon']} {i['name']}  🪙 {i['buyPrice']:,}  {i.get('effect', '')}".strip(),
                value=i["name"]
            )
            for i in items[:25]
        ]

    @rpg.command(name="sell", description="Sell an item back to the shop for 45% of its value.")
    @app_commands.describe(item="Item to sell", quantity="How many to sell (default 1)")
    async def sell(self, interaction: discord.Interaction, item: str, quantity: int = 1):
        qty_str = f" {quantity}" if quantity > 1 else ""
        await self._game_command(interaction, f"/sell {item}{qty_str}")

    @sell.autocomplete("item")
    async def sell_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", f"/api/bot/game/inventory/{interaction.user.id}")
        if status != 200 or not isinstance(data, list):
            return []
        items = [i for i in data if not i.get("isEquipped") and (not current or current.lower() in i["name"].lower())]
        return [
            app_commands.Choice(name=f"{i['name']} (×{i['quantity']})", value=i["name"])
            for i in items[:25]
        ]

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

    @equip.autocomplete("item")
    async def equip_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", f"/api/bot/game/inventory/{interaction.user.id}")
        if status != 200:
            return []
        equipable = [
            i for i in (data if isinstance(data, list) else [])
            if i.get("type") in ("Weapon", "Armor") and not i.get("equipped")
        ]
        if current:
            equipable = [i for i in equipable if current.lower() in i["name"].lower()]
        return [
            app_commands.Choice(
                name=f"{i.get('icon', '')} {i['name']}  [{i['rarity']}]",
                value=i["name"]
            )
            for i in equipable[:25]
        ]

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

    # ── /rpg cook ─────────────────────────────────────────────────────────────

    _RAW_FISH = [
        "Raw Shrimp", "Raw Trout", "Raw Salmon", "Raw Tuna",
        "Raw Lobster", "Raw Swordfish", "Raw Shark", "Raw Abyssal Eel",
    ]

    @rpg.command(name="cook", description="Cook a raw fish. Higher Cooking level reduces burn chance.")
    @app_commands.describe(fish="Raw fish to cook — start typing to see options")
    async def cook(self, interaction: discord.Interaction, fish: str):
        await self._game_command(interaction, f"/cook {fish}")

    @cook.autocomplete("fish")
    async def cook_fish_autocomplete(self, interaction: discord.Interaction, current: str):
        """Suggest raw fish the player has in their inventory (filtered from known fish list)."""
        status, data = await _api("POST", "/api/bot/game/command", json={
            "discordUserId": str(interaction.user.id),
            "command": "/inventory"
        })
        inventory_names: set[str] = set()
        if status == 200:
            responses = data.get("responses") or []
            for resp in responses:
                if resp.get("type") == "inventory":
                    for item in (resp.get("payload", {}).get("items") or []):
                        inventory_names.add(item.get("name", ""))

        matches = [
            f for f in self._RAW_FISH
            if (not inventory_names or f in inventory_names) and current.lower() in f.lower()
        ][:25]
        return [app_commands.Choice(name=f, value=f) for f in matches]

    @rpg.command(name="recipes", description="View available crafting recipes.")
    async def recipes(self, interaction: discord.Interaction):
        await self._game_command(interaction, "/recipes")

    _recipe_cache: list = []

    @rpg.command(name="craft", description="Craft an item.")
    @app_commands.describe(item="Item to craft")
    async def craft(self, interaction: discord.Interaction, item: str):
        await self._game_command(interaction, f"/craft {item}")

    @craft.autocomplete("item")
    async def craft_autocomplete(self, interaction: discord.Interaction, current: str):
        if not self.__class__._recipe_cache:
            status, data = await _api("GET", "/api/bot/recipes")
            if status == 200 and isinstance(data, list):
                self.__class__._recipe_cache = data
        recipes = self.__class__._recipe_cache
        if current:
            recipes = [r for r in recipes if current.lower() in r["name"].lower()]
        return [
            app_commands.Choice(
                name=f"{r['name']}  [{r['skill']} Lv.{r['skillLevel']}]",
                value=r["name"]
            )
            for r in recipes[:25]
        ]

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

        embed.add_field(name="🍳 Cooking", value=(
            "**`/rpg cook <raw fish>`** — Cook a raw fish into food\n"
            "Cooked food restores HP **in combat** via `/rpg item <name>`.\n"
            "Higher Cooking level reduces burn chance (starts at 40%, -0.5%/level).\n"
            "Fish: Shrimp · Trout · Salmon · Tuna · Lobster · Swordfish · Shark · Abyssal Eel"
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


class Market(commands.Cog):
    """Grand Exchange — player-driven marketplace."""

    def __init__(self, bot):
        self.bot = bot

    async def _game(self, interaction: discord.Interaction, cmd: str):
        await interaction.response.defer()
        status, data = await _api("POST", "/api/bot/game/command", json={
            "discordUserId": str(interaction.user.id),
            "channelId":     str(interaction.channel_id),
            "command":       cmd,
        })
        if status != 200:
            await interaction.followup.send("❌ Could not reach the game server.", ephemeral=True)
            return
        for resp in (data.get("responses") or []):
            embed = _game_embed(resp)
            if embed:
                await interaction.followup.send(embed=embed)
                return
        await interaction.followup.send("❌ No response.", ephemeral=True)

    market = app_commands.Group(name="market", description="Grand Exchange — buy and sell items with other players.")

    # ── /market search ────────────────────────────────────────────────────────
    @market.command(name="search", description="Browse all items currently listed on the market.")
    @app_commands.describe(item="Filter by item name (optional)")
    async def market_search(self, interaction: discord.Interaction, item: str = ""):
        await self._game(interaction, f"/market search {item}".strip())

    @market_search.autocomplete("item")
    async def _search_ac(self, interaction: discord.Interaction, current: str):
        return await _market_item_ac(current)

    # ── /market browse ────────────────────────────────────────────────────────
    @market.command(name="browse", description="See all listings for a specific item, sorted by price.")
    @app_commands.describe(item="Item to look up")
    async def market_browse(self, interaction: discord.Interaction, item: str):
        await self._game(interaction, f"/market browse {item}")

    @market_browse.autocomplete("item")
    async def _browse_ac(self, interaction: discord.Interaction, current: str):
        return await _market_item_ac(current)

    # ── /market buy ───────────────────────────────────────────────────────────
    @market.command(name="buy", description="Buy the cheapest listing for an item.")
    @app_commands.describe(item="Item to buy")
    async def market_buy(self, interaction: discord.Interaction, item: str):
        await self._game(interaction, f"/market buy {item}")

    @market_buy.autocomplete("item")
    async def _buy_ac(self, interaction: discord.Interaction, current: str):
        return await _market_item_ac(current)

    # ── /market list ──────────────────────────────────────────────────────────
    @market.command(name="list", description="List one of your items for sale.")
    @app_commands.describe(item="Item to sell", price="Price per unit (coins)", quantity="How many to list (default 1)")
    async def market_list(self, interaction: discord.Interaction, item: str, price: int, quantity: int = 1):
        await self._game(interaction, f"/market list {item} {price} {quantity}")

    @market_list.autocomplete("item")
    async def _list_ac(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", f"/api/bot/game/inventory/{interaction.user.id}")
        if status != 200 or not isinstance(data, list):
            return []
        items = [i for i in data if not i.get("isEquipped") and current.lower() in i["name"].lower()]
        return [
            app_commands.Choice(name=f"{i['name']} (×{i['quantity']})", value=i["name"])
            for i in items[:25]
        ]

    # ── /market listings ──────────────────────────────────────────────────────
    @market.command(name="listings", description="View your active market listings.")
    async def market_listings(self, interaction: discord.Interaction):
        await self._game(interaction, "/market listings")

    # ── /market cancel ────────────────────────────────────────────────────────
    @market.command(name="cancel", description="Cancel one of your active listings and get the item back.")
    @app_commands.describe(listing_id="Your listing (pick from list)")
    async def market_cancel(self, interaction: discord.Interaction, listing_id: str):
        await self._game(interaction, f"/market cancel {listing_id}")

    @market_cancel.autocomplete("listing_id")
    async def _cancel_ac(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", f"/api/bot/market/search?discordUserId={interaction.user.id}")
        if status != 200 or not isinstance(data, list):
            return []
        return [
            app_commands.Choice(
                name=f"{i['name']} — 🪙 {i['cheapest']:,} (×{i['totalQty']})",
                value=str(i["listingId"])
            )
            for i in data[:25] if i.get("listingId")
        ]


async def _market_item_ac(current: str) -> list[app_commands.Choice]:
    q = f"?q={current}" if current else ""
    status, data = await _api("GET", f"/api/bot/market/search{q}")
    if status != 200 or not isinstance(data, list):
        return []
    return [
        app_commands.Choice(
            name=f"{i['icon']} {i['name']}  — 🪙 {i['cheapest']:,}  ({i['totalQty']} avail)",
            value=i["name"]
        )
        for i in data[:25]
    ]


async def setup(bot):
    await bot.add_cog(RPG(bot))
    await bot.add_cog(Market(bot))
