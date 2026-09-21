"""Last to survive — the ban race. Discord side of utils/ban_race.py.

    /race start|stop|edit|status   anyone can open a ghost race; real bans and
                              channel: need Manage Server; stop/edit = host or mod
    /race channel <#channel>  Manage Server: the server's race channel (a setting —
                              the bot never creates channels)
    /vote <player>            fire this round's shot — private until the round closes
    /powerup [use] [target]   inventory, or aim an Overload / a heal

Everything else is buttons on the lobby and round messages, plus power-up
drops that appear in the channel mid-round (first click takes it). Buttons
carry fixed custom_ids ("lts:<action>:<race>") and are answered by the
on_interaction listener, so a restart mid-race doesn't kill the panel; the
round loop itself is resumed from the DB on the next on_ready.

Eliminations in mode "real" are actual bans, executed by the bot — the only
way members at the same role level can knock each other out. Each victim is
DM'd their elimination card WITH the return invite BEFORE the ban lands
(after it, no shared server usually means no DM), and the bot unbans every
race ban the moment a winner is declared or the host stops the race. Race
bans are marked quiet so the goodbye channel and mod-log embeds don't get
thirty cards in a night; member_events still records every one.
"""
import asyncio
import logging
import os
import random
import secrets
import sys
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import ban_race as engine  # noqa: E402
from utils.quiet_removals import mark as quiet_mark  # noqa: E402
from utils.security_config import get_config, set_config  # noqa: E402

log = logging.getLogger("ban_race")

PREFIX = "lts"
COLOR = 0xE74C3C
COLOR_ROUND = 0xF1C40F
COLOR_DROP = 0x9B59B6
COLOR_SUPER = 0xFFD700
COLOR_RARE = 0xF1C40F      # rare drops: no-strings power-ups (Paul 9/12 tiers)
COLOR_LEGENDARY = 0xE67E22 # the golden apple
COLOR_WIN = 0x2ECC71
MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)
NO_MENTIONS = discord.AllowedMentions.none()
INVITE_DAYS = 7
RACER_ROLE = "Racer"     # only this role can talk in the race channel; Join grants it
MAX_PINGS = 60
START_DELAY = 30        # seconds between the start ping and round 1 opening
BUMP_AFTER = 6          # messages in the race channel (the bot's own included) before the panel is re-posted at the bottom
BUMP_COOLDOWN = 20      # seconds between two bumps of the same race
CARD_REPOST_COOLDOWN = 30   # seconds before a missing lobby card may be re-posted again
SCHEDULE_CHECK_S = 20   # how often the scheduler looks at the clock

MODE_CHOICES = [
    app_commands.Choice(name="ghost — no bans, eliminated players are just out (default)", value="ghost"),
    app_commands.Choice(name="real — actual bans, auto-unban when it ends", value="real"),
]
USE_CHOICES = [
    app_commands.Choice(name="Overload — take 1 damage to deal 2 (needs a player)", value="overload"),
    app_commands.Choice(name="Transfuse — heal someone else 1, lose 1 yourself (needs a player)", value="transfuse"),
    app_commands.Choice(name="Blood bag — heal someone else 1, costs you nothing (needs a player)", value="bloodbag"),
    app_commands.Choice(name="Paramedic — heal someone else 2 (needs a player)", value="paramedic"),
    app_commands.Choice(name="Field hospital — heal someone else 3 (needs a player)", value="fieldhosp"),
    app_commands.Choice(name="Patch — heal yourself 1", value="patch"),
    app_commands.Choice(name="Medkit — heal yourself 2, skip this round's vote", value="medkit"),
    app_commands.Choice(name="Small revive — bring someone back with 1 life (needs a player)", value="revive_small"),
    app_commands.Choice(name="Medium revive — bring someone back with 2 lives (needs a player)", value="revive_medium"),
    app_commands.Choice(name="Full revive — bring someone back at full lives (needs a player)", value="revive_full"),
    app_commands.Choice(name="Extra revive — bring someone back one life ABOVE max (needs a player)", value="revive_extra"),
]


def _fmt_round(secs):
    """'1.5 min', '3 min', '45 s' — whatever reads cleanly."""
    secs = int(secs or 0)
    if secs < 60:
        return f"{secs} s"
    return f"{secs / 60:g} min"

PITCH = ("You've got **{lives} lives**. There are **power-ups**. Pick who you're going for "
         "and shoot. See you after the round ends.")


# ── custom ids ────────────────────────────────────────────────────────────────

def _cid(action, race_id, extra=None):
    s = f"{PREFIX}:{action}:{race_id}"
    return f"{s}:{extra}" if extra else s


def _parse_cid(cid):
    if not cid or not cid.startswith(PREFIX + ":"):
        return None
    parts = cid.split(":")
    if len(parts) < 3:
        return None
    try:
        rid = int(parts[2])
    except ValueError:
        return None
    return parts[1], rid, (parts[3] if len(parts) > 3 else None)


# ── views ─────────────────────────────────────────────────────────────────────

def lobby_view(race_id, closed=False):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Join", emoji="🔫", style=discord.ButtonStyle.success,
                                 custom_id=_cid("join", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Leave", style=discord.ButtonStyle.secondary,
                                 custom_id=_cid("leave", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Start the race", emoji="🏁", style=discord.ButtonStyle.danger,
                                 custom_id=_cid("begin", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary,
                                 custom_id=_cid("cancel", race_id), disabled=closed))
    return v


def round_view(race_id, closed=False):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Vote", emoji="🔫", style=discord.ButtonStyle.danger,
                                 custom_id=_cid("vote", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Power-ups", emoji="🎒", style=discord.ButtonStyle.primary,
                                 custom_id=_cid("powerup", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Standings", emoji="📊", style=discord.ButtonStyle.secondary,
                                 custom_id=_cid("standings", race_id), disabled=closed))
    v.add_item(discord.ui.Button(label="Close round now", emoji="⏭️", style=discord.ButtonStyle.secondary,
                                 custom_id=_cid("close", race_id), disabled=closed))
    # Paul 9/16: "anyone can join at anytime" — the lobby card is long gone by
    # now, so the way in during a race is here.
    v.add_item(discord.ui.Button(label="Join", emoji="🚪", style=discord.ButtonStyle.success,
                                 custom_id=_cid("join", race_id), disabled=closed))
    return v


def drop_view(race_id, nonce):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="GRAB IT", emoji="⚡", style=discord.ButtonStyle.success,
                                 custom_id=_cid("drop", race_id, nonce)))
    return v


class _TargetSelect(discord.ui.View):
    """Ephemeral picker: who to aim at. One select, up to 25 living players."""

    def __init__(self, cog, race_id, kind, me, rows, label=None, seq=None):
        super().__init__(timeout=120)
        self.cog, self.race_id, self.kind, self.seq = cog, race_id, kind, seq
        if kind in engine.REVIVES:
            pool = [p for p in rows if not p["alive"] and not p.get("revived")]
            opts = [discord.SelectOption(label=p["name"][:100], value=p["user_id"],
                                         description=f"out since round {p.get('died_round') or '?'}")
                    for p in pool][:25]
        else:
            opts = [discord.SelectOption(label=p["name"][:100], value=p["user_id"],
                                         description=f"{p['lives']} lives · {p['kills']} kills")
                    for p in engine.alive(rows) if p["user_id"] != str(me)][:25]
        if not opts:
            opts = [discord.SelectOption(label="(nobody to pick)", value="0")]
        if not label and kind in engine.HEALS:
            _, heal_label, gives, _ = engine.HEALS[kind]
            label = f"{heal_label} — who gets +{gives} {'life' if gives == 1 else 'lives'}?"
        label = label or {"shot": "Who are you going for?",
                          "overload": "Overload — who takes 2?"}.get(kind) \
            or f"{engine.REVIVES[kind][1]} — who comes back?"
        sel = discord.ui.Select(placeholder=label, options=opts, min_values=1, max_values=1)
        sel.callback = self._pick
        self.add_item(sel)

    async def _pick(self, interaction):
        target = interaction.data["values"][0]
        if self.seq is not None:
            text = await self.cog._do_retarget(self.race_id, interaction.user.id, int(target), self.seq)
        else:
            text = await self.cog._do_cast(interaction.guild, self.race_id, interaction.user.id,
                                           int(target), self.kind)
        await interaction.response.edit_message(content=text, embed=None, view=None)


class _ShotView(_TargetSelect):
    """The Vote pop-up: the target picker for the next shot (when one is
    banked) plus a "Change shot N" button per shot already fired this round
    (Paul 9/12: "need a way to edit them tho (change shot)")."""

    def __init__(self, cog, race_id, me, rows, fired, label=None, with_select=True):
        super().__init__(cog, race_id, "shot", me, rows, label=label)
        self.rows, self.me = rows, me
        if not with_select:
            self.clear_items()
        for sh in fired[:5]:
            name = next((p["name"] for p in rows if p["user_id"] == sh["target_id"]), "?")
            emoji = "💥" if sh["kind"] == "overload" else "🎯"
            b = discord.ui.Button(label=f"Change shot {sh['seq']} (→ {name})"[:80], emoji=emoji,
                                  style=discord.ButtonStyle.secondary)
            b.callback = self._mk(sh["seq"], sh["kind"], name)
            self.add_item(b)

    def _mk(self, seq, kind, name):
        async def cb(interaction):
            v = _TargetSelect(self.cog, self.race_id, kind, self.me, self.rows,
                              label=f"Shot {seq} is on {name} — who instead?", seq=seq)
            await interaction.response.edit_message(
                content=f"🔁 **Change shot {seq}** — currently locked on **{name}**.", embed=None, view=v)
        return cb


class _PowerupView(discord.ui.View):
    """Ephemeral inventory with a button per aimable power-up."""

    def __init__(self, cog, race_id, p, rows):
        super().__init__(timeout=120)
        self.cog, self.race_id, self.rows = cog, race_id, rows
        if p["overload"] > 0:
            b = discord.ui.Button(label=f"Use Overload ×{p['overload']}", emoji="💥",
                                  style=discord.ButtonStyle.danger)
            b.callback = self._mk("overload")
            self.add_item(b)
        # the heal-an-ally ladder, in tier order — every one of them aims at
        # somebody else, so they all open the target picker
        for kind, (emoji, label, gives, _cost) in engine.HEALS.items():
            if p.get(kind, 0) > 0:
                b = discord.ui.Button(label=f"Use {label} ×{p[kind]} (heal an ally {gives})"[:80], emoji=emoji,
                                      style=discord.ButtonStyle.primary)
                b.callback = self._mk(kind)
                self.add_item(b)
        for kind, (emoji, label, _) in engine.REVIVES.items():
            if p.get(kind, 0) > 0:
                b = discord.ui.Button(label=f"Use {label} ×{p[kind]}", emoji=emoji,
                                      style=discord.ButtonStyle.success)
                b.callback = self._mk(kind)
                self.add_item(b)
        for kind in engine.SELF_USE:
            if p.get(kind, 0) > 0:
                emoji, name, _ = engine.POWERUPS[kind]
                b = discord.ui.Button(label=f"Use {name} ×{p[kind]}", emoji=emoji,
                                      style=discord.ButtonStyle.success)
                b.callback = self._mk_self(kind)
                self.add_item(b)

    def _mk_self(self, kind):
        async def cb(interaction):
            text = await self.cog._do_use_self(self.race_id, interaction.user.id, kind)
            await interaction.response.edit_message(content=text, embed=None, view=None)
        return cb

    def _mk(self, kind):
        async def cb(interaction):
            v = _TargetSelect(self.cog, self.race_id, kind, interaction.user.id, self.rows)
            await interaction.response.edit_message(content=engine.blurb(kind), embed=None, view=v)
        return cb


# ── embeds ────────────────────────────────────────────────────────────────────

def _lives_bar(n, max_lives=None):
    """Hearts; anything above max (golden apple) shows gold."""
    if not n:
        return "💀"
    if max_lives and n > max_lives:
        return "❤️" * max_lives + "💛" * (n - max_lives)
    return "❤️" * max(0, n)


FIELD_LIMIT = 1024  # Discord's hard cap on one embed field's value


def add_chunked(e, name, blocks, inline=False):
    """Add `blocks` to embed `e` under `name`, spilling into "(cont.)" fields so
    no value ever passes Discord's 1024-char limit. A block is a group of lines
    kept together when it fits (a drop tier, say); an oversized block splits
    line by line, and a single oversized line is trimmed.

    Why this exists: an over-long value is a 400 on the WHOLE message, and a
    button handler that 400s never answers its interaction — the player just
    sees "didn't respond in time". The 9/13 revive tiers pushed the kit's two
    guides past 1024 and killed the Power-ups button for races 8 and 9.
    """
    parts, cur = [], ""

    def flush():
        nonlocal cur
        if cur:
            parts.append(cur)
            cur = ""

    for block in blocks:
        text = "\n".join(block)
        if len(text) <= FIELD_LIMIT:
            if cur and len(cur) + 1 + len(text) <= FIELD_LIMIT:
                cur = f"{cur}\n{text}"
            else:
                flush()
                cur = text
            continue
        for line in block:
            if len(line) > FIELD_LIMIT:
                line = line[:FIELD_LIMIT - 2] + " …"
            if cur and len(cur) + 1 + len(line) > FIELD_LIMIT:
                flush()
            cur = f"{cur}\n{line}" if cur else line
    flush()
    for i, value in enumerate(parts or ["—"]):
        e.add_field(name=name if i == 0 else f"{name} (cont.)", value=value, inline=inline)


EMBED_TOTAL_LIMIT = 6000    # Discord's cap on title + description + every field
EMBED_BUDGET = EMBED_TOTAL_LIMIT - 250   # leave room for a footer and a fallback line


def embed_len(e):
    """What Discord counts against the 6000-character cap."""
    n = len(e.title or "") + len(e.description or "")
    n += len((e.footer.text or "") if e.footer else "")
    return n + sum(len(f.name or "") + len(f.value or "") for f in e.fields)


def add_chunked_if_room(e, name, blocks, *, budget=EMBED_BUDGET):
    """`add_chunked`, rolled back if it would push the embed past the cap.

    The field cap is not the only one that 400s a message, and a 400 inside a
    button handler reads to the player as "didn't respond in time" (the 9/13
    revive-tier bug). The guide grows every time a power-up is added, so the
    tail sections yield instead of taking the whole card down with them."""
    start = len(e.fields)
    add_chunked(e, name, blocks)
    if embed_len(e) <= budget:
        return True
    for i in range(len(e.fields) - 1, start - 1, -1):
        e.remove_field(i)
    return False


# What a rarity MEANS, said once at the top of the guide instead of on every
# line. The glyphs come from engine.rarity_mark, which reads the drop tables.
RARITY_LEGEND = (
    "⚪ **Common** — drops often, and it bites back  ·  🟢 **Uncommon** — the middle rung  ·  "
    "🟡 **Rare** — no strings  ·  🌟 **Super** — sudden death only, one a round "
    "(🌟🌟 = super rare, one super in ten).\n"
    "A super fires the moment you grab it — except revives and the field hospital, which go to "
    "your kit. Everything else stays with you until the race ends."
)


def powerup_blocks():
    """Every item GROUPED BY WHAT IT DOES — shooting, defense, healing
    yourself, healing an ally, revives — one short line each: what it does,
    then what it costs you.

    Paul 9/20: "grouped by type … each listed just as short as possible with
    effect and side effect". The copy lives in engine.BRIEF/ITEM_GROUPS, so
    a rule a whole family shares is written once in the group header. Supers
    sit in their group with a 🌟 rather than in a section of their own — a
    nuke is a shooting item first and a sudden-death item second.

    One block per group, so a group's header never splits from its items when
    add_chunked spills into "(cont.)" fields."""
    blocks = [[RARITY_LEGEND]]
    for _key, g_emoji, title, note, items in engine.grouped_items():
        block = [f"{g_emoji} **{title}**" + (f" · *{note}*" if note else "")]
        for _kind, emoji, label, rarity, effect, cost in items:
            # 🌟 Full revive's own emoji IS the super glyph — don't print it twice.
            face = emoji if rarity == emoji else f"{rarity} {emoji}"
            line = f"{face} **{label}** — {effect}."
            if cost:
                line += f" ❌ {cost}."
            block.append(line)
        blocks.append(block)
    return blocks


def lives_line(s, n_joined):
    """The lobby's lives line. Auto = the recommendation for whoever's in so
    far (locked in at start); fixed = the host's number, with the
    recommendation shown beside it when it differs — advice, never override."""
    need = s.get("min_players", engine.MIN_PLAYERS)
    rec_now = engine.recommended_lives(max(n_joined, engine.MIN_PLAYERS))
    rec_full = engine.recommended_lives(need)
    if s.get("lives_auto"):
        return (f"❤️ **Auto** — {rec_now} for the {n_joined} in so far, {rec_full} at {need}. "
                f"Locked in for whoever's actually here when it starts.")
    fixed = s["lives"]
    if fixed == rec_full:
        return f"❤️ **{fixed}** (the host's call — also the recommendation for {need} players)."
    return (f"❤️ **{fixed}** (the host's call). Recommended for {need} players: **{rec_full}**"
            + (f", for the {n_joined} in so far: **{rec_now}**" if rec_now != rec_full and n_joined else "") + ".")


def overkill_blocks():
    """How overkill pays, for the kit pop-up. Rules live in the engine, so the
    thresholds here can never drift from what actually resolves."""
    ladder = " · ".join(f"{emoji} **{label}** {need}+ \u2192 🩹 ×{min(need - 1, engine.OVERKILL_MAX_PATCHES)}"
                        for need, emoji, label in reversed(engine.OVERKILL_LABELS))
    return [[
        "💀 **Overkill** = damage past a victim's last life, inside one round. A shot at someone "
        "who's already down THIS round is no longer wasted — it piles on.",
        f"The credited killer collects for the whole pile: {ladder}.",
        "❌ Killing someone who was AFK still pays nothing, and a pile of 1 pays nothing.",
    ]]


def reference_embed(settings=None, *, header=None):
    """The power-up card: what every drop does, what it costs, how rare it is —
    plus how a shot, an Overload and a backfire resolve, because those are what
    the items ride on.

    It exists because the answer to "what does this power-up do?" used to be
    reachable only from a kit, and a kit only exists inside a running race —
    ask between races and the bot said "No race running right now" and stopped
    (Paul 9/20: "it just says 'no race running right now', does not offer an
    explanation of what's going on"). Settings default to DEFAULTS, so it
    answers with real numbers even when there's no race to read them from.

    It no longer teaches the RACE (Paul 9/20: "remove the how a race works in
    the power up description ... we'll add a wiki to the dashboard instead") —
    the round/storm/AFK rules stay on the lobby card, where a player meets them
    before joining.
    """
    s = dict(engine.DEFAULTS)
    s.update(settings or {})
    e = discord.Embed(title="🎒 Power-ups", color=COLOR_DROP,
                      description=header or PITCH.format(lives=s["lives"]))
    add_chunked(e, "Shots, Overload & backfire", shot_rules_blocks(s))
    if not add_chunked_if_room(e, "What the power-ups do", powerup_blocks()):
        e.add_field(name="What the power-ups do", inline=False,
                    value=" · ".join(f"{emoji} **{name}**"
                                     for emoji, name, _ in engine.POWERUPS.values())[:FIELD_LIMIT]
                          + "\nEvery drop says what it does when it lands — grab one and read it.")
    # Tail sections yield if the card is full — see add_chunked_if_room.
    if not add_chunked_if_room(e, "Overkill", overkill_blocks()):
        e.add_field(name="Overkill", inline=False,
                    value="💀 Damage past someone's last life inside one round pays the credited "
                          "killer in 🩹 Patches. Full ladder in 🎒 **Power-ups**.")
    return e


def fmt_slots(slots, tz):
    """The daily start times as Discord timestamps — '<t:...:t> · <t:...:t> …'.
    Discord renders each one on the READER's clock, so a player in London sees
    their own times instead of having to work out what "EDT" means
    (Paul 9/16: "use timestamps for this ... instead of EDT"). Each stamp is
    that slot's next occurrence, which is the same time of day every day."""
    try:
        stamps = engine.next_of_each(time.time(), slots, tz)
    except Exception:
        return ", ".join(slots) + f" ({tz})"
    return " · ".join(f"<t:{int(ts)}:t>" for ts in stamps)


def how_it_works(s):
    """The "How it works" bullets. Shared by the lobby card and the /powerup
    reference so a rule can never be right in one place and stale in the other."""
    return (f"• Rounds last **{_fmt_round(s['round_secs'])}**; shots are secret and all land at once.\n"
            f"• One shot per round — it doesn't stack. Power-ups you grab stay with you for the whole race.\n"
            f"• Zero lives = {'**actually banned**' if s['mode'] == 'real' else 'out'}. "
            f"{'Everyone is unbanned the moment it ends, and you get the invite by DM first.' if s['mode'] == 'real' else ''}\n"
            f"• Don't vote in a round and you lose a life. AFK is not a strategy.\n"
            f"• Only racers can talk here — **Join** unlocks the channel; ghosts watch in silence.\n"
            f"• Power-ups drop in this channel every round — the more of you still in, the more drops. First click takes it. "
            f"Sudden death adds **super drops**.\n"
            f"• **Sudden death** (shields off, half-length rounds) once **{s.get('sudden_death_at', engine.DEFAULTS['sudden_death_at'])}** are left"
            f"{' — sized to the head-count when it starts' if s.get('sudden_auto') else ''}.\n"
            f"• **Overkill**: damage past someone's last life still counts — pile on a target who's "
            f"already down and the killer takes the Patches.\n"
            f"• Last one standing wins. 🎁")


def shot_rules_blocks(s):
    """What actually happens to a shot — the questions players ask mid-race:
    when it lands, what an Overload needs, which way backfire resolves."""
    pct = int(round(float(s.get("backfire", engine.DEFAULTS["backfire"])) * 100))
    return [[
        "🔫 **One shot per round**, cast in private — **Vote** on the round card, or `/vote`. "
        "Shots don't compile: an unused one is gone when the round closes.",
        "💥 **An Overload rides on that shot.** Arm it from **🎒 Power-ups** (or "
        "`/powerup use:Overload player:@them`) INSTEAD of shooting — shoot first and there's "
        "nothing left for it to ride on. It is your shot for the round, doubled.",
        f"🔥 **Backfire ({pct}%)** — one roll per shot, at round close. It redirects YOUR shot "
        "onto you, so your target takes nothing; there's no version where it backfires *and* "
        "lands. A shield eats it (not in sudden death), and a backfired Overload costs you 3: "
        "the 1 it burns to fire, then its own 2.",
        "⚰️ Dying doesn't cancel what you cast — a dead shooter still fires.",
        "🩺 **Heals and revives are not your vote.** They're aimed at someone else and land at "
        "round close; you still owe a shot, or the AFK penalty takes a life anyway.",
    ]]


def lobby_embed(race, rows, guild_name, schedule=None):
    s = race["settings"]
    e = discord.Embed(title="🔫 LAST TO SURVIVE", color=COLOR,
                      description=PITCH.format(lives=s["lives"]))
    e.add_field(name="How it works", value=how_it_works(s), inline=False)
    e.add_field(name="Lives", value=lives_line(s, len(rows)), inline=False)
    # No power-up catalogue here (Paul 9/13: "way too much") — the item blurbs
    # live on the drops themselves and behind the Power-ups button.
    # The whole roster, never a "+12 more" — there's no cap on who plays
    # (Paul 9/16), so the list spills into (cont.) fields instead of truncating.
    names = [p["name"] for p in rows]
    add_chunked(e, f"Players ({len(rows)})",
                [[", ".join(names)]] if names else [["*nobody yet — hit Join*"]])
    if schedule:
        need = s.get("min_players", engine.MIN_PLAYERS)
        nxt = int(schedule["next"])
        e.add_field(name="🕗 Next race",
                    value=(f"**<t:{nxt}:F>** — <t:{nxt}:R>\n"
                           f"Races run **{fmt_slots(schedule['slots'], schedule['tz'])}**, every day. "
                           f"The clock starts it, not the head-count.\n"
                           f"• **Join whenever** — no cap, no cut-off, and the lobby never closes.\n"
                           f"• Turn up while a race is on and you drop straight into the next round, "
                           f"**one life lighter for every round already played**.\n"
                           f"• Fewer than **{need}** here when a slot comes and it waits for the next one "
                           f"— you keep your seat, nobody re-joins."),
                    inline=False)
        e.set_footer(text=f"{guild_name} · the lobby never closes — your seat carries to the next race")
    else:
        e.set_footer(text=f"{guild_name} · starts automatically at {s['min_players']} players")
    return e


def round_embed(race, rows, round_no, ends_at, sudden, extra_lines):
    n = len(engine.alive(rows))
    e = discord.Embed(title=f"⏱️ Round {round_no}", color=COLOR_ROUND,
                      description=f"**{n}** alive · closes <t:{int(ends_at)}:R>\n"
                                  f"Hit **Vote** (or `/vote`) to fire. Shots land when the round closes.")
    if sudden:
        e.add_field(name="☠️ SUDDEN DEATH", value="Shields are off. Rounds are half as long.", inline=False)
    for line in extra_lines:
        e.add_field(name="​", value=line, inline=False)
    return e


def standings_embed(race, rows):
    live, fallen = engine.standings(rows)
    e = discord.Embed(title=f"📊 Standings · round {race['round_no']}", color=COLOR)
    lines = []
    for p in live:
        tag = " 🎯" if p["bounty"] else ""
        lines.append(f"{_lives_bar(p['lives'], race['settings']['lives'])} **{p['name']}**{tag} · {p['kills']} kills")
    add_chunked(e, f"Alive ({len(live)})", [[ln] for ln in lines[:30]])
    if fallen:
        f = [f"💀 {p['name']} · round {p['died_round']}" for p in fallen[:30]]
        add_chunked(e, f"Fallen ({len(fallen)})", [[ln] for ln in f])
    e.set_footer(text="Shields are secret. Kills pay a shield (a Patch if you already hold one); "
                      "the bounty pays two shots. AFK kills pay nothing.")
    return e


def drop_embed(kind, is_super=False):
    if is_super:
        emoji, name, blurb = engine.SUPER[kind]
        if engine.SUPER_WEIGHTS.get(kind) == 1:
            return discord.Embed(title=f"{emoji} SUPER RARE DROP — {name}", description=blurb, color=COLOR_LEGENDARY)
        return discord.Embed(title=f"🌟 SUPER DROP — {emoji} {name}", description=blurb, color=COLOR_SUPER)
    emoji, name, blurb = engine.POWERUPS[kind]
    t_emoji, t_label = engine.tier_of(kind)
    return discord.Embed(title=f"⚡ POWER-UP DROP — {emoji} {name} · {t_emoji} {t_label}",
                         description=blurb, color=COLOR_RARE if t_label == "Rare" else COLOR_DROP)


def elimination_dm(race, round_no, killer_id, guild_name):
    s = race["settings"]
    e = discord.Embed(title="⛔ You're out", color=COLOR,
                      description=f"Eliminated in **round {round_no}** of Last to survive in **{guild_name}**"
                                  + (f", by <@{killer_id}>." if killer_id else "."))
    if s["mode"] == "real":
        e.add_field(name="What happens now",
                    value="You're being banned — that's the game. The bot **unbans everyone the moment the race "
                          "ends**. Keep this invite; it's how you get back in:\n"
                          + (race["invite_url"] or "*(the host will post the invite)*"),
                    inline=False)
    return e


# ── cog ───────────────────────────────────────────────────────────────────────

class BanRace(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._tasks = {}                  # race_id -> asyncio.Task
        self._running = {}                # guild_id -> race_id
        self._msgs = defaultdict(int)     # (race_id, user_id) -> messages this round
        self._chatter = defaultdict(int)  # race_id -> human messages since the panel was last (re)posted
        self._last_bump = {}              # race_id -> time of the last re-post
        self._chan_cache = {}             # guild_id -> (expires, race dict | None) for on_message's channel check
        self._last_card = {}              # race_id -> when its lobby card was last re-posted
        self._drops = {}                  # nonce -> {kind, race_id, claimed}
        self._lobby_sig = {}              # race_id -> the schedule last drawn on its card
        self._resumed = False
        self._rng = random.Random()
        engine.init()
        self._scheduler.start()

    def cog_unload(self):
        self._scheduler.cancel()
        for t in self._tasks.values():
            t.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._resumed:
            return
        self._resumed = True
        for race in engine.running_races():
            self._start_task(race)
            log.info("resumed race %s in guild %s", race["id"], race["guild_id"])
        # Open lobbies: re-render their embed so the rules on it are the rules
        # that will run (power-ups, tiers, lives line) — a deploy mid-lobby
        # used to leave a stale card up until the next join.
        for race in engine.lobby_races():
            guild = self.bot.get_guild(int(race["guild_id"]))
            channel = guild and await self._channel_for(guild, race)
            if channel is None:
                continue
            if await self._ensure_lobby_card(guild, channel, race):
                log.info("refreshed lobby embed for race %s", race["id"])

    # ── the schedule ──────────────────────────────────────────────────────────────────
    # Paul 9/16: "instead of starting when everyone joins, it should be
    # scheduled 4 times a day ... if there's only 2 people it should delay till
    # the next round, minimum 3." So on a scheduled server the head-count stops
    # being a trigger and becomes a gate, and one lobby stands open forever —
    # people keep their seat from one race to the next.

    def _schedule_cfg(self, guild_id):
        """(slots, tz, cfg) when this guild runs on the clock, else None. ON by
        default (Paul 9/16) — a guild opts OUT on the dashboard, and a guild
        with no race channel never gets a race either way."""
        cfg = get_config(guild_id)
        if not cfg.get("race_schedule_enabled", 1):
            return None
        try:
            slots = engine.parse_slots(cfg.get("race_schedule_slots"))
        except (ValueError, TypeError):
            slots = tuple(engine.SCHEDULE_SLOTS)      # junk in config: fall back, never kill the card
        tz = cfg.get("race_schedule_tz") or engine.SCHEDULE_TZ
        try:
            engine._zone(tz)
        except Exception:
            tz = engine.SCHEDULE_TZ                   # a bad zone can't stop the races
        return slots, tz, cfg

    def _scheduled(self, guild_id):
        return self._schedule_cfg(guild_id) is not None

    def _next_slot_ts(self, guild_id, now=None):
        sc = self._schedule_cfg(guild_id)
        if not sc:
            return None
        slots, tz, _ = sc
        try:
            return engine.next_slot(now or time.time(), slots, tz)
        except Exception:                              # bad tz in config: show no slot, keep the card
            log.exception("race schedule: next slot failed for guild %s", guild_id)
            return None

    def _card(self, guild, race, rows):
        """The lobby embed — explaining the schedule where the guild races on
        the clock, rather than counting up to a threshold."""
        sc = self._schedule_cfg(guild.id)
        sched = None
        if sc:
            slots, tz, _ = sc
            try:
                sched = {"next": engine.next_slot(time.time(), slots, tz), "slots": slots, "tz": tz}
            except Exception:
                log.exception("race schedule: next slot failed for guild %s", guild.id)
        return lobby_embed(race, rows, guild.name, schedule=sched)

    async def _lobby_message(self, channel, race):
        if not channel or not race.get("lobby_msg_id"):
            return None
        try:
            return await channel.fetch_message(int(race["lobby_msg_id"]))
        except (discord.HTTPException, ValueError):
            return None

    async def _ensure_lobby_card(self, guild, channel, race):
        """The lobby card, re-posted if it has gone missing.

        Every path used to give up when `fetch_message` 404'd, so a card that
        someone deleted left the lobby alive in the database with no buttons and
        no way back — the scheduler kept warning about a race nobody could join
        (Paul 9/19: "maybe cuz i deleted it? so it doesn't know what to do").
        Deleting the card is now just a request for a fresh one."""
        if channel is None:
            return None
        # Re-read the row. Two callers can arrive with the same stale race dict
        # — on_ready and the scheduler tick did, on the first deploy of this
        # helper — and both would then "heal" the same missing card and leave
        # two live panels in the channel.
        race = engine.get_race(race["id"]) or race
        if time.time() - self._last_card.get(race["id"], 0) < CARD_REPOST_COOLDOWN:
            return await self._lobby_message(channel, race)
        rows = engine.players(race["id"])
        msg = await self._lobby_message(channel, race)
        if msg is not None:
            try:
                await msg.edit(embed=self._card(guild, race, rows), view=lobby_view(race["id"]))
                return msg
            except discord.NotFound:
                msg = None          # deleted between the fetch and the edit
            except discord.HTTPException:
                return msg          # transient — the card is still there
        try:
            msg = await channel.send(embed=self._card(guild, race, rows),
                                     view=lobby_view(race["id"]))
        except discord.HTTPException:
            log.warning("race %s: could not re-post the lobby card", race["id"])
            return None
        engine.update_race(race["id"], lobby_msg_id=str(msg.id))
        self._chatter[race["id"]] = 0
        self._last_card[race["id"]] = time.time()
        log.info("race %s: lobby card re-posted (the old one was gone)", race["id"])
        return msg

    async def _open_lobby(self, guild, channel, host_id, settings):
        """Lock the channel, mint the return invite, create the race, post its
        card. Shared by /race start and the scheduler's standing lobby.
        Returns (race, warnings); race is None when the engine refused."""
        warn = []
        racer_role_id = None
        try:
            role = await self._ensure_racer_role(guild)
            racer_role_id = role.id
            await self._lock_channel(channel, role)
        except (discord.Forbidden, discord.HTTPException) as e:
            warn.append(f"⚠️ Couldn't set up the Racer role / channel lock ({e.__class__.__name__}) — "
                        f"I need Manage Roles and Manage Channels; anyone can talk there until then.")
        invite_url = None
        try:
            inv = await channel.create_invite(max_age=INVITE_DAYS * 86400, max_uses=0, unique=True,
                                              reason="Last to survive — return invite for the eliminated")
            invite_url = inv.url
        except discord.HTTPException:
            pass
        try:
            race = engine.create_race(guild.id, channel.id, host_id,
                                      settings=dict(settings, racer_role_id=racer_role_id))
        except ValueError as e:
            return None, [str(e)]
        engine.update_race(race["id"], invite_url=invite_url)
        race = engine.get_race(race["id"])
        if not invite_url:
            warn.append("⚠️ Couldn't mint a return invite (need Create Invite in that channel) — "
                        "post one yourself before it starts.")
        msg = await channel.send(embed=self._card(guild, race, []), view=lobby_view(race["id"]))
        engine.update_race(race["id"], lobby_msg_id=str(msg.id))
        return engine.get_race(race["id"]), warn

    @tasks.loop(seconds=SCHEDULE_CHECK_S)
    async def _scheduler(self):
        # Every guild, because the schedule is on by default now; _tick drops
        # the ones with no race channel, which is all of them until a mod picks
        # one on the dashboard.
        for guild in list(self.bot.guilds):
            try:
                await self._tick(guild.id)
            except Exception:
                log.exception("race schedule: tick failed for guild %s", guild.id)

    @_scheduler.before_loop
    async def _before_scheduler(self):
        await self.bot.wait_until_ready()

    async def _tick(self, gid):
        guild = self.bot.get_guild(int(gid))
        sc = self._schedule_cfg(gid)
        if guild is None or sc is None:
            return
        slots, tz, cfg = sc
        now = time.time()
        due = engine.due_slot(now, cfg.get("race_schedule_last_fired"), slots, tz)
        race = engine.active_race(gid)

        # A race in progress owns the channel — the slot is consumed, not queued.
        if race and race["status"] == "running":
            if due:
                set_config(gid, race_schedule_last_fired=due)
                channel = await self._channel_for(guild, race)
                nxt = engine.next_slot(now, slots, tz)
                if channel:
                    await self._say(channel, f"⏭️ The <t:{int(due)}:t> race is skipped — this one is "
                                             f"still running. Next: <t:{int(nxt)}:F> (<t:{int(nxt)}:R>).")
            return

        channel = self._configured_channel(guild)
        if channel is None:
            return

        # The lobby is infinite: if none is open, open one. It opens with an
        # empty queue (Paul 9/16 — every race starts from scratch), and the
        # card IS the announcement: no chat line beside it (Paul 9/17, "just
        # send the same card with a cleared queue").
        if race is None:
            race, warn = await self._open_lobby(guild, channel, self.bot.user.id,
                                                self._template(cfg))
            if race is None:
                return

        if race["status"] == "lobby":
            race = await self._sync_lobby(guild, channel, race, cfg, slots, tz)
        rows = engine.players(race["id"])
        need = race["settings"].get("min_players", engine.MIN_PLAYERS)
        if due:
            await self._fire_slot(guild, channel, race, rows, need, due, slots, tz, gid)
            return

        # T-15 and T-1 warnings. They do NOT ping: a warning fires before anyone
        # knows whether the slot will reach min_players, so pinging here meant
        # people were pulled in for races that then rolled over (Paul 9/19:
        # "people are getting annoyed if the game doesn't start and they are
        # getting pinged"). The names still render, just silently — the only
        # ping that survives is the one on the race actually starting.
        nxt = engine.next_slot(now, slots, tz)
        sent = cfg.get("race_schedule_warned") or []
        if float(cfg.get("race_schedule_warn_slot") or 0) != nxt:
            sent = []
        off = engine.warning_due(now, nxt, sent)
        if off is None:
            return
        set_config(gid, race_schedule_warn_slot=nxt, race_schedule_warned=list(sent) + [off])
        short = max(0, need - len(rows))
        when = "15 minutes" if off >= 900 else "1 minute"
        body = (f"⏰ **{when}** to the <t:{int(nxt)}:t> race. "
                + (f"**{len(rows)}/{need}** — it's ON." if not short
                   else f"**{len(rows)}/{need}** — {short} more or it waits for the next slot."))
        pings = " ".join(f"<@{p['user_id']}>" for p in rows[:MAX_PINGS])
        if pings:
            body += "\nIn: " + pings + "\nNot around? Hit **Leave** on the lobby."
        await self._say(channel, body, mentions=NO_MENTIONS)

    async def _fire_slot(self, guild, channel, race, rows, need, due, slots, tz, gid):
        """The slot is here: start the race, or roll it to the next one."""
        set_config(gid, race_schedule_last_fired=due, race_schedule_warned=[], race_schedule_warn_slot=0)
        lobby_msg = await self._lobby_message(channel, race)
        if len(rows) >= need:
            log.info("race %s: scheduled start (%d players)", race["id"], len(rows))
            await self._begin(guild, channel, lobby_msg, race, rows)
            return
        nxt = engine.next_slot(time.time(), slots, tz)
        log.info("guild %s: slot %s rolled over (%d/%d)", gid, int(due), len(rows), need)
        await self._say(channel,
                        f"🕗 Only **{len(rows)}/{need}** in, so the <t:{int(due)}:t> race waits. "
                        f"Next: <t:{int(nxt)}:F> (<t:{int(nxt)}:R>) — you keep your seat, "
                        f"nobody has to re-join.")
        if lobby_msg:
            try:
                await lobby_msg.edit(embed=self._card(guild, race, rows), view=lobby_view(race["id"]))
            except discord.HTTPException:
                pass

    async def _sync_lobby(self, guild, channel, race, cfg, slots=None, tz=None):
        """On a scheduled guild the dashboard is the source of truth: the
        standing lobby follows the card. Race 11 sat at a threshold of 5
        because it was opened by hand long before the card existed — Paul
        9/16, pointing at the panel: "change this to three".

        Threshold and round length are both harmless to change before a race
        starts. Mode is deliberately NOT synced: everyone in the lobby was
        vetted under the mode they joined in, so a ghost→real switch applies to
        the next lobby, not to people already sitting in this one."""
        t = self._template(cfg)
        s = dict(race["settings"])
        changed = []
        for key, label in (("min_players", "minimum"), ("round_secs", "round length")):
            try:
                cur = int(s.get(key))
            except (TypeError, ValueError):
                cur = None
            if cur != int(t[key]):
                s[key] = int(t[key])
                changed.append(f"{label} {cur} → {t[key]}")
        # The card prints the start times too, and those live in config, not in
        # the race's settings — so change the schedule on the dashboard and the
        # standing lobby would go on advertising the old times until something
        # else redrew it. Remember what was last drawn and redraw when it moves
        # (Paul 9/16, pointing at a stale number on the panel: "make sure the
        # code follows it"). An empty memo after a restart redraws once, which
        # is also a free safety net for a deploy mid-lobby.
        sig = (tuple(slots or ()), tz)
        redraw = self._lobby_sig.get(race["id"]) != sig
        self._lobby_sig[race["id"]] = sig
        if not changed and not redraw:
            return race
        if changed:
            if s.get("lives_auto"):
                s["lives"] = engine.recommended_lives(s["min_players"])
            if s.get("sudden_auto"):
                s["sudden_death_at"] = engine.recommended_sudden_death(s["min_players"])
            engine.update_race(race["id"], settings=s)
            race = engine.get_race(race["id"])
            log.info("race %s: lobby synced to the dashboard (%s)", race["id"], "; ".join(changed))
        await self._ensure_lobby_card(guild, channel, race)
        return race

    def _template(self, cfg):
        """Settings a scheduled race is built from — the dashboard's Last to
        Survive card writes these keys. Every one is coerced and falls back to
        the engine default: a typo in config must never stop the races (same
        lesson as the antinuke id coercion)."""
        def num(key, fallback, lo, hi):
            try:
                return max(lo, min(hi, int(cfg.get(key) or fallback)))
            except (TypeError, ValueError):
                return fallback
        need = num("race_schedule_min_players", engine.MIN_PLAYERS, engine.MIN_PLAYERS, 10_000)
        mode = cfg.get("race_schedule_mode")
        return {
            "lives": engine.recommended_lives(need),
            "lives_auto": True,
            "round_secs": num("race_schedule_round_secs", engine.DEFAULTS["round_secs"], 30, 1800),
            "mode": mode if mode in engine.MODES else engine.DEFAULTS["mode"],
            "min_account_days": num("race_schedule_min_account_days",
                                    engine.DEFAULTS["min_account_days"], 0, 365),
            "min_players": need,
            "sudden_death_at": engine.recommended_sudden_death(need),
            "sudden_auto": True,
        }

    async def _say(self, channel, text, mentions=None):
        try:
            await channel.send(text, allowed_mentions=mentions or NO_MENTIONS)
        except discord.HTTPException:
            pass

    # ── helpers ───────────────────────────────────────────────────────────────

    def _start_task(self, race):
        self._running[int(race["guild_id"])] = race["id"]
        t = self._tasks.get(race["id"])
        if t and not t.done():
            return
        self._tasks[race["id"]] = asyncio.create_task(self._run(race["id"]))

    def _bannable(self, guild, member):
        if member is None:
            return True     # not in the server: ban-by-id works
        return member.id != guild.owner_id and member.top_role < guild.me.top_role

    async def _channel_for(self, guild, race):
        return guild.get_channel(int(race["channel_id"]))

    def _reference(self, guild, race=None):
        """`reference_embed` with a first line that says where the game IS —
        no race, a lobby waiting, or a round you're not in. A player asking
        "what does this do?" between races gets the rules AND a way in, never
        a bare refusal."""
        settings = race["settings"] if race else None
        where = ""
        ch = self._configured_channel(guild)
        sc = self._schedule_cfg(guild.id)
        if sc and ch:
            slots, tz, _ = sc
            try:
                nxt = int(engine.next_slot(time.time(), slots, tz))
                where = f" Next one is **<t:{nxt}:R>** in {ch.mention}."
            except Exception:
                where = f" Races run in {ch.mention}."
        elif ch:
            where = f" Anyone can open one with `/race start` — it runs in {ch.mention}."
        else:
            where = (" Anyone can open one with `/race start`, but a mod has to point it at a "
                     "channel first (`/race channel`).")

        if race is None:
            header = "**No race is running right now.**" + where + " Here's the whole game anyway:"
        elif race["status"] == "lobby":
            need = race["settings"].get("min_players", engine.MIN_PLAYERS)
            header = ("**The lobby is open — the race hasn't started yet.** Hit **Join** on the card"
                      + (f" in {ch.mention}." if ch else ".")
                      + (where if sc else f" It starts at **{need}** players."))
        else:
            header = ("**A round is running.** You're not in this one — **Join** on the round card "
                      "drops you into the next round, a life lighter for every round already played.")
        return reference_embed(settings, header=header)

    def _configured_channel(self, guild):
        """The server's race channel from config, or None. Coerces the stored
        id and fails closed on junk (see antinuke id-coercion trap)."""
        raw = get_config(guild.id).get("race_channel_id")
        try:
            cid = int(raw) if raw else 0
        except (TypeError, ValueError):
            return None
        ch = guild.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    # ── the Racer role: the channel's send permission ─────────────────────────
    # "No one can message the channel unless they click Join" (Paul, 9/11).
    # @everyone loses Send in the race channel, the Racer role gets it; Join
    # grants the role, Leave / elimination / race end take it away — so ghosts
    # can watch but not talk, and the channel is silent between races.

    async def _ensure_racer_role(self, guild):
        role = discord.utils.get(guild.roles, name=RACER_ROLE)
        if role is None:
            role = await guild.create_role(name=RACER_ROLE, mentionable=False,
                                           colour=discord.Colour(COLOR),
                                           reason="Last to survive — racers can talk in the race channel")
        return role

    async def _lock_channel(self, channel, role):
        await channel.set_permissions(channel.guild.default_role, send_messages=False,
                                      send_messages_in_threads=False,
                                      reason="Last to survive — only racers talk here")
        await channel.set_permissions(role, send_messages=True, send_messages_in_threads=True,
                                      reason="Last to survive — racers talk here")

    def _racer_role(self, guild, race):
        rid = race["settings"].get("racer_role_id")
        return guild.get_role(int(rid)) if rid else None

    async def _set_racer(self, guild, race, uid, on):
        role = self._racer_role(guild, race)
        member = guild.get_member(int(uid))
        if role is None or member is None:
            return
        try:
            if on:
                await member.add_roles(role, reason="Last to survive — joined the race")
            elif role in member.roles:
                await member.remove_roles(role, reason="Last to survive — out of the race")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("race %s: racer role %s for %s failed: %s", race["id"], on, uid, e)

    async def _strip_all_racers(self, guild, race):
        for p in engine.players(race["id"]):
            await self._set_racer(guild, race, p["user_id"], False)
            await asyncio.sleep(0.3)

    async def _post_lines(self, channel, title, lines, color, content=None, extra_embed=None):
        """Send lines as one or more embeds, never over Discord's limits."""
        chunks, cur = [], []
        size = 0
        for ln in lines:
            if size + len(ln) + 1 > 3800 and cur:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(ln)
            size += len(ln) + 1
        if cur:
            chunks.append(cur)
        first = None
        for i, ch in enumerate(chunks):
            e = discord.Embed(title=title if i == 0 else f"{title} (cont.)", color=color,
                              description="\n".join(ch))
            embeds = [e]
            if extra_embed is not None and i == len(chunks) - 1:
                embeds.append(extra_embed)
            msg = await channel.send(content=content if i == 0 else None, embeds=embeds,
                                     allowed_mentions=MENTIONS)
            first = first or msg
        return first

    async def _dm(self, user_id, **kwargs):
        try:
            user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
            await user.send(**kwargs)
            return True
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return False

    async def _do_cast(self, guild, race_id, shooter_id, target_id, kind):
        """Shared by /vote, /powerup and the buttons. Returns the ephemeral text."""
        race = engine.get_race(race_id)
        if not race or race["status"] != "running":
            return "There's no round to shoot in right now."
        p = engine.player(race_id, shooter_id)
        t = engine.player(race_id, target_id)
        rn = race["round_no"]
        err = engine.cast_error(p, t, kind, round_no=rn)
        if err:
            return err
        if kind == "shot" and p["shots"] <= 0:
            mv = engine.retarget(race_id, rn, shooter_id, target_id)
            if mv:
                was = engine.player(race_id, mv["prev_target_id"]) if mv.get("prev_target_id") else None
                what = "Overload" if mv.get("kind") == "overload" else "shot"
                which = f"{what} {mv['seq']}" if mv.get("seq") else f"your {what.lower()}"
                return (f"🔁 Re-aimed {which} at **{t['name']}**"
                        f"{' (was ' + was['name'] + ')' if was else ''}. "
                        f"Still resolves <t:{int(race['round_ends_at'])}:R>.")
            return "You're out of shots this round and have nothing to re-aim. Grab a drop."
        engine.spend(p, kind)
        engine.update_player(race_id, shooter_id, shots=p["shots"],
                             **{k: p.get(k, 0) for k in engine.ITEM_COLS})
        # purge rounds hand everyone three; anything past the allowance was paid
        # for by a drop, a bounty or Arsenal — the story labels those "(extra)"
        allowance = 3 if rn == race.get("purge_round") else 1
        info = engine.cast(race_id, rn, shooter_id, target_id, kind, allowance=allowance)
        when = f"<t:{int(race['round_ends_at'])}:R>"
        seq = info.get("seq") or 1
        tag = f"**Shot {seq}{' (extra)' if info.get('extra') else ''}** "
        if kind == "shot":
            return (f"🔫 {tag}locked on **{t['name']}**. Lands {when}. " +
                    (f"**{p['shots']}** left — fire again for **Shot {seq + 1}**."
                     if p["shots"] > 0 else "That's all your shots this round.")
                    + self._slate(race_id, rn, shooter_id))
        if kind == "overload":
            return (f"💥 {tag}Overload armed at **{t['name']}** — you burn 1, they take 2. Lands {when}. "
                    + (f"**{p['shots']}** shot(s) still banked."
                       if p["shots"] > 0 else
                       "An Overload IS your shot for the round, doubled — that's all of them. "
                       "Hit **Vote** → **Change shot** to re-aim it.")
                    + self._slate(race_id, rn, shooter_id))
        if kind in engine.REVIVES:
            emoji, label, _ = engine.REVIVES[kind]
            back = engine.revive_lives(kind, race["settings"]["lives"])
            return (f"{emoji} {label} set for **{t['name']}** — they come back with **{back}** "
                    f"{'life' if back == 1 else 'lives'} when the round closes {when}. It doesn't count as your shot.")
        emoji, label, gives, costs = engine.HEALS[kind]
        return (f"{emoji} **{label}** set for **{t['name']}** — they gain **{gives}**"
                + (", you lose 1" if costs else ", it costs you nothing")
                + f". Lands {when}. It is NOT your shot — you still owe one this round.")

    def _slate(self, race_id, rn, shooter_id):
        """Every shot the player has placed this round, so a reply can never
        read as "your shot moved" when it was a second shot (Paul 9/13: race 7
        round 3 — fired shot 2 at a new target believing shot 1 had moved)."""
        fired = engine.fired_shots(race_id, rn, shooter_id)
        if len(fired) < 2:
            return ""
        names = {p["user_id"]: p["name"] for p in engine.players(race_id)}
        parts = [f"Shot {sh['seq']}{' (extra)' if sh.get('extra') else ''} → {names.get(sh['target_id'], '?')}"
                 for sh in fired]
        return ("\n📋 Your shots this round: " + " · ".join(parts)
                + ". To MOVE one, hit **Vote** → **Change shot N**.")

    async def _do_retarget(self, race_id, shooter_id, target_id, seq):
        """Move shot `seq` (fired this round) onto a new target. Same checks as
        a fresh cast minus the bank: the race must be running and the target
        alive and not yourself."""
        race = engine.get_race(race_id)
        if not race or race["status"] != "running":
            return "There's no round to shoot in right now."
        p = engine.player(race_id, shooter_id)
        t = engine.player(race_id, target_id)
        if p is None or not p["alive"]:
            return "You're out of the race."
        if t is None or not t["alive"]:
            return "That player isn't in the race (or is already gone)."
        if t["user_id"] == p["user_id"]:
            return "Shooting yourself is what backfire is for."
        mv = engine.retarget(race_id, race["round_no"], shooter_id, target_id, seq=seq)
        if not mv:
            return f"You haven't fired a shot {seq} this round."
        if mv["was"] == str(target_id):
            return f"Shot {seq} was already on **{t['name']}**."
        was = engine.player(race_id, mv["was"])
        what = "Overload" if mv["kind"] == "overload" else "Shot"
        return (f"🔁 **{what} {seq}** moved to **{t['name']}**"
                f"{' (was ' + was['name'] + ')' if was else ''}. "
                f"Lands <t:{int(race['round_ends_at'])}:R>."
                + self._slate(race_id, race["round_no"], shooter_id))

    async def _do_use_self(self, race_id, uid, kind):
        """Patch / Medkit from /powerup or the inventory button."""
        race = engine.get_race(race_id)
        if not race or race["status"] != "running":
            return "There's no round open right now."
        p = engine.player(race_id, uid)
        rn = race["round_no"]
        voted = any(s["shooter_id"] == str(uid) and s["kind"] in ("shot", "overload")
                    for s in engine.shots(race_id, rn))
        ok, text = engine.use_self(p, kind, rn, race["settings"]["lives"], voted)
        if ok:
            engine.update_player(race_id, uid, lives=p["lives"], patch=p["patch"], medkit=p["medkit"],
                                 skip_round=p.get("skip_round"))
            engine.log(race_id, rn, f"{engine.m(uid)} used a **{engine.POWERUPS[kind][1]}** — {text}",
                       kind="use", user_id=uid)
        return text

    # ── round loop ────────────────────────────────────────────────────────────

    async def _run(self, race_id):
        try:
            if (engine.get_race(race_id) or {}).get("round_no", 1) == 0:
                await asyncio.sleep(START_DELAY)     # the countdown promised by the start ping
            while True:
                race = engine.get_race(race_id)
                if not race or race["status"] != "running":
                    return
                s = race["settings"]
                guild = self.bot.get_guild(int(race["guild_id"]))
                channel = guild and await self._channel_for(guild, race)
                if channel is None:
                    engine.update_race(race_id, status="aborted", finished_at=time.time())
                    return
                rows = engine.players(race_id)
                if race["round_no"] > 0 and race["round_ends_at"]:
                    # A round is still open — we were restarted mid-round. Pick
                    # it up where it was: same round number, same close time,
                    # the shots already cast still count. (Before 9/12 a restart
                    # opened round N+1 on top and silently dropped round N's
                    # votes.) The rest of this round's drops are lost; fine.
                    round_no, ends = race["round_no"], race["round_ends_at"]
                    msg, queue = None, []
                    if race.get("round_msg_id"):
                        try:
                            msg = await channel.fetch_message(int(race["round_msg_id"]))
                        except (discord.HTTPException, ValueError):
                            msg = None
                    log.info("race %s resumed inside round %s (closes in %.0fs)",
                             race_id, round_no, max(0, ends - time.time()))
                else:
                    round_no = race["round_no"] + 1
                    sudden = engine.is_sudden_death(rows, s["sudden_death_at"])
                    extra = engine.open_round(rows, round_no, race["purge_round"], s["shot_cap"])
                    for line in extra:      # e.g. PURGE ROUND — on the card AND in the story (9/13)
                        engine.log(race_id, round_no, line, kind="open")
                    engine.save_players(race_id, rows)
                    secs = max(30, s["round_secs"] // 2 if sudden else s["round_secs"])
                    ends = time.time() + secs
                    for k in [k for k in self._msgs if k[0] == race_id]:
                        del self._msgs[k]
                    engine.update_race(race_id, round_no=round_no, round_ends_at=ends)
                    msg = await channel.send(embed=round_embed(race, rows, round_no, ends, sudden, extra),
                                             view=round_view(race_id))
                    engine.update_race(race_id, round_msg_id=str(msg.id))
                    self._chatter[race_id] = 0

                    # every round drops, scaled to the players still alive (Paul 9/12:
                    # "powerups should scale with the amount of active users");
                    # sudden death adds a super. Races started before 9/12 carry no
                    # drops_per_player in their settings — the default applies.
                    opened = time.time()
                    queue = [(opened + at, kind, is_super) for at, kind, is_super in
                             engine.drop_schedule(self._rng, secs, len(engine.alive(rows)),
                                                  s.get("drops_per_player", engine.DEFAULTS["drops_per_player"]),
                                                  sudden, s.get("drop_window", engine.DEFAULTS["drop_window"]))]
                while True:
                    race = engine.get_race(race_id)
                    if race["status"] != "running":
                        return
                    now = time.time()
                    if now >= (race["round_ends_at"] or 0):
                        break
                    while queue and now >= queue[0][0]:
                        _, kind, is_super = queue.pop(0)
                        asyncio.create_task(self._spawn_drop(race_id, channel, s, kind, is_super))
                    await asyncio.sleep(min(2.0, max(0.2, race["round_ends_at"] - now)))
                cur_id = (engine.get_race(race_id) or {}).get("round_msg_id")
                if cur_id:
                    try:
                        panel = msg if (msg is not None and str(msg.id) == str(cur_id))                             else await channel.fetch_message(int(cur_id))
                        await panel.edit(view=round_view(race_id, closed=True))
                    except (discord.HTTPException, ValueError):
                        pass
                await self._close_round(race_id, round_no, guild, channel)
                engine.update_race(race_id, round_ends_at=None)    # closed: nothing to resume
                race = engine.get_race(race_id)
                if race["status"] != "running":
                    return
                await asyncio.sleep(6)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("race %s loop crashed", race_id)

    async def _close_round(self, race_id, round_no, guild, channel):
        race = engine.get_race(race_id)
        s = race["settings"]
        rows = engine.players(race_id)
        shot_rows = engine.shots(race_id, round_no)
        sudden = engine.is_sudden_death(rows, s["sudden_death_at"])
        msgs = {p["user_id"]: self._msgs.get((race_id, p["user_id"]), 0) for p in rows}
        res = engine.resolve_round(rows, shot_rows, round_no, self._rng,
                                   backfire=s["backfire"], sudden=sudden,
                                   storm=round_no >= s["storm_from_round"], msgs=msgs,
                                   max_lives=s["lives"], shot_cap=s["shot_cap"])
        engine.save_players(race_id, rows)
        engine.mark_shots(res.get("results"))
        for ln in res["lines"]:
            engine.log(race_id, round_no, ln, kind="resolve")

        involved = []
        for sh in shot_rows:
            involved += [sh["shooter_id"], sh["target_id"]]
        involved += res["dead"]
        seen, pings = set(), []
        for uid in involved:
            if uid not in seen:
                seen.add(uid)
                pings.append(f"<@{uid}>")
        content = " ".join(pings[:MAX_PINGS]) or None
        lines = res["lines"] or ["🕊️ Nobody fired. The storm noticed."]
        board = None if res["winners"] else standings_embed(engine.get_race(race_id), rows)
        await self._post_lines(channel, f"💥 Round {round_no} — results", lines, COLOR, content=content,
                               extra_embed=board)

        for uid in res["dead"]:
            await self._eliminate(guild, channel, race, uid, round_no, res["killers"].get(uid))
        for uid in res.get("revived", []):
            await self._revive(guild, channel, race, uid, round_no)

        # the dead still watch: DM earlier casualties the round's results.
        # Real mode only — banned players can't see the channel; ghosts can,
        # so DMs there are just noise (Paul, 9/11: "turn off dms in ghost mode").
        for p in rows if s["mode"] == "real" else []:
            if not p["alive"] and p["user_id"] not in res["dead"]:
                e = discord.Embed(title=f"👻 Round {round_no} — from the grave", color=COLOR,
                                  description="\n".join(lines)[:4000])
                await self._dm(p["user_id"], embed=e)
                await asyncio.sleep(0.4)

        if res["winners"]:
            await self._finish(race_id, guild, channel, res["winners"])

    async def _eliminate(self, guild, channel, race, uid, round_no, killer_id):
        member = guild.get_member(int(uid))
        s = race["settings"]
        await self._set_racer(guild, race, uid, False)
        if s["mode"] != "real":
            return          # ghosts see everything in the channel; no DMs
        if member:
            await self._dm(uid, embed=elimination_dm(race, round_no, killer_id, guild.name))
        quiet_mark(uid)
        try:
            await guild.ban(discord.Object(id=int(uid)),
                            reason=f"Last to survive: eliminated round {round_no} (auto-unban when the race ends)",
                            delete_message_seconds=0)
            engine.update_player(race["id"], uid, banned=1)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("race %s: could not ban %s: %s", race["id"], uid, e)
            try:
                await channel.send(f"⚠️ Couldn't ban <@{uid}> ({e.__class__.__name__}) — they're out of the "
                                   f"race regardless.", allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    async def _revive(self, guild, channel, race, uid, round_no):
        """Undo an elimination for a revived player: Racer role back so they can
        talk; in a real race lift the ban and DM the invite (they left the
        server when they were banned)."""
        s = race["settings"]
        p = engine.player(race["id"], uid)
        if s["mode"] == "real" and p and p["banned"]:
            try:
                await guild.unban(discord.Object(id=int(uid)),
                                  reason=f"Last to survive: revived in round {round_no}")
            except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
                log.warning("race %s: could not unban revived %s: %s", race["id"], uid, e)
            engine.update_player(race["id"], uid, banned=0)
            invite = race.get("invite_url") or ""
            await self._dm(uid, content=(f"💫 Someone **revived** you in **{guild.name}** — you're back in the "
                                         f"race with 1 life. Rejoin now: {invite}" if invite else
                                         f"💫 Someone **revived** you in **{guild.name}** — you're back in the "
                                         f"race with 1 life. Ask the host for the invite."))
        await self._set_racer(guild, race, uid, True)

    async def _unban_all(self, guild, race):
        n = 0
        for p in engine.players(race["id"]):
            if p["banned"]:
                try:
                    await guild.unban(discord.Object(id=int(p["user_id"])),
                                      reason="Last to survive: race over — everyone comes back")
                    n += 1
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    pass
                engine.update_player(race["id"], p["user_id"], banned=0)
                await asyncio.sleep(0.3)
        return n

    async def _finish(self, race_id, guild, channel, winners):
        engine.update_race(race_id, status="finished", finished_at=time.time(), winner_ids=winners)
        self._running.pop(guild.id, None)
        self._lobby_sig.pop(race_id, None)
        race = engine.get_race(race_id)
        rows = engine.players(race_id)
        restored = await self._unban_all(guild, race)
        # The queue is cleared after every race (Paul 9/16), so nobody keeps the
        # Racer role between them either — you rejoin, you get it back.
        await self._strip_all_racers(guild, race)
        live, fallen = engine.standings(rows)
        killers = sorted(rows, key=lambda p: -p["kills"])[:3]
        if len(winners) == 1:
            title, desc = "🏆 LAST TO SURVIVE", f"<@{winners[0]}> is the last one standing after **{race['round_no']}** rounds."
        else:
            title = "🤝 Mutual destruction"
            desc = ("Nobody's left. Died in the final round together: " + ", ".join(f"<@{w}>" for w in winners)
                    + " — host settles it.")
        e = discord.Embed(title=title, description=desc, color=COLOR_WIN)
        e.add_field(name="Top killers",
                    value="\n".join(f"{p['kills']} — {p['name']}" for p in killers if p["kills"]) or "—",
                    inline=True)
        if fallen:
            e.add_field(name="Runner-up", value=fallen[0]["name"], inline=True)
        if race["settings"]["mode"] == "real":
            e.add_field(name="Everyone's back", value=f"Unbanned **{restored}** — the invite is in their DMs.",
                        inline=False)
        e.set_footer(text="Host: pay the winner. 🎁")
        await channel.send(content=" ".join(f"<@{w}>" for w in winners), embed=e, allowed_mentions=MENTIONS)
        for p in rows if race["settings"]["mode"] == "real" else []:
            if not p["alive"]:
                await self._dm(p["user_id"], content=f"Race over in **{guild.name}** — you're unbanned. "
                                                     f"{race['invite_url'] or ''}".strip())
                await asyncio.sleep(0.3)

    async def _abort(self, guild, channel, race, by):
        engine.update_race(race["id"], status="aborted", finished_at=time.time())
        self._running.pop(guild.id, None)
        t = self._tasks.pop(race["id"], None)
        if t and not t.done():
            t.cancel()
        restored = await self._unban_all(guild, race)
        await self._strip_all_racers(guild, race)
        try:
            await channel.send(f"🛑 Race stopped by {by.mention}."
                               + (f" Unbanned **{restored}**." if restored else ""),
                               allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass

    async def _spawn_drop(self, race_id, channel, s, kind=None, is_super=False):
        kind = kind or engine.roll_drop(self._rng)
        nonce = secrets.token_hex(4)
        self._drops[nonce] = {"kind": kind, "race_id": race_id, "claimed": None, "super": is_super}
        try:
            msg = await channel.send(embed=drop_embed(kind, is_super), view=drop_view(race_id, nonce))
        except discord.HTTPException:
            self._drops.pop(nonce, None)
            return
        await asyncio.sleep(s["drop_window"])
        d = self._drops.pop(nonce, None)
        if d and not d["claimed"]:
            try:
                await msg.edit(embed=discord.Embed(title="💨 The drop vanished", color=COLOR_DROP,
                                                   description="Too slow, all of you."), view=None)
            except discord.HTTPException:
                pass

    # ── /race ─────────────────────────────────────────────────────────────────

    # Open to everyone (Paul 9/12: "anyone should be able to start the race").
    # Whoever runs /race start is the host. Real bans and picking the channel
    # stay behind Manage Server (the bot bans people / locks a channel down);
    # stop and edit are the host's or a mod's.
    race = app_commands.Group(name="race", description="Last to survive — the ban race (anyone can start one)",
                              guild_only=True)

    @race.command(name="start", description="Open a lobby in the server's race channel (set with /race channel).")
    @app_commands.describe(lives="Lives per player (blank = recommended for however many join)",
                           round_minutes="Minutes per round (default 1.5; sudden death runs half that)",
                           mode="ghost = no bans (default); real = actual bans, auto-unban at the end",
                           min_account_days="Minimum account age to enter (default 7)",
                           min_players="Players needed before the race can start (default 3)",
                           sudden_death_at="Alive count that starts sudden death (blank = sized to the field: ~¼, 2–5)",
                           channel="Mods only: run this race somewhere other than the server's race channel")
    @app_commands.choices(mode=MODE_CHOICES)
    async def race_start(self, interaction: discord.Interaction,
                         lives: app_commands.Range[int, 1, 50] = None,
                         round_minutes: app_commands.Range[float, 0.5, 30.0] = 1.5,
                         mode: app_commands.Choice[str] = None,
                         min_account_days: app_commands.Range[int, 0, 365] = 7,
                         min_players: app_commands.Range[int, 3, None] = 3,     # no cap on players (Paul 9/12)
                         sudden_death_at: app_commands.Range[int, 2, 50] = None,
                         channel: discord.TextChannel = None):
        guild = interaction.guild
        mode_v = mode.value if mode else engine.DEFAULTS["mode"]
        if engine.active_race(guild.id):
            return await interaction.response.send_message(
                "A race is already on here — `/race stop` it first.", ephemeral=True)
        is_mod = interaction.user.guild_permissions.manage_guild
        if not is_mod and mode_v == "real":
            return await interaction.response.send_message(
                "Real bans are a mod's call — anyone can open a **ghost** race, but `mode: real` needs "
                "**Manage Server**.", ephemeral=True)
        if not is_mod and channel is not None:
            return await interaction.response.send_message(
                "Picking the channel needs **Manage Server** (the race locks it down). Leave `channel:` blank "
                "and it runs in the server's race channel.", ephemeral=True)
        me = guild.me.guild_permissions
        if mode_v == "real" and not me.ban_members:
            return await interaction.response.send_message(
                "I need **Ban Members** to run a real race (that's the whole game).", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        # The race channel is a SERVER SETTING (Paul 9/12: "not a 'create a
        # channel', that's crazy") — /race channel sets it; the bot never
        # creates one. A mod may still point one race elsewhere with channel:.
        if channel is None:
            channel = self._configured_channel(guild)
        if channel is None:
            return await interaction.followup.send(
                "This server has no race channel yet — a mod picks one with `/race channel #channel`.",
                ephemeral=True)

        # lives blank = auto: recommended for the lobby size, re-computed for the
        # actual head-count the moment the race starts. An explicit value is the
        # host's call and is never touched.
        lives_auto = lives is None
        race, warn = await self._open_lobby(guild, channel, interaction.user.id, {
            "lives": engine.recommended_lives(min_players) if lives_auto else lives,
            "lives_auto": lives_auto,
            "round_secs": int(round(round_minutes * 60)), "mode": mode_v,
            "min_account_days": min_account_days, "min_players": min_players,
            # a fixed 5 put a 6-player race in sudden death from round 2 (9/12);
            # blank = sized to the field, re-done for the real head-count at start
            "sudden_death_at": sudden_death_at or engine.recommended_sudden_death(min_players),
            "sudden_auto": sudden_death_at is None})
        if race is None:
            return await interaction.followup.send(warn[0], ephemeral=True)
        note = ("\n" + "\n".join(warn)) if warn else ""
        lives_note = (f" Lives are on **auto** — {race['settings']['lives']} for {min_players} players, "
                      f"re-checked for whoever's actually in when it starts."
                      if lives_auto else
                      f" Lives fixed at **{lives}** (recommended for {min_players} players: "
                      f"{engine.recommended_lives(min_players)}).")
        slot = self._next_slot_ts(guild.id)
        how = (f"It starts on the schedule — next slot <t:{int(slot)}:F> (<t:{int(slot)}:R>). "
               f"Times, minimum and mode live on the dashboard."
               if slot else "Hit **Start the race** on it when enough people have joined.")
        await interaction.followup.send(f"Lobby's open in {channel.mention}. {how}{lives_note}{note}",
                                        ephemeral=True)

    @race.command(name="stop", description="Stop the race (host or a mod). Unbans everyone it banned.")
    async def race_stop(self, interaction: discord.Interaction):
        race = engine.active_race(interaction.guild.id)
        if not race:
            return await interaction.response.send_message("No race running here.", ephemeral=True)
        if not self._is_host(interaction, race):
            return await interaction.response.send_message(
                "Only the host or a mod (**Manage Server**) can stop this race.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        channel = await self._channel_for(interaction.guild, race) or interaction.channel
        await self._abort(interaction.guild, channel, race, interaction.user)
        await interaction.followup.send("Stopped.", ephemeral=True)

    # Paul 9/12: "someone said 15 is too high" — the join threshold has to be
    # changeable on an OPEN lobby, not only at /race start. Same auto-start
    # rule as a join: if the lobby already holds that many, it goes now.
    @race.command(name="edit", description="Change an open lobby's settings before the race starts.")
    @app_commands.describe(min_players="Players needed before the race starts — it starts the moment the lobby holds this many",
                           sudden_death_at="Alive count that starts sudden death (0 = back to auto, sized to the field)",
                           round_minutes="Minutes per round for this race (sudden death runs half that)")
    async def race_edit(self, interaction: discord.Interaction,
                        min_players: app_commands.Range[int, 3, None] = None,
                        sudden_death_at: app_commands.Range[int, 0, 50] = None,
                        round_minutes: app_commands.Range[float, 0.5, 30.0] = None):
        guild = interaction.guild
        race = engine.active_race(guild.id)
        if not race:
            return await interaction.response.send_message("No race running here.", ephemeral=True)
        if not self._is_host(interaction, race):
            return await interaction.response.send_message(
                "Only the host or a mod (**Manage Server**) can change this lobby.", ephemeral=True)
        if race["status"] != "lobby":
            return await interaction.response.send_message(
                "The race is on — these only matter in the lobby.", ephemeral=True)
        if min_players is None and sudden_death_at is None and round_minutes is None:
            return await interaction.response.send_message("Give me something to change.", ephemeral=True)
        s = race["settings"]
        changes = []
        old_need = s.get("min_players", engine.MIN_PLAYERS)
        if min_players is not None and min_players != old_need:
            s["min_players"] = min_players
            if s.get("lives_auto"):
                # auto lives track the lobby size until start; keep the lobby's number honest
                s["lives"] = engine.recommended_lives(min_players)
            if s.get("sudden_auto"):
                s["sudden_death_at"] = engine.recommended_sudden_death(min_players)
            changes.append(f"threshold {old_need} → **{min_players}**")
        if sudden_death_at is not None:
            old_sd = s.get("sudden_death_at", engine.DEFAULTS["sudden_death_at"])
            if sudden_death_at == 0:
                s["sudden_auto"] = True
                s["sudden_death_at"] = engine.recommended_sudden_death(s.get("min_players", engine.MIN_PLAYERS))
                changes.append(f"sudden death {old_sd} → **auto** ({s['sudden_death_at']} for the current size, "
                               f"re-sized at start)")
            elif sudden_death_at == 1:
                return await interaction.response.send_message(
                    "Sudden death at 1 alive is the end of the race — pick 2 or more, or 0 for auto.", ephemeral=True)
            else:
                s["sudden_auto"] = False
                s["sudden_death_at"] = sudden_death_at
                changes.append(f"sudden death {old_sd} → **{sudden_death_at}** alive")
        if round_minutes is not None:
            new_secs = int(round(round_minutes * 60))
            if new_secs != s.get("round_secs"):
                changes.append(f"rounds {_fmt_round(s.get('round_secs'))} → **{_fmt_round(new_secs)}**")
                s["round_secs"] = new_secs
        if not changes:
            return await interaction.response.send_message("That's what it's already set to.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        engine.update_race(race["id"], settings=s)
        race = engine.get_race(race["id"])
        rows = engine.players(race["id"])
        need = s.get("min_players", engine.MIN_PLAYERS)
        channel = await self._channel_for(guild, race)
        lobby_msg = None
        if channel and race.get("lobby_msg_id"):
            try:
                lobby_msg = await channel.fetch_message(int(race["lobby_msg_id"]))
            except (discord.HTTPException, ValueError):
                lobby_msg = None
        summary = "; ".join(changes)
        if (min_players is not None and len(rows) >= need and channel and lobby_msg
                and not self._scheduled(guild.id)):
            await self._begin(guild, channel, lobby_msg, race, rows)
            return await interaction.followup.send(
                f"{summary} — the lobby already had {len(rows)} in, so it's starting now.", ephemeral=True)
        if lobby_msg:
            try:
                await lobby_msg.edit(embed=self._card(guild, race, rows), view=lobby_view(race["id"]))
            except discord.HTTPException:
                pass
        slot = self._next_slot_ts(guild.id)
        when = (f" Next scheduled start <t:{int(slot)}:R>." if slot else f" It starts at {need}.")
        await interaction.followup.send(f"{summary}. {len(rows)} in so far —{when}", ephemeral=True)

    @race.command(name="channel", description="Mods: set this server's race channel — where /race start opens lobbies.")
    @app_commands.describe(channel="The race channel (blank = show the current one)",
                           clear="Forget the setting (then /race start needs a mod's channel:)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def race_channel(self, interaction: discord.Interaction,
                           channel: discord.TextChannel = None, clear: bool = False):
        guild = interaction.guild
        if clear:
            set_config(guild.id, race_channel_id=None)
            return await interaction.response.send_message(
                "Race channel cleared. `/race start` needs one again — set it here, or a mod passes `channel:`.",
                ephemeral=True)
        if channel is None:
            cur = self._configured_channel(guild)
            return await interaction.response.send_message(
                f"Race channel: {cur.mention}" if cur else
                "No race channel set — `/race channel #channel` to pick one.", ephemeral=True)
        perms = channel.permissions_for(guild.me)
        missing = [name for name, ok in (("Send Messages", perms.send_messages),
                                         ("Manage Permissions", perms.manage_roles),
                                         ("Create Invite", perms.create_instant_invite)) if not ok]
        set_config(guild.id, race_channel_id=str(channel.id))
        note = ("\n" + f"⚠️ I'm missing **{', '.join(missing)}** there — the Racer lock / return invite "
                f"will fail until that's fixed." if missing else "")
        await interaction.response.send_message(
            f"Race channel set to {channel.mention}. `/race start` opens lobbies there.{note}", ephemeral=True)

    @race.command(name="status", description="Standings for the race in progress.")
    async def race_status(self, interaction: discord.Interaction):
        race = engine.active_race(interaction.guild.id)
        if not race:
            return await interaction.response.send_message("No race running here.", ephemeral=True)
        rows = engine.players(race["id"])
        if race["status"] == "lobby":
            return await interaction.response.send_message(
                embed=self._card(interaction.guild, race, rows), ephemeral=True)
        await interaction.response.send_message(embed=standings_embed(race, rows), ephemeral=True)

    # Top-level and open to everyone (Paul 9/12: "last race stats or something,
    # anyone can see anyone's rounds") — /race stays a group (subcommands cost no tree slot).
    @app_commands.command(name="lastrace",
                          description="Last to survive stats — all-time leaderboard, or one race round by round.")
    @app_commands.describe(player="One player: their all-time record, or their story in the race you pick",
                           round="Only this round of a race (blank = every round)",
                           race="A race by its number — blank = all-time stats")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
    async def lastrace(self, interaction: discord.Interaction, player: discord.Member = None,
                       round: app_commands.Range[int, 1, 500] = None,
                       race: app_commands.Range[int, 1, 10 ** 9] = None):
        history = engine.guild_races(interaction.guild.id, limit=8)
        if race is None and round is None:
            # Blank = ALL TIME (Paul 9/12). round: alone still means the latest race.
            return await self._alltime(interaction, player, history)
        if race is not None:
            race = next((r for r in history if r["id"] == race), None) or engine.get_race(race)
            if not race or str(race["guild_id"]) != str(interaction.guild.id):
                return await interaction.response.send_message(
                    "No race with that number here. Recent ones: "
                    + (", ".join(f"#{r['id']}" for r in history) or "none"), ephemeral=True)
        else:
            # A lobby has no rounds — "last race" means the last one that actually
            # ran (or the one running now), never the lobby being filled.
            race = (next((r for r in history if r["status"] != "lobby"), None)
                    or engine.latest_race(interaction.guild.id))
        if not race:
            return await interaction.response.send_message("No race has been run here.", ephemeral=True)
        if race["status"] == "lobby":
            return await interaction.response.send_message(
                f"Race #{race['id']} is still in the lobby — nothing has happened yet.", ephemeral=True)
        s = race["settings"]
        earlier = ", ".join(f"#{r['id']}" for r in history if r["id"] != race["id"])
        if player is not None:
            p = engine.player(race["id"], player.id)
            if not p:
                return await interaction.response.send_message(
                    f"**{player.display_name}** wasn't in race #{race['id']}.", ephemeral=True)
            story = engine.timeline(player.id, engine.player_shots(race["id"], player.id),
                                    engine.player_log(race["id"], player.id))
            fate = (f"still in with **{p['lives']}** lives" if p["alive"] else
                    f"out in round **{p['died_round']}**" + (" (banned)" if p["banned"] else ""))
            title = f"🔎 {p['name']} — race #{race['id']} ({race['status']})"
            desc = f"{fate} · **{p['kills']}** kills · started with {s['lives']} lives"
            kit = engine.digest_line(engine.powerup_digest(race["id"], player.id))
            if kit:
                desc += "\n" + kit
            footer = "Casts are the player's own aim; everything else is what the round log says happened."
        else:
            story = engine.by_round(engine.race_log(race["id"]))
            title = f"📜 Race #{race['id']} — the open chart ({race['status']})"
            desc = (f"**{len(engine.players(race['id']))}** players · {race['round_no']} rounds · "
                    f"every hit, miss, heal and drop. Add `player:` to follow one person.")
            footer = "The round log, verbatim."
        if round is not None:
            story = [(r, lines) for r, lines in story if r == round]
        e = discord.Embed(title=title, description=desc, color=COLOR)
        shown, dropped = engine.fit_rounds(story)
        if not shown:
            e.add_field(name="Nothing recorded",
                        value=("Nothing for " + (f"round {round}." if round else "this race yet.")), inline=False)
        for r, text in shown:
            e.add_field(name=f"Round {r}", value=text, inline=False)
        if dropped:
            footer = f"Earliest {dropped} round(s) don't fit — use round: to see one. " + footer
        if earlier:
            footer += f" · Other races: {earlier} (race: to open one)"
        e.set_footer(text=footer[:2048])
        # Public (Paul 9/12: "everyone should see it"). Mentions live inside the
        # embed, which never pings; AllowedMentions.none() makes that explicit.
        await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())

    async def _alltime(self, interaction, player, history):
        data = engine.alltime(interaction.guild.id)
        races, players = data["races"], data["players"]
        recent = "\n".join(
            f"#{r['id']} · <t:{int(r['finished_at'] or r['created_at'])}:d> · {r['round_no']} rounds · "
            f"🏆 {', '.join(r['winner_names']) or 'draw'}" for r in races[:8]) or "—"
        none = discord.AllowedMentions.none()
        if player is not None:
            st = players.get(str(player.id))
            if not st:
                return await interaction.response.send_message(
                    f"**{player.display_name}** hasn't finished a race here.", ephemeral=True)
            rate = f"{100 * st['wins'] / st['races']:.0f}%" if st["races"] else "—"
            e = discord.Embed(
                title=f"🔎 {st['name']} — all time",
                description=(f"**{st['races']}** races · **{st['wins']}** wins ({rate}) · **{st['kills']}** kills · "
                             f"**{st['shots']}** shots fired · out **{st['outs']}** times · "
                             f"**{st['rounds']}** rounds survived · "
                             f"💀 **{st.get('overkill', 0)}** overkill"),
                color=COLOR)
            lines = []
            for h in st["history"][:15]:
                result = "🏆 won" if h["won"] else (f"out r{h['out_round']}" if h["out_round"] else "survived")
                lines.append(f"#{h['race_id']} · {result} · {h['kills']} kills · {h['rounds']} rounds")
            e.add_field(name="Races", value="\n".join(lines) or "—", inline=False)
            e.set_footer(text="race: <number> opens one race with this player's round-by-round story.")
            return await interaction.response.send_message(embed=e, allowed_mentions=none)
        e = discord.Embed(
            title="🏆 Last to survive — all time",
            description=(f"**{len(races)}** races finished · **{len(players)}** players. "
                         f"Ranked by wins, then kills." if races else
                         "No race has finished here yet."),
            color=COLOR)
        board = engine.leaderboard(players)
        if board:
            medal = {0: "🥇", 1: "🥈", 2: "🥉"}
            e.add_field(name="Leaderboard", inline=False, value="\n".join(
                f"{medal.get(i, f'{i + 1}.')} **{st['name']}** — {st['wins']} wins · {st['kills']} kills · "
                f"{st['races']} races" for i, (uid, st) in enumerate(board))[:1024])
        e.add_field(name="Recent races", value=recent[:1024], inline=False)
        e.set_footer(text="race: <number> for one race · player: for someone's full record.")
        await interaction.response.send_message(embed=e, allowed_mentions=none)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        """Say why a command was refused instead of letting it time out."""
        if isinstance(error, app_commands.MissingPermissions):
            msg = "That one needs **Manage Server**."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Easy — try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "You can't use that here."
        else:
            log.exception("ban_race command error", exc_info=error)
            msg = "That didn't work. Try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    # ── /vote and /powerup ────────────────────────────────────────────────────

    @app_commands.command(name="vote", description="Last to survive: fire this round's shot at a player.")
    @app_commands.describe(player="Who you're going for")
    @app_commands.guild_only()
    async def vote(self, interaction: discord.Interaction, player: discord.Member):
        race = engine.active_race(interaction.guild.id)
        if not race or race["status"] != "running":
            return await interaction.response.send_message(
                embed=self._reference(interaction.guild, race), ephemeral=True)
        text = await self._do_cast(interaction.guild, race["id"], interaction.user.id, player.id, "shot")
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="powerup", description="Last to survive: your power-ups — or aim an Overload / a heal.")
    @app_commands.describe(use="Which power-up to use (leave empty to see what you hold)",
                           player="Who it's aimed at")
    @app_commands.choices(use=USE_CHOICES)
    @app_commands.guild_only()
    async def powerup(self, interaction: discord.Interaction, use: app_commands.Choice[str] = None,
                      player: discord.Member = None):
        race = engine.active_race(interaction.guild.id)
        if not race or race["status"] != "running":
            # No kit to show — but this is the command people reach for when
            # they want to know what a power-up does, so answer that instead.
            return await interaction.response.send_message(
                embed=self._reference(interaction.guild, race), ephemeral=True)
        if use and use.value in engine.SELF_USE:
            text = await self._do_use_self(race["id"], interaction.user.id, use.value)
            return await interaction.response.send_message(text, ephemeral=True)
        if use and player:
            text = await self._do_cast(interaction.guild, race["id"], interaction.user.id, player.id, use.value)
            return await interaction.response.send_message(text, ephemeral=True)
        if use and not player:
            return await interaction.response.send_message("Pick a `player:` to aim it at.", ephemeral=True)
        await self._send_inventory(interaction, race)

    async def _send_inventory(self, interaction, race):
        p = engine.player(race["id"], interaction.user.id)
        if not p:
            return await interaction.response.send_message(
                embed=self._reference(interaction.guild, race), ephemeral=True)
        if not p["alive"]:
            return await interaction.response.send_message(
                embed=reference_embed(race["settings"],
                                      header="**You're out — power-ups are for the living.** "
                                             "Someone holding a revive can still bring you back. "
                                             "Here's the game while you watch:"),
                ephemeral=True)
        rows = engine.players(race["id"])
        e = discord.Embed(title="🎒 Your kit", color=COLOR_DROP)
        e.add_field(name="Lives", value=_lives_bar(p["lives"], race["settings"]["lives"]), inline=True)
        e.add_field(name="Shots this round", value=str(p["shots"]), inline=True)
        e.add_field(name="Shield", value=f"🛡️ ×{p['shield']}", inline=True)
        e.add_field(name="Overload", value=f"💥 ×{p['overload']}", inline=True)
        e.add_field(name="Heal an ALLY", inline=True,
                    value=" ".join(f"{em}×{p.get(k, 0)}" for k, (em, _, _, _) in engine.HEALS.items()))
        e.add_field(name="Heal YOURSELF", value=f"🩹 ×{p.get('patch', 0)} · 🏥 ×{p.get('medkit', 0)}", inline=True)
        e.add_field(name="Revives", value=" ".join(f"{em}×{p.get(k, 0)}" for k, (em, _, _) in engine.REVIVES.items()),
                    inline=True)
        e.add_field(name="Kills", value=str(p["kills"]), inline=True)
        e.set_footer(text="Shields and extra shots work on their own. Overload, the ally heals "
                          "(💉🩸🚑⛑️) and revives need a target. Patch and Medkit are the only ones that heal "
                          "YOU, and they're instant. Everything stays with you until the race ends.")
        # _if_room, not add_chunked: the kit already carries nine stat fields,
        # and a kit that 400s reads to the player as "didn't respond in time".
        add_chunked_if_room(e, "What they do", powerup_blocks())
        add_chunked_if_room(e, "Overkill", overkill_blocks())
        view = _PowerupView(self, race["id"], p, rows)
        # discord.py treats view=None as "a view" and calls .is_finished() on it —
        # pass the kwarg only when there are buttons to show (crashed live 9/12).
        kw = {"view": view} if view.children else {}
        await interaction.response.send_message(embed=e, ephemeral=True, **kw)

    # ── buttons ───────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component or interaction.guild is None:
            return
        parsed = _parse_cid((interaction.data or {}).get("custom_id"))
        if not parsed:
            return
        action, race_id, extra = parsed
        race = engine.get_race(race_id)
        if not race or str(interaction.guild.id) != race["guild_id"]:
            return await interaction.response.send_message("That race is gone.", ephemeral=True)
        try:
            handler = getattr(self, f"_btn_{action}", None)
            if handler:
                await handler(interaction, race, extra)
        except discord.HTTPException as e:
            log.warning("race button %s failed: %s", action, e)
            await self._button_failed(interaction)
        except Exception:
            log.exception("race button %s crashed", action)
            await self._button_failed(interaction)

    @staticmethod
    async def _button_failed(interaction):
        """A handler that raised has usually not answered its interaction, and
        Discord then shows the player "didn't respond in time" with nothing to
        act on. Always put something in front of them."""
        text = "That didn't go through — hit the button again. If it keeps failing, tell the host."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    def _is_host(self, interaction, race):
        return (str(interaction.user.id) == race["host_id"]
                or interaction.user.guild_permissions.manage_guild)

    async def _refresh_lobby(self, interaction, race, closed=False):
        rows = engine.players(race["id"])
        try:
            await interaction.message.edit(embed=self._card(interaction.guild, race, rows),
                                           view=lobby_view(race["id"], closed=closed))
        except discord.HTTPException:
            pass

    async def _btn_join(self, interaction, race, _):
        if race["status"] == "running":
            return await self._late_join(interaction, race)
        if race["status"] != "lobby":
            return await interaction.response.send_message("The lobby's closed.", ephemeral=True)
        s = race["settings"]
        m = interaction.user
        err = engine.join_error(m.created_at.timestamp(), time.time(), s["min_account_days"],
                                is_bot=m.bot, bannable=self._bannable(interaction.guild, m), mode=s["mode"])
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        if engine.player(race["id"], m.id):
            return await interaction.response.send_message("You're in already.", ephemeral=True)
        engine.join(race["id"], m.id, m.display_name, s["lives"])
        rows = engine.players(race["id"])
        need = s.get("min_players", engine.MIN_PLAYERS)
        lives_txt = ("lives get set when it starts — recommended for the head-count"
                     if s.get("lives_auto") else f"{s['lives']} lives")
        slot = self._next_slot_ts(interaction.guild.id)
        if slot:
            tail = (f" ({len(rows)}/{need} — {need - len(rows)} more or it waits for the slot after.)"
                    if len(rows) < need else f" ({len(rows)}/{need} — it's on.)")
            tail += f" Starts <t:{int(slot)}:R>."
        else:
            tail = f" ({len(rows)}/{need} — it starts the moment the lobby fills.)" if len(rows) < need else ""
        await interaction.response.send_message(
            f"You're in. {lives_txt}. Don't trust anyone. 🔫" + tail, ephemeral=True)
        await self._set_racer(interaction.guild, race, m.id, True)
        if len(rows) >= need and not slot:
            # unscheduled server: a full lobby is the trigger, no waiting on the host
            await self._begin(interaction.guild, interaction.channel, interaction.message, race, rows)
        else:
            await self._refresh_lobby(interaction, race)

    async def _late_join(self, interaction, race):
        """Walking in on a race in progress. Paul 9/16: "anyone can join at
        anytime" + "give them a handicap based on how many rounds there are" —
        so entry costs a life per round already finished, and the round they
        arrive in doesn't count them AFK or feed them to the storm."""
        s = race["settings"]
        m = interaction.user
        if engine.player(race["id"], m.id):
            return await interaction.response.send_message(
                "You're already in this one — check **Standings**.", ephemeral=True)
        err = engine.join_error(m.created_at.timestamp(), time.time(), s["min_account_days"],
                                is_bot=m.bot, bannable=self._bannable(interaction.guild, m), mode=s["mode"])
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        rnd = int(race["round_no"] or 1)
        played = max(0, rnd - 1)
        lives = engine.late_join_lives(s["lives"], played)
        engine.join(race["id"], m.id, m.display_name, lives, joined_round=rnd, shots=1)
        await self._set_racer(interaction.guild, race, m.id, True)
        word = "life" if lives == 1 else "lives"
        await interaction.response.send_message(
            f"You're in, mid-race — **{lives}** {word}"
            + (f" (one off for each of the **{played}** rounds you missed)." if played else ".")
            + " You've got this round's shot, and the round you walked in on can't count you AFK. 🔫",
            ephemeral=True)
        channel = await self._channel_for(interaction.guild, race) or interaction.channel
        await self._say(channel,
                        f"🚪 {m.mention} walked into round **{rnd}** with **{lives}** {word}"
                        + (f" — handicapped for the {played} rounds already run." if played else "."),
                        mentions=MENTIONS)

    async def _btn_leave(self, interaction, race, _):
        if race["status"] != "lobby":
            return await interaction.response.send_message("Too late to leave — the race is on.", ephemeral=True)
        if engine.leave(race["id"], interaction.user.id):
            await interaction.response.send_message("Out of the lobby.", ephemeral=True)
            await self._set_racer(interaction.guild, race, interaction.user.id, False)
            await self._refresh_lobby(interaction, race)
        else:
            await interaction.response.send_message("You weren't in.", ephemeral=True)

    async def _btn_begin(self, interaction, race, _):
        if not self._is_host(interaction, race):
            return await interaction.response.send_message("Host only.", ephemeral=True)
        if race["status"] != "lobby":
            return await interaction.response.send_message("Already started.", ephemeral=True)
        rows = engine.players(race["id"])
        need = race["settings"].get("min_players", engine.MIN_PLAYERS)
        if len(rows) < need:
            return await interaction.response.send_message(
                f"Need at least {need} players ({len(rows)} in).", ephemeral=True)
        await interaction.response.send_message("🏁 Go.", ephemeral=True)
        await self._begin(interaction.guild, interaction.channel, interaction.message, race, rows)

    async def _begin(self, guild, channel, lobby_msg, race, rows):
        """Flip the lobby to running, ping everyone in, start the round loop.
        Shared by the host's Start button and the auto-start on a full lobby;
        the status check makes a double trigger harmless."""
        if engine.get_race(race["id"])["status"] != "lobby":
            return
        purge = engine.pick_purge_round(self._rng, len(rows))
        s = race["settings"]
        if s.get("lives_auto"):
            s["lives"] = engine.recommended_lives(len(rows))
            for p in rows:
                p["lives"] = s["lives"]
            engine.save_players(race["id"], rows)
        if s.get("sudden_auto"):
            s["sudden_death_at"] = engine.recommended_sudden_death(len(rows))
        if s.get("lives_auto") or s.get("sudden_auto"):
            engine.update_race(race["id"], settings=s)
        engine.update_race(race["id"], status="running", started_at=time.time(), purge_round=purge)
        race = engine.get_race(race["id"])
        try:
            if lobby_msg:
                await lobby_msg.edit(embed=self._card(guild, race, rows),
                                     view=lobby_view(race["id"], closed=True))
        except discord.HTTPException:
            pass
        if lobby_msg is None:
            log.warning("race %s: started without a lobby card to close", race["id"])
        opens = int(time.time() + START_DELAY)
        await channel.send(
            content=" ".join(f"<@{p['user_id']}>" for p in rows[:MAX_PINGS]),
            embed=discord.Embed(title="🏁 THE RACE IS ON", color=COLOR,
                                description=f"**{len(rows)}** players. {PITCH.format(lives=race['settings']['lives'])}\n\n"
                                            f"⏳ **Round 1 opens <t:{opens}:R>.**"),
            allowed_mentions=MENTIONS)
        self._start_task(race)

    async def _btn_cancel(self, interaction, race, _):
        if not self._is_host(interaction, race):
            return await interaction.response.send_message("Host only.", ephemeral=True)
        if race["status"] != "lobby":
            return await interaction.response.send_message("Use `/race stop` once it's running.", ephemeral=True)
        engine.update_race(race["id"], status="aborted", finished_at=time.time())
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        await self._refresh_lobby(interaction, engine.get_race(race["id"]), closed=True)
        await self._strip_all_racers(interaction.guild, race)

    async def _btn_vote(self, interaction, race, _):
        if race["status"] != "running":
            return await interaction.response.send_message("No round open.", ephemeral=True)
        rows = engine.players(race["id"])
        p = engine.player(race["id"], interaction.user.id)
        if not p or not p["alive"]:
            return await interaction.response.send_message("You're not in this round.", ephemeral=True)
        others = [q for q in engine.alive(rows) if q["user_id"] != str(interaction.user.id)]
        if not others:
            return await interaction.response.send_message("Nobody left to shoot.", ephemeral=True)
        note = "" if len(others) <= 25 else "\n(Showing 25 — use `/vote` for anyone else.)"
        # label the shot up front (Paul 9/12: "shot one and shot two need to be
        # labeled in the ephemeral pop up"): fired-so-far + 1, out of fired + banked
        fired = engine.fired_shots(race["id"], race["round_no"], p["user_id"])
        names = {q["user_id"]: q["name"] for q in rows}
        done = "\n".join(f"{'💥' if sh['kind'] == 'overload' else '🎯'} **Shot {sh['seq']}** → "
                         f"{names.get(sh['target_id'], '?')}" for sh in fired)
        if p["shots"] > 0:
            n, total = len(fired) + 1, len(fired) + p["shots"]
            head = (f"🔫 **Fire shot {n} of {total}** — pick a target." +
                    (f"\n{done}\n⚠️ Picking a target here fires a NEW shot — it does not move the one(s) above. "
                     f"To move one, use its **Change shot** button." if fired else "") + note)
            label = f"Fire shot {n} of {total} — who are you going for?"
        elif fired:
            head = (f"All **{len(fired)}** of your shots are placed this round.\n{done}\n"
                    f"Use a **Change shot** button to move one.{note}")
            label = None
        else:
            return await interaction.response.send_message(
                f"No shots banked this round. Grab a drop.{note}", ephemeral=True)
        await interaction.response.send_message(
            head, view=_ShotView(self, race["id"], interaction.user.id, rows, fired, label=label,
                                 with_select=p["shots"] > 0),
            ephemeral=True)

    async def _btn_powerup(self, interaction, race, _):
        if race["status"] != "running":
            return await interaction.response.send_message(
                embed=self._reference(interaction.guild, race), ephemeral=True)
        await self._send_inventory(interaction, race)

    async def _btn_standings(self, interaction, race, _):
        rows = engine.players(race["id"])
        await interaction.response.send_message(embed=standings_embed(race, rows), ephemeral=True)

    async def _btn_close(self, interaction, race, _):
        if not self._is_host(interaction, race):
            return await interaction.response.send_message("Host only.", ephemeral=True)
        if race["status"] != "running":
            return await interaction.response.send_message("No round open.", ephemeral=True)
        engine.update_race(race["id"], round_ends_at=time.time())
        await interaction.response.send_message("Closing the round.", ephemeral=True)

    async def _btn_drop(self, interaction, race, nonce):
        d = self._drops.get(nonce)
        if not d or d["race_id"] != race["id"]:
            return await interaction.response.send_message("Gone.", ephemeral=True)
        p = engine.player(race["id"], interaction.user.id)
        if not p or not p["alive"]:
            return await interaction.response.send_message("Power-ups are for players still alive.", ephemeral=True)
        if d["claimed"]:
            return await interaction.response.send_message("Too slow.", ephemeral=True)
        d["claimed"] = str(interaction.user.id)
        st = race["settings"]
        if d.get("super"):
            emoji, name, _ = engine.SUPER[d["kind"]]
            if d["kind"] == "nuke":
                engine.cast(race["id"], race["round_no"], p["user_id"], p["user_id"], "nuke")
                line = f"🧨 {p['name']} armed a **NUKE** — everyone else takes 1 when the round closes."
                engine.log(race["id"], race["round_no"], f"🧨 {engine.m(p['user_id'])} armed a **NUKE**.",
                           kind="use", user_id=p["user_id"])
            else:
                line = engine.grant_super(p, d["kind"], st["shot_cap"], st["lives"])
                engine.update_player(race["id"], p["user_id"], shots=p["shots"], lives=p["lives"],
                                     **{k: p.get(k, 0) for k in engine.ITEM_COLS})
                engine.log(race["id"], race["round_no"], line, kind="grab", user_id=p["user_id"])
            title, color = f"🌟 {emoji} {name} — taken", COLOR_SUPER
        else:
            emoji, name, _ = engine.POWERUPS[d["kind"]]
            line = engine.grant(p, d["kind"], st["shot_cap"])
            engine.update_player(race["id"], p["user_id"], shots=p["shots"], shield=p["shield"],
                                 **{k: p.get(k, 0) for k in engine.ITEM_COLS})
            engine.log(race["id"], race["round_no"], line, kind="grab", user_id=p["user_id"])
            title, color = f"⚡ {emoji} {name} — taken", COLOR_DROP
        await interaction.response.send_message(f"{emoji} **{name}** is yours.", ephemeral=True)
        try:
            await interaction.message.edit(
                embed=discord.Embed(title=title, description=line, color=color),
                view=None, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass

    # ── activity for the storm ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        # Storm activity is a PLAYER measure — humans only. A bot cannot be
        # quiet-and-therefore-stormed, so its lines never count here.
        if not message.author.bot:
            rid = self._running.get(message.guild.id)
            if rid:
                self._msgs[(rid, str(message.author.id))] += 1
        # Chat buries the panel (Paul 9/12: "bump the panel to the bottom so people
        # can see it if typing happens") — and in a race channel MOST of what
        # buries it is the bot itself: round cards, eliminations, drops. Counting
        # only humans meant the panel sank through a whole round and never came
        # back (Paul 9/19). Everything counts now except the panel message, which
        # would otherwise start the countdown to the next bump the moment it lands.
        race = self._race_in_channel(message.guild.id, message.channel.id)
        if race is None:
            return
        if str(message.id) in (str(race.get("lobby_msg_id") or ""),
                               str(race.get("round_msg_id") or "")):
            return
        self._chatter[race["id"]] += 1
        if (self._chatter[race["id"]] >= BUMP_AFTER
                and time.time() - self._last_bump.get(race["id"], 0) >= BUMP_COOLDOWN):
            self._last_bump[race["id"]] = time.time()     # claim it before awaiting
            await self._bump_panel(engine.get_race(race["id"]), message.channel)

    def _race_in_channel(self, guild_id, channel_id):
        """The lobby/running race whose channel this is, or None. Cached 20 s per
        guild so the message firehose doesn't hit sqlite on every line."""
        now = time.time()
        hit = self._chan_cache.get(guild_id)
        if hit is None or hit[0] < now:
            hit = (now + 20, engine.active_race(guild_id))
            self._chan_cache[guild_id] = hit
        race = hit[1]
        return race if race and str(race["channel_id"]) == str(channel_id) else None

    async def _bump_panel(self, race, channel):
        """Re-post the current panel (lobby card or round card) at the bottom of
        the channel and retire the old copy. Buttons carry persistent custom ids,
        so the new message works exactly like the old one; the stored message id
        is updated so joins / round-close edits target the fresh copy."""
        if not race or race["status"] not in ("lobby", "running"):
            return
        key = "lobby_msg_id" if race["status"] == "lobby" else "round_msg_id"
        old_id = race.get(key)
        if not old_id:
            if race["status"] == "lobby":
                await self._ensure_lobby_card(channel.guild, channel, race)
            return
        try:
            old = await channel.fetch_message(int(old_id))
        except (discord.HTTPException, ValueError):
            # Gone (deleted by hand, or purged). A lobby can be rebuilt from the
            # race row; a round card can't — its embed only exists on the message
            # — so that one waits for the next round to post a fresh one.
            if race["status"] == "lobby":
                await self._ensure_lobby_card(channel.guild, channel, race)
            return
        if race["status"] == "lobby":
            embed = self._card(channel.guild, race, engine.players(race["id"]))
            view = lobby_view(race["id"])
        else:
            if not old.embeds:
                return
            embed, view = old.embeds[0], round_view(race["id"])
        try:
            new = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            return
        engine.update_race(race["id"], **{key: str(new.id)})
        self._chatter[race["id"]] = 0
        try:
            await old.delete()
        except discord.HTTPException:
            try:
                await old.edit(content="⬇️ The panel moved to the bottom of the channel.", embed=None, view=None)
            except discord.HTTPException:
                pass
        log.info("race %s: %s panel bumped in #%s", race["id"], race["status"], channel.name)


async def setup(bot):
    await bot.add_cog(BanRace(bot))
