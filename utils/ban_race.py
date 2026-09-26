"""Last to survive — the ban race. Engine + store, no discord import.

The pitch Paul gives players is the whole rulebook: "you've got five lives,
there are power-ups, pick who you're going for and shoot, see you after the
round ends." Everything below exists so the bot can enforce that sentence
without anyone reading more than it.

Why the bot fires the shots instead of handing out Ban Members: Discord will
not let a member ban anyone whose top role is equal to or above their own, so
a flat "everyone can ban" role means nobody can ban anybody. The bot sits at
the top of the hierarchy and executes on players' behalf — which is also what
makes rules possible at all (lives, hidden shots, power-ups) instead of a
ten-second click race.

Round model (all resolution is SIMULTANEOUS at round close):
  * every living player gets ONE shot per round — it does not stack (Paul 9/13:
    "one shot per round, they don't compile"); extras come only from power-ups
    and are good for that round;
  * shots are cast privately and resolve together in a random order — so a
    player who dies this round still fires (dead man's shot) and two people
    focusing one target is how you kill someone in a single round;
  * a shot costs one life; at zero the bot bans you (mode "real") or just
    marks you out (mode "ghost");
  * shields eat one whole shot and are hidden until they pop; none work in
    sudden death (alive <= `sudden_death_at`, rounds also run at half length);
  * `backfire` fraction of OVERLOAD shots hit the shooter instead — a plain
    shot never turns on you (Paul 9/21: "the backfire thing should go away
    except for overload");
  * AFK costs a life: a survivor who cast nothing in a round loses one at
    round close (shields don't apply, no kill credit) — you play or you bleed;
  * the STORM takes a life from the survivor who sent the fewest chat messages each round from
    `storm_from_round` on, whenever 3+ are alive — hiding is not a strategy;
  * the top killer (2+ kills, unique max) carries a BOUNTY: finishing them is
    worth two extra shots;
  * drops are GUARANTEED every round and SCALE WITH THE PLAYERS STILL IN:
    alive × `drops_per_player` (min 1, capped so drops never overlap a
    `drop_window`), spread evenly over the middle of the timer — twenty
    players get a busy channel, three get one drop; sudden death adds a
    SUPER drop each round: nuke (everyone else takes 1 at close), full
    heal, arsenal (+3 shots) — instant on grab — and, one super in ten, the
    GOLDEN APPLE: a full heal PLUS 2 lives ABOVE max (you land on max+2,
    never lower), the only way past the cap (Paul 9/12, Minecraft
    reference; 9/26: "it should heal more points than the item below it");
  * killing blows pay a shield (or a shot if you already hold one) — unless
    the victim was AFK that round: no shield, no shot, no kill credit, no
    bounty. Shooting someone who isn't playing is free, so it pays nothing.

Power-ups (dropped in the channel, first click takes it):
  shield / shot / overload (take 1 to deal 2) / patch (heal 1) / medkit
  (heal 2, forfeit this round's vote — can't be used after voting, and the AFK
  penalty doesn't apply that round).
Healing SOMEONE ELSE is its own ladder, and it works nothing like the two
self-heals above — it is aimed at another player and lands when the round
closes: transfuse (common, +1 to them, −1 to you) / blood bag (uncommon, +1,
free) / paramedic (rare, +2) / field hospital (super, +3).

Everything is scoped by guild through the race row, and a guild can only have
one race in lobby or running at a time.
"""
import json
import os
import random
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("TORVEX_BANRACE_DB") or os.path.join(ROOT, "ban_race.db")

DEFAULT_CHANNEL = "last-to-survive"

DEFAULTS = dict(
    lives=5,
    round_secs=90,           # 1.5 min (Paul 9/13: "people are complaining it's too long")
    mode="ghost",            # ghost = marked out only; real = actual bans (opt-in)
    backfire=0.10,          # OVERLOAD shots only (9/21) — a plain shot never backfires
    sudden_death_at=5,       # overwritten at start from the head-count unless the host set it (9/12)
    min_account_days=7,
    min_players=3,           # lobby needs this many before Start works
    shot_cap=3,
    storm_from_round=2,
    drops_per_player=0.5,    # drops per round = alive players × this (min 1; Paul 9/12: "scale with active users")
    drop_window=10,          # seconds a drop stays grabbable
)

MODES = ("real", "ghost")
MIN_PLAYERS = 3   # floor for the setting; below this the game has no decisions

# Every power-up says what it gives AND what it costs (Paul 9/12: "make sure
# every power up has its benefits and weaknesses listed"). The blurb is shown
# on the drop, in the kit, and in the lobby — one string, ✅ then ❌.
POWERUPS = {
    "shield":    ("🛡️", "Shield",
                  "✅ Eats the next shot fired at you, whole. "
                  "❌ One at a time (a second becomes a Patch); OFF in sudden death; "
                  "doesn't stop the AFK penalty or the storm."),
    "reflect":   ("🪞", "Reflect",
                  "✅ The next shot fired at you bounces back — you take nothing, the shooter takes it, "
                  "and the feed says you reflected it. "
                  "❌ One at a time (a second becomes a Patch); OFF in sudden death; a nuke is a blast, "
                  "not an aimed shot, so it goes straight through."),
    "shot":      ("🔫", "Extra shot",
                  "✅ One more shot this round — fire twice. "
                  "❌ Still 1 damage each, and it's gone when the round closes — use it or lose it."),
    "overload":  ("💥", "Overload",
                  "✅ Your shot deals 2 instead of 1 — a full-health kill in two rounds, or a finisher now. "
                  "❌ Burns 1 of YOUR lives the moment it fires (it can kill you), rides on a banked shot, and "
                  "it is the ONLY shot that can backfire — one in ten comes back at you for all 3."),
    "transfuse": ("💉", "Transfuse",
                  "✅ Heals ANOTHER player 1 — keep an ally in. "
                  "❌ You lose 1 of yours; refused on your last life; lands at round close, and it does NOT "
                  "count as your vote, so you still owe a shot."),
    "bloodbag":  ("🩸", "Blood bag",
                  "✅ Heals ANOTHER player 1 and costs you nothing — the clean version of a Transfuse. "
                  "❌ Never heals you; can't take them above max; lands at round close and is NOT your vote."),
    "paramedic": ("🚑", "Paramedic",
                  "✅ Heals ANOTHER player **2** — pulls an ally straight out of the fire. "
                  "❌ Never heals you; can't take them above max; lands at round close and is NOT your vote."),
    "patch":     ("🩹", "Patch",
                  "✅ Heal 1, instantly, no strings. "
                  "❌ Can't go above max lives — wasted at full health."),
    "medkit":    ("🏥", "Medkit",
                  "✅ Heal 2, instantly. "
                  "❌ You sit this round's vote out (can't use it after you've fired); wasted at full health."),
    "revive_small": ("💫", "Small revive",
                  "✅ Bring an eliminated player back with **1** life when the round closes "
                  "(unbanned and re-invited in a real race). "
                  "❌ One life is one shot from being out again; nobody comes back twice."),
    "revive_medium": ("✨", "Medium revive",
                  "✅ Bring an eliminated player back with **2** lives when the round closes. "
                  "❌ Needs someone who's already out; nobody comes back twice."),
}

# Every revive variant: kind -> (emoji, label, lives rule). Small/medium are
# regular drops (common/rare), full/extra are SUPER drops (sudden death only,
# extra = SUPER RARE). Paul 9/13: "small revive, medium revive, full revive,
# extra revive — all placed in according drop categories."
REVIVES = {
    "revive_small":  ("💫", "Small revive",  "one"),
    "revive_medium": ("✨", "Medium revive", "two"),
    "revive_full":   ("🌟", "Full revive",   "max"),
    "revive_extra":  ("🪄", "Extra revive",  "over"),
}
REVIVE_COLS = tuple(REVIVES)

# Healing SOMEONE ELSE — the ladder (Paul 9/18: "there needs to be a heal
# others item … we need ever increasing drops to heal others. transfuse heal 1
# lose one … uncommon heal one, rare: heal 2, super are heal 3").
# kind -> (emoji, label, lives given, lives it costs the giver).
#
# They are deliberately unmistakable for the SELF heals (🩹 Patch, 🏥 Medkit,
# 💖 Full heal), which are instant and only ever touch your own bar:
#   * you AIM them at another player (never at yourself — the engine refuses),
#   * they land when the ROUND CLOSES, with the shots, so the target can die
#     first ("too late") — a self-heal never misses,
#   * they are not your vote: healing alone still takes the AFK penalty,
#   * the whole family is blood-red 💉🩸🚑⛑️ against the self heals' 🩹🏥💖.
GOLDAPPLE_OVER = 2          # how far above max lives a golden apple can take you
# kind -> (emoji, label, lives given, lives it costs the giver, lives it may
# push the TARGET above max). That last column is the whole difference between
# the ladder and 🍯 Ambrosia (Paul 9/20: "we need a heal another person item
# that goes above max. golden apple equivalent") — every other rung stops dead
# at max, Ambrosia is the only way to put someone ELSE over the cap.
HEALS = {
    "transfuse": ("💉", "Transfuse",      1, 1, 0),   # common    — the original: they gain 1, you lose 1
    "bloodbag":  ("🩸", "Blood bag",      1, 0, 0),   # uncommon  — 1, free
    "paramedic": ("🚑", "Paramedic",      2, 0, 0),   # rare      — 2
    "fieldhosp": ("⛑️", "Field hospital", 3, 0, 0),   # super     — 3, sudden death only
    "ambrosia":  ("🍯", "Ambrosia",       2, 0, GOLDAPPLE_OVER),   # SUPER RARE — 2, and they may go ABOVE max
}
HEAL_COLS = tuple(HEALS)


def heal_amount(kind):
    """(lives given to the target, lives it costs the giver)."""
    _, _, gives, costs, _ = HEALS[kind]
    return gives, costs


def revive_lives(kind, max_lives):
    """Lives a revived player comes back with."""
    rule = REVIVES[kind][2]
    if rule == "one":
        return 1
    if rule == "two":
        return min(2, max_lives)
    if rule == "max":
        return max_lives
    return max_lives + 1                      # "over": one above the cap, like a golden apple


def blurb(kind):
    """The ✅/❌ text for any power-up or super, by kind."""
    if kind in POWERUPS:
        return POWERUPS[kind][2]
    return SUPER[kind][2]


def item_face(kind):
    """(emoji, label) for anything a player can hold — regular drop or super."""
    table = POWERUPS if kind in POWERUPS else SUPER
    emoji, label, _ = table[kind][:3]
    return emoji, label
# Drop tiers (Paul 9/12): "the ones that have a pitfall should be more common;
# the ones that don't have pitfalls are rares." A pitfall is a COST you pay
# (a life, your vote) — not a mere limit like "one shield at a time".
#   common   — bites back: Overload (burns a life), Transfuse (costs a life),
#              Medkit (costs the vote)
#   uncommon — the middle rung of the heal-an-ally ladder (Paul 9/18)
#   rare     — no strings: Shield, Extra shot, Patch
# Weights live on the tier, so re-tiering a power-up is a one-word edit.
TIERS = {
    "common":   ("⚪", "Common",   3),
    "uncommon": ("🟢", "Uncommon", 2),
    "rare":     ("🟡", "Rare",     1),
}
POWERUP_TIER = {
    "overload": "common", "transfuse": "common", "medkit": "common",
    "shield": "rare", "shot": "rare", "patch": "rare", "reflect": "rare",
    "revive_small": "rare", "revive_medium": "rare",
    "bloodbag": "uncommon", "paramedic": "rare",
}
# Within a tier a kind can be rarer still. Extra shots also arrive from
# bounties, kill rewards and Arsenal, so the drop itself is halved (Paul 9/12:
# "extra shot appearing way too much").
# A mirror beats a shield outright, so it stays rarer than one — but 0.5 was
# rare enough that Paul never saw one drop (9/21: "literally no one has used a
# reflect yet. they haven't even appeared"). 0.75 is ~5% of drops, still under
# 🛡️ Shield's 7%.
# 💫/✨ Revives sat in the COMMON tier from 9/13 to 9/26 and were ~30 % of every
# drop, with nobody out to use them on (Paul 9/25: "why are revives still
# appearing so much"). A revive has no strings, so it is a rare, and the medium
# one is half of that again. They also never drop while nobody is out — see
# drop_weights.
KIND_WEIGHT = {"shot": 0.5, "reflect": 0.75, "revive_medium": 0.5}
# Paul 9/21: "ally items should be less common than everything else." The rungs
# keep their ladder against each other (the cheap one is the common one) but the
# whole family is scaled UNDER the rarest thing anyone else can grab, so an ally
# heal reads as a treat rather than filler. 0.15 puts the top rung (💉 Transfuse,
# 3 × 0.15 = 0.45) below 🔫 Extra shot and 🪞 Reflect at 0.5 — the invariant the
# tests hold: max(ally) < min(everything else), in the super table too.
ALLY_SCARCITY = 0.15
KIND_WEIGHT.update({kind: ALLY_SCARCITY for kind in HEALS})
DROP_WEIGHTS = {kind: TIERS[tier][2] * KIND_WEIGHT.get(kind, 1) for kind, tier in POWERUP_TIER.items()}


def tier_of(kind):
    """(emoji, label) of a power-up's drop tier."""
    e, label, _ = TIERS[POWERUP_TIER[kind]]
    return e, label
# ── Overkill (Paul 9/15: "when you kill them with 2 or more over their health
# … the more over they get eliminated the more benefit") ─────────────────────
# Overkill is damage that lands on a victim BEYOND what it took to eliminate
# them, inside one round: the excess on the killing blow (an Overload's 2 into
# a one-life player is 1 over) plus every shot that lands on them after they
# are already down that round. Those pile-on shots used to resolve as "wasted"
# — now they count, which is what makes a dogpile worth joining.
#
# The reward is Patches and nothing else: clean no-strings heals, never extra
# shots (Paul 9/12: "extra shot appearing way too much"), never a pitfall item.
# Every point of overkill past the first pays one, capped.
OVERKILL_MIN = 2           # below this it is flavour on the card, no reward
OVERKILL_MAX_PATCHES = 3   # even a ten-player dogpile pays at most this
OVERKILL_LABELS = ((4, "💥", "OBLITERATED"), (3, "☠️", "BRUTAL"), (2, "💀", "OVERKILL"))


def overkill_reward(over):
    """(emoji, label, patches) for `over` points of excess damage, or None
    below OVERKILL_MIN. Patches = over − 1, capped at OVERKILL_MAX_PATCHES."""
    if over < OVERKILL_MIN:
        return None
    for need, emoji, label in OVERKILL_LABELS:
        if over >= need:
            return emoji, label, min(over - 1, OVERKILL_MAX_PATCHES)
    return None


USABLE = ("overload",                                       # the ones a player has to aim
          "transfuse", "bloodbag", "paramedic", "fieldhosp", "ambrosia",  # (heals aim at the living)
          "revive_small", "revive_medium", "revive_full", "revive_extra")   # (revives aim at the dead)
SELF_USE = ("patch", "medkit")       # used on yourself, instantly
ITEM_COLS = ("overload", "transfuse", "patch", "medkit",
             "bloodbag", "paramedic", "fieldhosp", "ambrosia", "reflect", "reflect",
             "revive_small", "revive_medium", "revive_full", "revive_extra")
# Power-ups STAY with a player until the race ends (Paul 9/13, final: "unused
# power ups should stay until RACE end not round close"). Only shots reset each
# round. resolve_round(expire_items=True) still exists for a mode that wants
# use-it-or-lose-it; nothing sets it today.
EXPIRING = ("shield", "overload", "transfuse", "patch", "medkit",
            "bloodbag", "paramedic", "fieldhosp", "ambrosia",
            "revive_small", "revive_medium", "revive_full", "revive_extra")

# SUPER drops: sudden death only, one per round on top of the regular ones,
# and they fire the moment they're grabbed — no inventory, no aiming.
SUPER = {
    "nuke":     ("🧨", "NUKE",
                 "✅ Every other survivor takes 1 when the round closes — every kill is yours. "
                 "❌ It's your shot for the round, it lands at close (not now), and it makes you everyone's target."),
    "fullheal": ("💖", "Full heal",
                 "✅ Straight back to max lives. "
                 "❌ No shields exist in sudden death — lives are all you've got."),
    "arsenal":  ("🔫", "Arsenal",
                 "✅ +3 shots, right now. "
                 "❌ Still one target per shot, and unfired shots die with you."),
    "goldapple": ("🍎", "Golden apple",
                  "✅ Full heal PLUS 2 lives ABOVE max — you land on max+2, the only thing in the race "
                  "that passes the cap. "
                  "❌ SUPER RARE: one super in ten, sudden death only; a shot still takes 1 (it's a buffer, "
                  "not armor), heals can't stack past max+2."),
    "revive_full": ("🌟", "Full revive",
                    "✅ Goes to your kit: bring an eliminated player back at FULL lives when a round closes. "
                    "❌ Sudden death only; nobody comes back twice; the one you bring back is everyone's target."),
    "revive_extra": ("🪄", "Extra revive",
                     "✅ Goes to your kit: bring an eliminated player back with one life ABOVE max. "
                     "❌ SUPER RARE — as rare as the golden apple; nobody comes back twice."),
    "ambrosia": ("🍯", "Ambrosia",
                 "✅ Gives ANOTHER player **2 lives ABOVE max** — the golden apple you hand to someone "
                 "else, and the only way anyone but you gets past the cap. "
                 "❌ SUPER RARE, sudden death only; goes to your kit, lands at round close, never heals "
                 "you, and it isn't your vote."),
    "fieldhosp": ("⛑️", "Field hospital",
                  "✅ Goes to your kit: heals ANOTHER player **3** lives when the round closes — the top of "
                  "the heal-an-ally ladder. "
                  "❌ Never heals you, can't take them above max, and it isn't your vote — you still owe a shot."),
}
# Golden apple is LEGENDARY — one super drop in ten (Paul: "SUPER rare, only at
# the sudden death finale"; supers only ever fall in sudden death).
SUPER_WEIGHTS = {"nuke": 3, "fullheal": 3, "arsenal": 3, "goldapple": 1, "revive_full": 3,
                 "revive_extra": 1, "fieldhosp": 3, "ambrosia": 1}
# ⛑️ and 🍯 are ally items too, so the same scarcity applies to them (9/21).
SUPER_WEIGHTS.update({k: v * ALLY_SCARCITY for k, v in SUPER_WEIGHTS.items() if k in HEALS})

# ── heads-up: the last two standing (Paul 9/21: "when there's two people left
# make it more likely to get self heal and attack items") ────────────────
# With two alive the standard table has gone mostly dead. Two alive is ALWAYS
# sudden death (the threshold floors at 2), so 🛡️ Shield and 🪞 Reflect do
# nothing, and every heal-an-ally rung would hand lives to the ONE player
# you're trying to eliminate. That left a duel where most drops were noise.
# Heads-up the table tilts to the two things that can still decide it: hit
# harder, or stay up.
#
# Multipliers on the normal weights, not a second table, so re-tiering a
# power-up still carries its duel weight with it. 0 = it cannot drop heads-up.
# Resulting regular table: 💥 Overload 6 · 🏥 Medkit 6 · 🩹 Patch 4 ·
# 🔫 Extra shot 3 · revives 0.5 — attack 46 %, self-heal 51 %.
DUEL_ALIVE = 2
DUEL_MULT = {
    "overload": 2, "shot": 6,                          # attack (shot is halved at source)
    "patch": 4, "medkit": 2,                           # stay up
    "shield": 0, "reflect": 0,                         # off in sudden death — dead drops
    "transfuse": 0, "bloodbag": 0, "paramedic": 0,     # would heal your only opponent
    "revive_small": 0.25, "revive_medium": 0.5,        # still legal, just not the story here
}
# Same idea for the sudden-death super: 🧨 Nuke / 🔫 Arsenal / 💖 Full heal keep
# their weight, the ally heals are out, and the revives thin out — the golden
# apple stays about one super in ten, which is the whole point of it.
DUEL_SUPER_MULT = {"fieldhosp": 0, "ambrosia": 0, "revive_full": 1 / 3, "revive_extra": 0.5}

# ── small field: ally heals wait for a crowd (Paul 9/21: "ally items should come
# alive when there's more than 5 players … most people don't even use ally items
# yet") ───────────────────────────────────────────────────────────────────────
# Healing someone else is a five-way-alliance move: it only reads as generous
# while there is a crowd to be generous inside. In a thin field it is either
# pointless or a straight gift to a rival, which is why they were being grabbed
# and never spent. So the whole family — 💉🩸🚑 and the ⛑️🍯 supers — sits out
# any round that opens with ALLY_ALIVE or fewer survivors, and its weight goes
# back to the items people actually fire. Raise the number and they get rarer.
#
# Bands stack: <= ALLY_ALIVE drops the ally heals, <= DUEL_ALIVE then applies
# the heads-up tilt on top.
ALLY_ALIVE = 5
SMALL_FIELD_MULT = {kind: 0 for kind in HEALS}

# ── small-field ENDGAME (Paul 9/26: revives and ally items "should be only
# towards the end of small races") ───────────────────────────────────────────
# Race 20 (three players) dropped two Small revives in round 2 with nobody out.
# So: a revive never drops while there is nobody to bring back — any race size.
# And in a thin field (<= ALLY_ALIVE alive) the whole revive + heal-an-ally
# family waits for the endgame: someone is out AND at most LATE_ALIVE are
# standing. Heads-up still applies on top (ally heals gone, revives thinned).
# A crowd (> ALLY_ALIVE alive) keeps the standard table — ally heals drop, and
# revives drop as soon as someone is out.
LATE_ALIVE = 3


def drop_weights(alive, supers=False, dead=0):
    """The drop table a round rolls against, given the survivors it opens with
    and `dead` = eliminated players who could still be revived.
    Revives need someone to revive. Thin fields (`alive` <= ALLY_ALIVE) hold
    the revive + heal-an-ally family back until the endgame (`dead` >= 1 and
    `alive` <= LATE_ALIVE); heads-up (`alive` <= DUEL_ALIVE) also tilts to
    attack and self-heal and drops the kinds that do nothing there. Above
    ALLY_ALIVE it's the standard table."""
    base = SUPER_WEIGHTS if supers else DROP_WEIGHTS
    mult = {k: 1 for k in base}                       # the bands COMPOSE: a 0 stays a 0

    def scale(factors):
        for k, f in factors.items():
            if k in mult:
                mult[k] *= f

    if dead <= 0:
        scale({kind: 0 for kind in REVIVES})
    if alive <= ALLY_ALIVE:
        late = dead >= 1 and alive <= LATE_ALIVE
        if not late:
            scale(SMALL_FIELD_MULT)
            scale({kind: 0 for kind in REVIVES})
        if alive <= DUEL_ALIVE:
            scale(DUEL_SUPER_MULT if supers else DUEL_MULT)
    return {k: v * mult[k] for k, v in base.items() if v * mult[k] > 0}

# ── the guide's shape: BY TYPE, one short line each ───────────────────────────
# Paul 9/20: "the powerups should be grouped by type … healing items, shooting
# items, reviving items, defense items, and each listed just as short as
# possible with effect and side effect." The long ✅/❌ blurbs above stay on the
# DROP card, where a player reads one item at a time; the guide lists every
# item at once, so it reads from BRIEF instead.
#
# A rule the whole family obeys is stated ONCE, in the group header — that is
# what kept the old list bloated: every ally heal repeated "lands at round
# close, is not your vote".
ITEM_GROUPS = {
    "shoot":     ("🔫", "Shooting", ""),
    "defend":    ("🛡️", "Defense", ""),
    "heal_self": ("🩹", "Healing — yourself",
                  "instant, and you are capped at max unless you get the 🍎 Golden apple"),
    "heal_ally": ("💉", "Healing — an ally",
                  f"aimed at another player, lands at round close, and never your vote — you still owe "
                  f"a shot. Only 🍯 Ambrosia can take them above max. **They drop with a crowd (more "
                  f"than {ALLY_ALIVE} alive) or in a small field's endgame (someone out, {LATE_ALIVE} or "
                  f"fewer alive)**"),
    "revive":    ("💫", "Revives",
                  f"aimed at someone already out, land at round close, nobody comes back twice. "
                  f"**They only drop once someone is out** — in a small field, only in the endgame "
                  f"({LATE_ALIVE} or fewer alive)"),
}
ITEM_GROUP = {
    "overload": "shoot", "shot": "shoot", "nuke": "shoot", "arsenal": "shoot",
    "shield": "defend", "reflect": "defend", "goldapple": "heal_self",
    "patch": "heal_self", "medkit": "heal_self", "fullheal": "heal_self",
    "transfuse": "heal_ally", "bloodbag": "heal_ally", "paramedic": "heal_ally",
    "fieldhosp": "heal_ally", "ambrosia": "heal_ally",
    "revive_small": "revive", "revive_medium": "revive",
    "revive_full": "revive", "revive_extra": "revive",
}
# kind -> (what it does, what it costs you). An empty cost = no strings.
BRIEF = {
    "overload":   ("your shot deals 2 instead of 1",
                   "burns 1 of YOUR lives; needs an unfired shot — arm it instead of shooting; the only "
                   "shot that can backfire"),
    "shot":       ("one more shot this round", "1 damage each, and it's gone at round close"),
    "nuke":       ("1 damage to every other survivor at round close",
                   "it IS your shot for the round, and it paints a target on you"),
    "arsenal":    ("+3 shots, right now", "one target per shot; unfired shots die with you"),
    "reflect":    ("the next shot at you bounces back and hits whoever fired it",
                   "one at a time (a second becomes a Patch); OFF in sudden death; a nuke goes through it"),
    "shield":     ("eats the next shot at you, whole",
                   "one at a time (a second becomes a Patch); OFF in sudden death; no help against "
                   "the AFK penalty or the storm"),
    "goldapple":  ("full heal PLUS 2 lives ABOVE max — you land on max+2 (the only thing that passes the cap)",
                   "a shot still takes 1: a buffer, not armour"),
    "patch":      ("heal 1", "wasted at full lives"),
    "medkit":     ("heal 2", "costs you this round's vote; refused once you've fired"),
    "fullheal":   ("straight back to max lives", ""),
    "transfuse":  ("heals them 1", "costs you 1 life; refused on your last one"),
    "bloodbag":   ("heals them 1, free", ""),
    "paramedic":  ("heals them 2", ""),
    "fieldhosp":  ("heals them 3", ""),
    "ambrosia":   ("gives them 2 lives ABOVE max — the golden apple for someone else",
                   "the only heal that passes the cap; still never heals you"),
    "revive_small":  ("back with 1 life", "one shot from being out again"),
    "revive_medium": ("back with 2 lives", ""),
    "revive_full":   ("back at FULL lives", "the one you bring back is everyone's target"),
    "revive_extra":  ("back with one life ABOVE max", ""),
}


def rarity_mark(kind):
    """The one-glyph rarity for the guide: ⚪🟢🟡 for a regular drop, 🌟 for a
    super, 🌟🌟 for the two super-rares. Read from the same tables the drop
    roll uses, so a re-tier can't leave the guide lying."""
    if kind in POWERUP_TIER:
        return TIERS[POWERUP_TIER[kind]][0]
    return "🌟🌟" if SUPER_WEIGHTS.get(kind) == 1 else "🌟"


def grouped_items():
    """[(group key, emoji, title, shared-rules note, [(kind, emoji, label,
    rarity, effect, cost), …]), …] — the guide's whole catalogue, regular
    drops and supers together, ordered by ITEM_GROUPS. Within a group the
    commoner things come first, so each list reads cheapest to rarest."""
    order = {"⚪": 0, "🟢": 1, "🟡": 2, "🌟": 3, "🌟🌟": 4}
    out = []
    for key, (g_emoji, title, note) in ITEM_GROUPS.items():
        items = []
        for kind, group in ITEM_GROUP.items():
            if group != key:
                continue
            emoji, label = item_face(kind)
            effect, cost = BRIEF[kind]
            items.append((kind, emoji, label, rarity_mark(kind), effect, cost))
        items.sort(key=lambda it: order.get(it[3], 9))
        out.append((key, g_emoji, title, note, items))
    return out

ACTIVE = ("lobby", "running")


# ── store ─────────────────────────────────────────────────────────────────────

def _conn(db=None):
    c = sqlite3.connect(db or DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init(db=None):
    with _conn(db) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS races (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      TEXT NOT NULL,
                channel_id    TEXT NOT NULL,
                host_id       TEXT NOT NULL,
                status        TEXT NOT NULL,          -- lobby|running|finished|aborted
                settings      TEXT NOT NULL,          -- json
                round_no      INTEGER NOT NULL DEFAULT 0,
                round_ends_at REAL,
                lobby_msg_id  TEXT,
                round_msg_id  TEXT,
                invite_url    TEXT,
                created_at    REAL NOT NULL,
                started_at    REAL,
                finished_at   REAL,
                winner_ids    TEXT                    -- json list
            );
            CREATE INDEX IF NOT EXISTS idx_race_guild ON races(guild_id, status);
            CREATE TABLE IF NOT EXISTS players (
                race_id     INTEGER NOT NULL,
                user_id     TEXT NOT NULL,
                name        TEXT NOT NULL,
                lives       INTEGER NOT NULL,
                shots       INTEGER NOT NULL DEFAULT 0,
                shield      INTEGER NOT NULL DEFAULT 0,
                overload    INTEGER NOT NULL DEFAULT 0,
                transfuse   INTEGER NOT NULL DEFAULT 0,
                kills       INTEGER NOT NULL DEFAULT 0,
                bounty      INTEGER NOT NULL DEFAULT 0,
                alive       INTEGER NOT NULL DEFAULT 1,
                died_round  INTEGER,
                banned      INTEGER NOT NULL DEFAULT 0,
                joined_at   REAL NOT NULL,
                patch       INTEGER NOT NULL DEFAULT 0,
                medkit      INTEGER NOT NULL DEFAULT 0,
                skip_round  INTEGER,                 -- round whose vote a medkit forfeited
                PRIMARY KEY (race_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS shots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id    INTEGER NOT NULL,
                round_no   INTEGER NOT NULL,
                shooter_id TEXT NOT NULL,
                target_id  TEXT NOT NULL,
                kind       TEXT NOT NULL,             -- shot|overload|nuke|a heal|a revive
                ts         REAL NOT NULL,
                seq        INTEGER,                   -- shot 1, shot 2 … per shooter per round (banked shots only)
                extra      INTEGER NOT NULL DEFAULT 0, -- 1 = beyond the round's allowance (a drop/reward paid for it)
                result     TEXT,                      -- hit|kill|shielded|backfire|wasted|self … written at close
                prev_target_id TEXT                   -- the ORIGINAL aim when the shot was re-aimed
            );
            CREATE INDEX IF NOT EXISTS idx_shots_round ON shots(race_id, round_no);
            CREATE TABLE IF NOT EXISTS log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id  INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                ts       REAL NOT NULL,
                text     TEXT NOT NULL,
                kind     TEXT,                        -- grab|use|resolve|open … (9/12; older rows NULL)
                user_id  TEXT                         -- the player a grab/use belongs to
            );
        """)
        # columns added after the first deploy (9/11 evening): idempotent
        for table, cols in (
            ("players", (("patch", "INTEGER NOT NULL DEFAULT 0"),
                         ("medkit", "INTEGER NOT NULL DEFAULT 0"),
                         ("skip_round", "INTEGER"),
                         ("joined_round", "INTEGER"),
                         ("revived", "INTEGER NOT NULL DEFAULT 0"),
                         ("revive_small", "INTEGER NOT NULL DEFAULT 0"),
                         ("revive_medium", "INTEGER NOT NULL DEFAULT 0"),
                         ("revive_full", "INTEGER NOT NULL DEFAULT 0"),
                         ("revive_extra", "INTEGER NOT NULL DEFAULT 0"),
                         ("overkill", "INTEGER NOT NULL DEFAULT 0"),
                         ("bloodbag", "INTEGER NOT NULL DEFAULT 0"),
                         ("paramedic", "INTEGER NOT NULL DEFAULT 0"),
                         ("fieldhosp", "INTEGER NOT NULL DEFAULT 0"),
                         ("ambrosia", "INTEGER NOT NULL DEFAULT 0"),
                         ("reflect", "INTEGER NOT NULL DEFAULT 0"))),
            ("shots", (("seq", "INTEGER"),
                       ("extra", "INTEGER NOT NULL DEFAULT 0"),
                       ("result", "TEXT"),
                       ("prev_target_id", "TEXT"))),
            ("log", (("kind", "TEXT"),
                     ("user_id", "TEXT"))),
        ):
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols:
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _race_row(r):
    if r is None:
        return None
    d = dict(r)
    d["settings"] = json.loads(d["settings"] or "{}")
    d["winner_ids"] = json.loads(d["winner_ids"] or "[]")
    return d


def create_race(guild_id, channel_id, host_id, settings=None, now=None, db=None):
    """One race in lobby/running per guild. Returns the race dict or raises
    ValueError when one is already on."""
    init(db)
    now = now or time.time()
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (settings or {}).items() if v is not None})
    if s["mode"] not in MODES:
        raise ValueError("mode must be real or ghost")
    s["min_players"] = max(MIN_PLAYERS, int(s["min_players"]))
    with _conn(db) as c:
        row = c.execute("SELECT id FROM races WHERE guild_id=? AND status IN ('lobby','running')",
                        (str(guild_id),)).fetchone()
        if row:
            raise ValueError("a race is already on in this server")
        cur = c.execute(
            "INSERT INTO races (guild_id, channel_id, host_id, status, settings, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (str(guild_id), str(channel_id), str(host_id), "lobby", json.dumps(s), now))
        rid = cur.lastrowid
    return get_race(rid, db)


def get_race(race_id, db=None):
    with _conn(db) as c:
        return _race_row(c.execute("SELECT * FROM races WHERE id=?", (race_id,)).fetchone())


def active_race(guild_id, db=None):
    init(db)
    with _conn(db) as c:
        return _race_row(c.execute(
            "SELECT * FROM races WHERE guild_id=? AND status IN ('lobby','running')"
            " ORDER BY id DESC LIMIT 1", (str(guild_id),)).fetchone())


def latest_race(guild_id, db=None):
    """The guild's most recent race in any state — for post-mortems."""
    init(db)
    with _conn(db) as c:
        return _race_row(c.execute(
            "SELECT * FROM races WHERE guild_id=? ORDER BY id DESC LIMIT 1",
            (str(guild_id),)).fetchone())


def guild_races(guild_id, limit=10, db=None):
    """The guild's races, newest first — for looking back."""
    init(db)
    with _conn(db) as c:
        rows = c.execute("SELECT * FROM races WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                         (str(guild_id), limit)).fetchall()
    return [_race_row(r) for r in rows]


def alltime(guild_id, db=None):
    """Career numbers across every FINISHED race in the guild (aborted ones
    never crowned anyone, so they don't count). Returns
      races   — finished races, newest first, each with `winner_names`
      players — {user_id: {name, races, wins, kills, outs, rounds, shots, overkill}}
                rounds = rounds survived (died_round, or the race length if
                they were standing at the end); name = latest seen.
    Only structured columns are used — nothing is parsed out of log text."""
    init(db)
    with _conn(db) as c:
        races = [_race_row(r) for r in c.execute(
            "SELECT * FROM races WHERE guild_id=? AND status='finished' ORDER BY id DESC",
            (str(guild_id),)).fetchall()]
        if not races:
            return {"races": [], "players": {}}
        ids = [r["id"] for r in races]
        q = ",".join("?" * len(ids))
        prow = c.execute(f"SELECT * FROM players WHERE race_id IN ({q}) ORDER BY race_id DESC",
                         ids).fetchall()
        shots = {(str(r["race_id"]), r["shooter_id"]): r["n"] for r in c.execute(
            f"SELECT race_id, shooter_id, COUNT(*) n FROM shots WHERE race_id IN ({q})"
            " AND kind IN ('shot','overload','nuke') GROUP BY race_id, shooter_id", ids).fetchall()}
    by_id = {r["id"]: r for r in races}
    names = {}
    players = {}
    for p in prow:
        p = dict(p)
        race = by_id[p["race_id"]]
        uid = p["user_id"]
        names.setdefault(uid, p["name"])
        st = players.setdefault(uid, {"name": p["name"], "races": 0, "wins": 0, "kills": 0,
                                      "outs": 0, "rounds": 0, "shots": 0, "overkill": 0,
                                      "history": []})
        won = uid in race["winner_ids"]
        survived = p["died_round"] if p["died_round"] else race["round_no"]
        st["races"] += 1
        st["wins"] += 1 if won else 0
        st["kills"] += p["kills"] or 0
        st["outs"] += 0 if p["alive"] else 1
        st["rounds"] += survived or 0
        st["shots"] += shots.get((str(p["race_id"]), uid), 0)
        st["overkill"] += p.get("overkill") or 0
        st["history"].append({"race_id": p["race_id"], "won": won, "kills": p["kills"] or 0,
                              "out_round": p["died_round"], "rounds": race["round_no"],
                              "finished_at": race["finished_at"]})
    for r in races:
        r["winner_names"] = [names.get(w, w) for w in r["winner_ids"]]
    return {"races": races, "players": players}


def leaderboard(players, limit=15):
    """Players ordered by wins, then kills, then races — the all-time table."""
    rows = sorted(players.items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["kills"], -kv[1]["races"],
                                                   kv[1]["name"].lower()))
    return [(uid, st) for uid, st in rows[:limit]]


def lobby_races(db=None):
    init(db)
    with _conn(db) as c:
        return [_race_row(r) for r in c.execute("SELECT * FROM races WHERE status='lobby'")]


def running_races(db=None):
    init(db)
    with _conn(db) as c:
        return [_race_row(r) for r in c.execute("SELECT * FROM races WHERE status='running'")]


def update_race(race_id, db=None, **fields):
    if "winner_ids" in fields:
        fields["winner_ids"] = json.dumps([str(x) for x in fields["winner_ids"]])
    if "settings" in fields:
        fields["settings"] = json.dumps(fields["settings"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn(db) as c:
        c.execute(f"UPDATE races SET {cols} WHERE id=?", (*fields.values(), race_id))


def join(race_id, user_id, name, lives, now=None, db=None, joined_round=None, shots=0):
    """`joined_round` marks a late entry: the round they walked in on, which
    buys them that one round's exemption from the AFK penalty and the storm
    (they weren't here to be quiet). `shots` hands them this round's shot so
    they can fight immediately instead of watching."""
    with _conn(db) as c:
        c.execute("INSERT OR IGNORE INTO players (race_id, user_id, name, lives, joined_at,"
                  " joined_round, shots) VALUES (?,?,?,?,?,?,?)",
                  (race_id, str(user_id), name, lives, now or time.time(), joined_round, shots))


def leave(race_id, user_id, db=None):
    with _conn(db) as c:
        return c.execute("DELETE FROM players WHERE race_id=? AND user_id=?",
                         (race_id, str(user_id))).rowcount


def players(race_id, db=None):
    with _conn(db) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM players WHERE race_id=? ORDER BY joined_at", (race_id,))]


def player(race_id, user_id, db=None):
    with _conn(db) as c:
        r = c.execute("SELECT * FROM players WHERE race_id=? AND user_id=?",
                      (race_id, str(user_id))).fetchone()
        return dict(r) if r else None


def save_players(race_id, rows, db=None):
    with _conn(db) as c:
        for p in rows:
            c.execute(
                "UPDATE players SET lives=?, shots=?, shield=?, overload=?, transfuse=?, kills=?,"
                " bounty=?, alive=?, died_round=?, banned=?, patch=?, medkit=?, skip_round=?,"
                " revived=?, revive_small=?, revive_medium=?, revive_full=?, revive_extra=?,"
                " overkill=?, bloodbag=?, paramedic=?, fieldhosp=?"
                " WHERE race_id=? AND user_id=?",
                (p["lives"], p["shots"], p["shield"], p["overload"], p["transfuse"], p["kills"],
                 p["bounty"], p["alive"], p["died_round"], p["banned"], p.get("patch", 0),
                 p.get("medkit", 0), p.get("skip_round"), p.get("revived", 0),
                 p.get("revive_small", 0), p.get("revive_medium", 0), p.get("revive_full", 0),
                 p.get("revive_extra", 0), p.get("overkill", 0) or 0,
                 p.get("bloodbag", 0), p.get("paramedic", 0), p.get("fieldhosp", 0),
                 race_id, str(p["user_id"])))


def update_player(race_id, user_id, db=None, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn(db) as c:
        c.execute(f"UPDATE players SET {cols} WHERE race_id=? AND user_id=?",
                  (*fields.values(), race_id, str(user_id)))


BANKED = ("shot", "overload")   # the kinds that spend a banked shot — these get numbered


def cast(race_id, round_no, shooter_id, target_id, kind="shot", now=None, db=None, allowance=1):
    """Record a cast. Banked shots (shot/overload) are numbered per shooter per
    round — shot 1, shot 2 … — and flagged `extra` past `allowance` (always 1
    since the purge round was removed on 9/21), so the story can say WHICH shot
    did what and which ones a drop or reward paid for. Returns {id, seq, extra}."""
    with _conn(db) as c:
        seq, extra = None, 0
        if kind in BANKED:
            n = c.execute("SELECT COUNT(*) FROM shots WHERE race_id=? AND round_no=? AND shooter_id=?"
                          " AND kind IN ('shot','overload')",
                          (race_id, round_no, str(shooter_id))).fetchone()[0]
            seq = n + 1
            extra = 1 if seq > max(1, allowance) else 0
        cur = c.execute("INSERT INTO shots (race_id, round_no, shooter_id, target_id, kind, ts, seq, extra)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (race_id, round_no, str(shooter_id), str(target_id), kind, now or time.time(),
                         seq, extra))
        return {"id": cur.lastrowid, "seq": seq, "extra": extra}


def retarget(race_id, round_no, shooter_id, target_id, db=None, seq=None):
    """Re-aim one of the shooter's shots this round: shot `seq` when given
    (shot or overload — both are aimed), else the most recent plain shot.
    Returns the moved row's {seq, kind, was, prev_target_id} — `was` = the
    target it had a moment ago — or None if there was nothing to move. The
    ORIGINAL aim is kept in prev_target_id (first re-aim wins) so the story
    can show the change of mind."""
    with _conn(db) as c:
        if seq is not None:
            r = c.execute("SELECT id, seq, kind, target_id, prev_target_id FROM shots WHERE race_id=?"
                          " AND round_no=? AND shooter_id=? AND seq=? AND kind IN ('shot','overload')"
                          " ORDER BY id DESC LIMIT 1",
                          (race_id, round_no, str(shooter_id), seq)).fetchone()
        else:
            # An overload IS the shot you have this round, so it has to be
            # re-aimable the same way. Looking only at kind='shot' left a player
            # who overloaded their single shot with nothing to move and the
            # reply "you're out of shots and have nothing to re-aim".
            r = c.execute("SELECT id, seq, kind, target_id, prev_target_id FROM shots WHERE race_id=?"
                          " AND round_no=? AND shooter_id=? AND kind IN ('shot','overload')"
                          " ORDER BY id DESC LIMIT 1",
                          (race_id, round_no, str(shooter_id))).fetchone()
        if not r:
            return None
        prev = r["prev_target_id"] or r["target_id"]
        c.execute("UPDATE shots SET target_id=?, prev_target_id=? WHERE id=?",
                  (str(target_id), prev, r["id"]))
        return {"seq": r["seq"], "kind": r["kind"], "was": r["target_id"], "prev_target_id": prev}


def fired_shots(race_id, round_no, shooter_id, db=None):
    """The shooter's banked casts this round (shot/overload), in order — what
    the Vote pop-up offers to change."""
    with _conn(db) as c:
        rows = c.execute("SELECT seq, kind, target_id, prev_target_id FROM shots WHERE race_id=?"
                         " AND round_no=? AND shooter_id=? AND kind IN ('shot','overload') ORDER BY id",
                         (race_id, round_no, str(shooter_id))).fetchall()
    return [dict(r) for r in rows]


def mark_shots(results, db=None):
    """Write each shot's outcome back after the round resolves:
    {shot_id: result}."""
    if not results:
        return
    with _conn(db) as c:
        c.executemany("UPDATE shots SET result=? WHERE id=?",
                      [(res, sid) for sid, res in results.items()])


def shots(race_id, round_no, db=None):
    with _conn(db) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM shots WHERE race_id=? AND round_no=? ORDER BY id", (race_id, round_no))]


def log(race_id, round_no, text, now=None, db=None, kind=None, user_id=None):
    """`kind` = grab|use|resolve|open (what sort of line); `user_id` = whose
    grab/use it is. Both optional — resolution lines name several people."""
    with _conn(db) as c:
        c.execute("INSERT INTO log (race_id, round_no, ts, text, kind, user_id) VALUES (?,?,?,?,?,?)",
                  (race_id, round_no, now or time.time(), text, kind,
                   str(user_id) if user_id is not None else None))


def player_log(race_id, user_id, db=None):
    """Every logged line that mentions the player, in order: resolution
    lines (hits, shields, kills, AFK, storm), drops grabbed, items used."""
    with _conn(db) as c:
        rows = c.execute("SELECT round_no, ts, text, kind FROM log WHERE race_id=?"
                         " AND (user_id=? OR text LIKE ?) ORDER BY id",
                         (race_id, str(user_id), f"%<@{user_id}>%")).fetchall()
    return [dict(r) for r in rows]


def powerup_digest(race_id, user_id, db=None):
    """What one player grabbed and used across a race — the header line of
    their story. Grabs come from tagged log rows (9/12+); uses from the shots
    table (overload/transfuse/nuke casts, extra shots fired) plus tagged
    self-use lines (patch/medkit); shields that popped from the resolve text.
    Returns {"grabbed": {name: n}, "used": {name: n}}."""
    uid = str(user_id)
    grabbed, used = {}, {}

    def bump(d, k, n=1):
        d[k] = d.get(k, 0) + n

    with _conn(db) as c:
        for r in c.execute("SELECT text FROM log WHERE race_id=? AND kind='grab' AND user_id=?", (race_id, uid)):
            name = _bold_name(r["text"])
            if name:
                bump(grabbed, name)
        for r in c.execute("SELECT text FROM log WHERE race_id=? AND kind='use' AND user_id=?", (race_id, uid)):
            name = _bold_name(r["text"])
            if name:
                bump(used, name)
        cast_names = {"overload": "Overload", "nuke": "Nuke"}
        cast_names.update({k: v[1] for k, v in HEALS.items()})     # Transfuse, Blood bag, Paramedic, Field hospital
        holes = ",".join("?" * len(cast_names))
        for r in c.execute(f"SELECT kind, COUNT(*) n FROM shots WHERE race_id=? AND shooter_id=?"
                           f" AND kind IN ({holes}) GROUP BY kind", (race_id, uid, *cast_names)):
            bump(used, cast_names[r["kind"]], r["n"])
        n = c.execute("SELECT COUNT(*) FROM shots WHERE race_id=? AND shooter_id=? AND extra=1",
                      (race_id, uid)).fetchone()[0]
        if n:
            bump(used, "Extra shot", n)
        n = c.execute("SELECT COUNT(*) FROM log WHERE race_id=? AND text LIKE ?",
                      (race_id, f"%{m(uid)}'s **shield** ate%")).fetchone()[0]
        if n:
            bump(used, "Shield", n)
    return {"grabbed": grabbed, "used": used}


def _bold_name(text):
    """The first **bold** span of a log line — grab/use lines bold the item."""
    i = text.find("**")
    if i < 0:
        return None
    j = text.find("**", i + 2)
    return text[i + 2:j] if j > i else None


def digest_line(d):
    """'🎒 grabbed 3 (Shield ×2, Patch) · used 2 (Shield, Extra shot)' or ''."""
    def part(label, counts):
        if not counts:
            return None
        items = ", ".join(f"{k} ×{n}" if n > 1 else k for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        return f"{label} {sum(counts.values())} ({items})"
    bits = [b for b in (part("grabbed", d["grabbed"]), part("used", d["used"])) if b]
    return ("🎒 " + " · ".join(bits)) if bits else ""


def race_log(race_id, db=None):
    """The whole round log, in order — the open chart."""
    with _conn(db) as c:
        rows = c.execute("SELECT round_no, ts, text FROM log WHERE race_id=? ORDER BY id",
                         (race_id,)).fetchall()
    return [dict(r) for r in rows]


def by_round(log_rows):
    """[(round_no, [lines])] in round order."""
    grouped = {}
    for lg in log_rows:
        grouped.setdefault(lg["round_no"], []).append(lg["text"])
    return [(r, grouped[r]) for r in sorted(grouped)]


def fit_rounds(story, max_fields=24, max_chars=5200, field_chars=1000):
    """Newest rounds first, to fit a Discord embed (25 fields, 6000 chars,
    1024 per field). Returns ([(round_no, text)] in round order, how many of
    the earliest rounds didn't fit)."""
    out, total = [], 0
    for r, lines in reversed(story):
        text = "\n".join(lines)
        if len(text) > field_chars:
            text = text[:field_chars - 2] + " …"
        if len(out) >= max_fields or total + len(text) > max_chars:
            break
        out.append((r, text))
        total += len(text)
    out.reverse()
    return out, len(story) - len(out)


def player_shots(race_id, user_id, db=None):
    """Shots the player cast or was aimed at, in cast order."""
    with _conn(db) as c:
        rows = c.execute("SELECT round_no, shooter_id, target_id, kind, ts, seq, extra, result,"
                         " prev_target_id FROM shots"
                         " WHERE race_id=? AND (shooter_id=? OR target_id=?) ORDER BY id",
                         (race_id, str(user_id), str(user_id))).fetchall()
    return [dict(r) for r in rows]


CAST_VERB = {"shot": "🎯 fired at", "overload": "💥 overloaded at", "transfuse": "💉 transfused",
             "nuke": "🧨 armed a NUKE"}
# every other heal reads the same way: "🚑 Paramedic → @them"
CAST_VERB.update({k: f"{HEALS[k][0]} {HEALS[k][1]} →" for k in HEALS if k not in CAST_VERB})
RESULT_TAG = {"hit": " → hit", "kill": " → **KILL**", "shielded": " → eaten by a shield",
              "backfire": " → backfired", "reflect": " → reflected back at you",
              "wasted": " → wasted (already gone)", "self": " → hit themselves",
              "transfused": " → done", "healed": " → healed", "late": " → too late", "nuke": " → went off",
              "overkill": " → piled on (overkill)", "revived": " → brought them back"}


def timeline(user_id, shot_rows, log_rows):
    """Per-round story of one player: what they aimed at (their casts, in
    order — intent, even when a shot later backfired or was wasted), then what
    the log says happened to and around them (hits, shields popping, kills,
    the AFK penalty, the storm, drops grabbed, items used). Returns
    [(round_no, [lines])] in round order; rounds with nothing are skipped.
    Shots aimed AT the player are deliberately not listed as casts — they were
    secret at the time and the resolution line already says who hit them."""
    uid = str(user_id)
    by_round = {}
    mine = [sh for sh in shot_rows if sh["shooter_id"] == uid]
    # shots get a number only in rounds where the player fired more than one —
    # "shot 1 / shot 2 (extra)" is the whole point (Paul 9/12), a lone shot reads better bare
    banked_per_round = {}
    for sh in mine:
        if sh["kind"] in BANKED and sh.get("seq"):
            banked_per_round[sh["round_no"]] = banked_per_round.get(sh["round_no"], 0) + 1
    for sh in mine:
        verb = CAST_VERB.get(sh["kind"], f"cast {sh['kind']} at")
        line = verb if sh["kind"] == "nuke" else f"{verb} {m(sh['target_id'])}"
        if sh["kind"] in BANKED and sh.get("seq") and banked_per_round.get(sh["round_no"], 0) > 1:
            tag = f"Shot {sh['seq']}" + (" (extra)" if sh.get("extra") else "")
            line = f"{line[:1]} **{tag}** — {line[2:]}" if line[:1] else f"**{tag}** — {line}"
        elif sh["kind"] in BANKED and sh.get("extra"):
            line += " *(extra shot)*"
        if sh.get("prev_target_id") and sh["prev_target_id"] != sh["target_id"]:
            line += f" (re-aimed from {m(sh['prev_target_id'])})"
        if sh.get("result"):
            line += RESULT_TAG.get(sh["result"], "")
        by_round.setdefault(sh["round_no"], []).append(line)
    for lg in log_rows:
        by_round.setdefault(lg["round_no"], []).append(lg["text"])
    return [(r, by_round[r]) for r in sorted(by_round)]


# ── engine (pure) ─────────────────────────────────────────────────────────────

def m(uid):
    return f"<@{uid}>"


def alive(rows):
    return [p for p in rows if p["alive"]]


def revivable(rows):
    """Eliminated players a revive could still bring back (nobody returns twice)."""
    return [p for p in rows if not p["alive"] and not p.get("revived")]


def is_sudden_death(rows, at):
    return len(alive(rows)) <= at


def join_error(created_at, now, min_days, is_bot=False, bannable=True, mode="real"):
    """Why this account can't enter — or None."""
    if is_bot:
        return "Bots don't race."
    age_days = (now - created_at) / 86400.0
    if age_days < min_days:
        return (f"Accounts need to be at least **{min_days} days** old to enter "
                f"(yours is {age_days:.0f}). The prize attracts alts; this keeps it fair.")
    if mode == "real" and not bannable:
        return ("The bot can't ban you (owner, or your top role is above the bot's), so you "
                "can't be eliminated — hosts like that watch, they don't play.")
    return None


def recommended_lives(n_players):
    """Lives that keep a race of n players interesting (Paul 9/12: "scale
    lives with the amount of people playing — a recommended value, not
    forced"). Every survivor fires once a round, so the more players the more
    incoming fire per round.

    BASE_LIVES is the floor every race starts from (Paul 9/15: "default life
    count should be 5 and scaled from there instead of 3") and the ladder adds
    one life per 4 players on top of it:
      3 → 5 · 8 → 6 · 12 → 7 · 16 → 8 · 20 → 9 · 24 → 10 · 28 → 11 · 32+ → 12
    Soft cap 12 — the old cap of 10 sat only 5 above the old floor of 2, so
    keeping it would have flattened every lobby past 24 onto the same number.
    A host who sets `lives:` explicitly always wins over this (up to 50).
    There is NO cap on players."""
    return max(BASE_LIVES,
               BASE_LIVES - 1 + max(0, n_players) // 4)


BASE_LIVES = 5            # floor + the number a small lobby plays with
MAX_RECOMMENDED_LIVES = None   # no ceiling (Paul 9/16) — kept as a name so nothing importing it breaks


def recommended_sudden_death(n_players):
    """Alive count at which sudden death starts (shields off, half-length
    rounds). A fixed 5 meant a 6-player race was in sudden death from its
    second round (Paul 9/12: "sudden-deathing every single round, that's not
    normal"). About a quarter of the starters, never below 2, never above 5:
      6 → 2 · 8 → 2 · 12 → 3 · 15 → 4 · 20 → 5 · 40 → 10

    No ceiling (Paul 9/16: "no upper limit to it ... the parameters scale the
    same") — a 60-player race reaching its last 15 is the same fraction of the
    field as a 12-player race reaching its last 3."""
    return max(2, -(-max(0, n_players) // 4))


def late_join_lives(max_lives, rounds_played):
    """Lives someone dropping into a race already in progress starts with
    (Paul 9/16: "anyone can join at anytime" — "give them a handicap based on
    how many rounds there are"). One life less per round already finished, and
    never less than one: at 5 lives that's round 2 → 4, round 4 → 2, round 6+
    → 1. Arriving late is allowed, arriving late and fresh is not."""
    return max(1, int(max_lives) - max(0, int(rounds_played)))


def open_round(rows):
    """Hand every survivor this round's shot: ONE, never carried (Paul 9/13:
    "one shot per round, they don't compile"). Every extra shot in the race is
    something you earned that round — a drop, a bounty, an Arsenal. The PURGE
    round (one round per race where everyone got three) was removed on 9/21 at
    Paul's word: "we should definitely remove the purge round." Returns the
    lines to announce, so the caller's shape doesn't depend on the mechanic."""
    for p in alive(rows):
        p["shots"] = 1
    return []


def roll_drop(rng, weights=None):
    w = weights or DROP_WEIGHTS
    kinds = list(w)
    return rng.choices(kinds, weights=[w[k] for k in kinds], k=1)[0]


def drop_count(alive, per_player, round_secs, drop_window):
    """How many regular drops a round gets: alive players × per_player,
    never fewer than one, never so many that two are grabbable at once
    (the 10–85 % window divided by the grab window)."""
    n = int(round(max(0, alive) * per_player))
    cap = max(1, int(0.75 * round_secs // max(1, drop_window)))
    return max(1, min(cap, n))


def drop_schedule(rng, round_secs, alive, per_player, sudden, drop_window=10, dead=0):
    """When this round's drops appear: a list of (seconds_into_round, kind,
    is_super). Regular drops are guaranteed and scale with the players still
    alive (see drop_count). They're spread over the middle of the timer in
    equal slots with jitter inside each — steady traffic instead of a random
    clump — so none lands on the open or the close. Sudden death adds one
    SUPER drop. Down to the last two the table itself changes — see
    drop_weights / DUEL_MULT."""
    n = drop_count(alive, per_player, round_secs, drop_window)
    lo, hi = 0.10 * round_secs, 0.85 * round_secs
    slot = (hi - lo) / n
    out = []
    regular, supers = drop_weights(alive, dead=dead), drop_weights(alive, supers=True, dead=dead)
    for i in range(n):
        out.append((lo + slot * (i + rng.random()), roll_drop(rng, regular), False))
    if sudden:
        out.append((rng.uniform(0.20, 0.70) * round_secs, roll_drop(rng, supers), True))
    out.sort(key=lambda t: t[0])
    return out


def grant_super(p, kind, shot_cap, max_lives):
    """Instant super effects. `nuke` is not handled here — it's a cast, the
    caller records the shot row. Returns the feed line."""
    if kind == "fullheal":
        p["lives"] = max(p["lives"], max_lives)      # never lowers a golden-appled player
        return f"💖 {m(p['user_id'])} grabbed **Full heal** — back to {p['lives']} lives."
    if kind == "goldapple":
        # Paul 9/26: "it should heal more points than the item below it" — the
        # item below it on the card is Full heal (to max), so the apple is a
        # full heal AND the only thing that passes the cap: max + GOLDAPPLE_OVER,
        # never lower. (Until 9/26 it was +2 to CURRENT lives, which at 1 of 5
        # healed less than a Full heal and exactly what a common Medkit does.)
        p["lives"] = max(p["lives"], max_lives + GOLDAPPLE_OVER)
        return (f"🍎 {m(p['user_id'])} bit the **GOLDEN APPLE** — **{p['lives']}** lives, "
                f"{GOLDAPPLE_OVER} above the cap of {max_lives}.")
    if kind == "arsenal":
        p["shots"] = min(shot_cap + 3, p["shots"] + 3)
        return f"🔫 {m(p['user_id'])} grabbed **Arsenal** — three more shots."
    if kind in REVIVES:                       # the two super revives go to the kit, not instant
        p[kind] = p.get(kind, 0) + 1
        emoji, label, _ = REVIVES[kind]
        return f"{emoji} {m(p['user_id'])} grabbed a **{label}** — it's in their kit."
    if kind in HEALS:                         # the super heal needs a target, so it waits in the kit too
        p[kind] = p.get(kind, 0) + 1
        emoji, label, gives, _, over = HEALS[kind]
        tail = (f"put someone else **{gives} above the cap**." if over
                else f"heal someone else {gives}.")
        return f"{emoji} {m(p['user_id'])} grabbed a **{label}** — it's in their kit: {tail}"
    raise ValueError(kind)


def grant(p, kind, shot_cap):
    """Hand a power-up to a player. A second shield turns into a Patch so the
    'one held at a time' rule never wastes a grab (was an extra shot until
    9/12 — shots were coming from everywhere)."""
    if kind == "shield":
        if p["shield"]:
            p["patch"] = p.get("patch", 0) + 1
            return f"🛡️ {m(p['user_id'])} already holds a shield — it became a **Patch**."
        p["shield"] = 1
        return f"🛡️ {m(p['user_id'])} grabbed a **shield**."
    if kind == "reflect":
        if p.get("reflect"):
            p["patch"] = p.get("patch", 0) + 1
            return f"🪞 {m(p['user_id'])} already holds a mirror — it became a **Patch**."
        p["reflect"] = 1
        return f"🪞 {m(p['user_id'])} grabbed a **Reflect**."
    if kind == "shot":
        p["shots"] = min(shot_cap + 1, p["shots"] + 1)
        return f"🔫 {m(p['user_id'])} grabbed an **extra shot**."
    if kind == "overload":
        p["overload"] += 1
        return f"💥 {m(p['user_id'])} grabbed **Overload** — take 1 to deal 2."
    if kind == "transfuse":
        p["transfuse"] += 1
        return f"💉 {m(p['user_id'])} grabbed **Transfuse** — give 1, lose 1."
    if kind in HEALS:
        p[kind] = p.get(kind, 0) + 1
        emoji, label, gives, _, _ = HEALS[kind]
        return f"{emoji} {m(p['user_id'])} grabbed a **{label}** — heal someone else {gives}."
    if kind == "patch":
        p["patch"] = p.get("patch", 0) + 1
        return f"🩹 {m(p['user_id'])} grabbed a **Patch** — heal 1."
    if kind == "medkit":
        p["medkit"] = p.get("medkit", 0) + 1
        return f"🏥 {m(p['user_id'])} grabbed a **Medkit** — heal 2, skip a vote."
    if kind in REVIVES:
        p[kind] = p.get(kind, 0) + 1
        emoji, label, _ = REVIVES[kind]
        return f"{emoji} {m(p['user_id'])} grabbed a **{label}** — bring someone back."
    raise ValueError(kind)


def use_self(p, kind, round_no, max_lives, voted_this_round):
    """Patch / Medkit: instant, on yourself. Returns (ok, text). Mutates p."""
    if p is None or not p["alive"]:
        return False, "You're out of the race."
    if kind == "patch":
        if p.get("patch", 0) <= 0:
            return False, "You don't hold a Patch."
        if p["lives"] >= max_lives:
            return False, "You're at full lives — save it."
        p["patch"] -= 1
        p["lives"] = min(max_lives, p["lives"] + 1)
        return True, f"🩹 Patched up — **{p['lives']}** lives."
    if kind == "medkit":
        if p.get("medkit", 0) <= 0:
            return False, "You don't hold a Medkit."
        if p.get("skip_round") == round_no:
            return False, "You already sat this round out."
        if voted_this_round:
            return False, "You've already voted this round — a Medkit costs the vote, so it's next round or never."
        if p["lives"] >= max_lives:
            return False, "You're at full lives — save it."
        p["medkit"] -= 1
        p["lives"] = min(max_lives, p["lives"] + 2)
        p["skip_round"] = round_no
        return True, f"🏥 Medkit used — **{p['lives']}** lives. No shooting for you this round."
    return False, "That isn't something you use on yourself."


def cast_error(p, target, kind="shot", round_no=None):
    """Why this cast is illegal — or None. `target` may be None for a bad id."""
    if p is None or not p["alive"]:
        return "You're out of the race."
    if kind in REVIVES:
        if p.get(kind, 0) <= 0:
            return f"You don't hold a {REVIVES[kind][1]}."
        if target is None:
            return "That player isn't in this race."
        if target["user_id"] == p["user_id"]:
            return "You're alive — Revive is for someone who's out."
        if target["alive"]:
            return f"{target['name']} is still in the race."
        if target.get("revived"):
            return f"{target['name']} already came back once — nobody returns twice."
        return None
    if kind in ("shot", "overload") and round_no is not None and p.get("skip_round") == round_no:
        return "You used a Medkit this round — no shooting until the next one."
    if target is None or not target["alive"]:
        return "That player isn't in the race (or is already gone)."
    if target["user_id"] == p["user_id"]:
        if kind not in HEALS:
            return "Pick someone else — you can't shoot yourself."
        # the whole point of the ladder: these heal OTHER people. 🩹 Patch and
        # 🏥 Medkit are the ones that heal you.
        return (f"{HEALS[kind][1]} heals someone ELSE — use a 🩹 Patch or a 🏥 Medkit on yourself.")
    if kind == "shot" and p["shots"] <= 0:
        return None  # caller may re-aim; it decides
    if kind == "overload":
        if p["overload"] <= 0:
            return "You don't hold an Overload."
        if p["shots"] <= 0:
            return "Overload rides on a shot and you're out of shots this round."
    if kind in HEALS:
        label, costs = HEALS[kind][1], HEALS[kind][3]
        if p.get(kind, 0) <= 0:
            return f"You don't hold a {label}."
        if costs and p["lives"] <= costs:
            return f"{label} costs a life and you only have one."
    return None


def spend(p, kind):
    """Debit the cast from the player's bank/inventory."""
    if kind == "shot":
        p["shots"] -= 1
    elif kind == "overload":
        p["shots"] -= 1
        p["overload"] -= 1
    elif kind in HEALS:
        p[kind] = p.get(kind, 0) - 1
    elif kind in REVIVES:
        p[kind] = p.get(kind, 0) - 1


def _die(p, round_no, lines, how):
    p["lives"] = 0
    p["alive"] = 0
    p["died_round"] = round_no
    lines.append(how)


def resolve_round(rows, shot_rows, round_no, rng, *, backfire, sudden, storm, msgs,
                  max_lives, shot_cap, afk=True, expire_items=False):
    """Apply every shot cast this round, then the storm, then re-place the
    bounty. Mutates `rows` in place. Returns a dict:
      lines   — announcement lines in resolution order (mentions inline)
      dead    — user_ids eliminated this round, in order
      killers — {victim_id: killer_id} for kill credit (storm/self deaths absent)
      winners — [] while the race goes on; [uid] for a winner; several = draw
      results — {shot_id: outcome} for rows that carry an id (hit|kill|
                shielded|backfire|wasted|self|transfused|late|nuke|revived|
                overkill)
      overkill — {victim_id: excess damage} landed past their last life
      revived — user_ids brought back this round (the cog unbans / re-roles them)
    `sudden` disables shields. `backfire` is the chance an OVERLOAD turns on
    its shooter — plain shots never do (9/21). `msgs` is {user_id: messages
    this round} for the storm. `afk` charges a life to every survivor who cast no shot/overload.
    Shooters who die mid-batch still fire (dead man's shot)."""
    P = {p["user_id"]: p for p in rows}
    lines, dead, killers, results = [], [], {}, {}
    revives = [s for s in shot_rows if s["kind"] in REVIVES]      # resolved last, after the storm
    order = [s for s in shot_rows if s["kind"] not in REVIVES]
    rng.shuffle(order)

    # "A's shot" vs "A's shot 2 (extra)": number a shooter's banked shots only
    # when they fired more than one this round (Paul 9/12: "shot 1 here and
    # shot 2 here")
    fired = {}
    for s in shot_rows:
        if s["kind"] in BANKED and s.get("seq"):
            fired[s["shooter_id"]] = fired.get(s["shooter_id"], 0) + 1

    def shot_name(s):
        sid = s["shooter_id"]
        if s["kind"] in BANKED and s.get("seq") and fired.get(sid, 0) > 1:
            return f"{m(sid)}'s shot {s['seq']}" + (" (extra)" if s.get("extra") else "")
        if s["kind"] in BANKED and s.get("extra"):
            return f"{m(sid)}'s extra shot"
        return f"{m(sid)}'s shot"

    def record(s, res):
        if s.get("id") is not None:
            results[s["id"]] = res

    # who actually played this round: cast a shot/overload/nuke, or sat it
    # out on a Medkit. Everyone else is AFK — the penalty hits them at close,
    # and killing them pays nothing (no free kills on people who aren't here).
    voted = {s["shooter_id"] for s in shot_rows if s["kind"] in ("shot", "overload", "nuke")}

    def is_afk(p):
        # a late entry is not AFK for the round it walked in on, same as a revive
        return (p["user_id"] not in voted and p.get("skip_round") != round_no
                and p.get("joined_round") != round_no)

    rewards = []   # (shooter, victim) — paid AFTER every shot has landed
    # overkill: damage past the victim's last life, and who put it there. A
    # shot that lands on someone already down THIS round is a pile-on, not a
    # waste; the credited killer collects for the lot.
    overkill, pilers = {}, {}

    def pile(victim_id, shooter_id, amount):
        if amount <= 0:
            return
        overkill[victim_id] = overkill.get(victim_id, 0) + amount
        pilers.setdefault(victim_id, []).append(shooter_id)

    def reward_kill(shooter, victim, attribute=True):
        """`attribute=False` banks the kill without naming the killer — a 🪞
        Reflect pays its holder but must leave no trace, not even in the
        victim's elimination DM (Paul 9/20: "not telling people if it has been
        used would be good too")."""
        victim_id = victim["user_id"]
        dead.append(victim_id)
        if shooter is victim:
            return
        if attribute:
            killers[victim_id] = shooter["user_id"]      # attribution only
        if is_afk(victim):
            lines.append(f"   ↳ {m(victim_id)} was AFK — no reward for {m(shooter['user_id'])}.")
            return
        shooter["kills"] += 1
        rewards.append((shooter, victim))

    def pay_overkill(shooter, victim):
        """Paid with the kill, so it inherits every kill rule: an AFK victim
        and a self-kill pay nothing, because neither reaches `rewards`."""
        vid = victim["user_id"]
        over = overkill.get(vid, 0)
        if over <= 0:
            return
        shooter["overkill"] = (shooter.get("overkill", 0) or 0) + over
        helpers = [u for u in dict.fromkeys(pilers.get(vid, [])) if u != shooter["user_id"]]
        won = overkill_reward(over)
        if won is None:
            lines.append(f"   ↳ 💀 **{over} over** on {m(vid)} — one more would have paid.")
            return
        emoji, label, patches = won
        shooter["patch"] = (shooter.get("patch", 0) or 0) + patches
        line = (f"   ↳ {emoji} **{label}** — {m(shooter['user_id'])} put **{over}** past "
                f"{m(vid)}'s last life and takes 🩹 ×{patches}.")
        if helpers:
            line += " Piled on by " + ", ".join(m(u) for u in helpers) + "."
        lines.append(line)

    def pay(shooter, victim):
        if shooter["shield"]:
            shooter["patch"] = shooter.get("patch", 0) + 1
            lines.append(f"   ↳ {m(shooter['user_id'])} gets a Patch for the kill (already shielded).")
        else:
            shooter["shield"] = 1
            lines.append(f"   ↳ {m(shooter['user_id'])} gets a shield for the kill.")
        if victim["bounty"]:
            shooter["shots"] = min(shot_cap + 2, shooter["shots"] + 2)
            lines.append(f"   ↳ 🎯 **Bounty claimed** — {m(shooter['user_id'])} banks two more shots.")

    def hit(shooter, victim, dmg, via, credit=None):
        """One damage application. `via` names the source for the line.
        Returns the outcome word for the shot record.

        `credit` is who BANKS the kill when that differs from who the line
        names — a reflected shot reads as the shooter's own while the mirror's
        holder collects."""
        vid = victim["user_id"]
        if not victim["alive"]:
            return "wasted"
        if victim["shield"] and not sudden:
            victim["shield"] = 0
            lines.append(f"🛡️ {m(vid)}'s **shield** ate {via}.")
            return "shielded"
        earner = credit or shooter
        before = victim["lives"]
        victim["lives"] = max(0, victim["lives"] - dmg)
        if victim["lives"] > 0:
            who = "themselves" if victim is shooter else m(vid)
            lines.append(f"🩸 {via_cap(via)} hit {who} — **{victim['lives']}** "
                         f"{'life' if victim['lives'] == 1 else 'lives'} left.")
            return "self" if victim is shooter else "hit"
        by = "their own shot" if victim is shooter else via
        if victim is not earner:
            pile(vid, earner["user_id"], dmg - before)
        _die(victim, round_no, lines, f"⛔ {m(vid)} was **BANNED** by {by}.")
        reward_kill(earner, victim, attribute=credit is None)
        return "self" if victim is shooter else "kill"

    def via_cap(t):
        return t[0].upper() + t[1:] if t else t

    for s in order:
        shooter, target = P.get(s["shooter_id"]), P.get(s["target_id"])
        if shooter is None or target is None:
            continue
        kind = s["kind"]
        sid, tid = shooter["user_id"], target["user_id"]

        if kind == "nuke":
            lines.append(f"🧨 {m(sid)}'s **NUKE** goes off.")
            for v in rows:
                if v is not shooter and v["alive"]:
                    hit(shooter, v, 1, f"{m(sid)}'s nuke")
            record(s, "nuke")
            continue

        if kind in HEALS:
            emoji, label, gives, costs, over = HEALS[kind]
            if not target["alive"]:
                lines.append(f"{emoji} {m(sid)} tried to heal {m(tid)} — too late, they're gone.")
                record(s, "late")
                continue
            if costs and shooter["lives"] <= 0:
                lines.append(f"{emoji} {m(sid)} had nothing left to give {m(tid)}.")
                record(s, "late")
                continue
            record(s, "transfused" if kind == "transfuse" else "healed")
            shooter["lives"] -= costs
            # never LOWERS anyone (a golden-appled player sits above max), and
            # never goes past this heal's own ceiling — max for the ladder,
            # max + GOLDAPPLE_OVER for 🍯 Ambrosia.
            target["lives"] = max(target["lives"], min(max_lives + over, target["lives"] + gives))
            paid = f", {m(sid)} down to {shooter['lives']}" if costs else ""
            lines.append(f"{emoji} {m(sid)} used a **{label}** on {m(tid)} — "
                         f"{m(tid)} up to {target['lives']}{paid}.")
            if costs and shooter["lives"] == 0 and shooter["alive"]:
                _die(shooter, round_no, lines, f"⛔ {m(sid)} gave their last life away. **Eliminated.**")
                dead.append(sid)
            continue

        dmg = 1
        if kind == "overload":
            dmg = 2
            shooter["lives"] -= 1
            lines.append(f"💥 {m(sid)} **overloaded** — burns a life to fire double.")
            if shooter["lives"] <= 0 and shooter["alive"]:
                _die(shooter, round_no, lines, f"⛔ {m(sid)} burned out on their own overload. **Eliminated.**")
                dead.append(sid)

        victim = target
        name = shot_name(s)
        backfired, credit = False, None
        # Only an OVERLOAD can turn on you (Paul 9/21). A plain shot lands or
        # it doesn't — the risk now belongs to the item that buys the damage.
        if kind == "overload" and rng.random() < backfire:
            victim = shooter
            backfired = True
            lines.append(f"🔥 **Backfire!** {name} at {m(tid)} hit themselves.")
        elif target.get("reflect") and not sudden and target["alive"]:
            # 🪞 The mirror used to hide behind the backfire line — that cover is
            # gone now that only an Overload can backfire, so it says what it is
            # (Paul 9/21: "it's okay to say 'reflected'"). The kill still pays
            # the holder, the same as when it was silent.
            target["reflect"] = 0
            victim, credit = shooter, target
            lines.append(f"🪞 **Reflected!** {m(tid)} mirrored {name} — it hit {m(sid)} instead.")
        if not victim["alive"]:
            if victim is shooter:
                record(s, "backfire")
                continue
            if victim.get("died_round") == round_no:
                pile(tid, sid, dmg)
                # An AFK victim pays nothing — not the kill, not the overkill.
                # The line used to read "Overkill +2" either way, which is why
                # a pile-on on a sleeper looked like it had banked something.
                dud = " — *no reward given, player AFK*" if is_afk(target) else ""
                lines.append(f"💀 {via_cap(name)} lands on {m(tid)} — already down. "
                             f"**Overkill +{dmg}**{dud}.")
                record(s, "overkill")
                continue
            lines.append(f"💨 {via_cap(name)} at {m(tid)} — already gone. Wasted.")
            record(s, "wasted")
            continue
        out = hit(shooter, victim, dmg, name, credit=credit)
        record(s, "backfire" if backfired else ("reflect" if credit else out))

    # Optional use-it-or-lose-it mode (OFF by default — Paul 9/13: power-ups
    # stay until the race ends). If on: expire after every shot has landed and
    # BEFORE kill rewards pay, so a kill's shield survives into the next round.
    if expire_items:
        gone = []
        for p in alive(rows):
            bits = []
            for col in EXPIRING:
                n = p.get(col, 0) or 0
                if n:
                    bits.append(item_face(col)[0] + (f"×{n}" if n > 1 else ""))
                    p[col] = 0
            if bits:
                gone.append(f"{m(p['user_id'])} {' '.join(bits)}")
        if gone:
            lines.append("🧹 Unused power-ups expired: " + " · ".join(gone))
    # kill rewards land only now: a shield earned this round must not eat a
    # shot that was fired this round (resolution is simultaneous)
    for shooter, victim in rewards:
        pay(shooter, victim)
        pay_overkill(shooter, victim)

    # AFK: didn't vote this round = one life gone, shields don't apply
    afk_hit = set()
    if afk:
        for p in alive(rows):
            if not is_afk(p):
                continue
            afk_hit.add(p["user_id"])
            p["lives"] -= 1
            if p["lives"] > 0:
                lines.append(f"😴 {m(p['user_id'])} didn't vote — loses a life. **{p['lives']}** left.")
            else:
                _die(p, round_no, lines, f"😴 {m(p['user_id'])} didn't vote and had nothing left. **Eliminated.**")
                dead.append(p["user_id"])

    # the storm: fewest-chat-messages survivor bleeds one life, shields don't help;
    # it skips anyone the AFK penalty already hit this round
    if storm and len(alive(rows)) >= 3:
        cands = [p for p in alive(rows)
                 if p["user_id"] not in afk_hit and p.get("joined_round") != round_no] or alive(rows)
        low = min(msgs.get(p["user_id"], 0) for p in cands)
        pool = [p for p in cands if msgs.get(p["user_id"], 0) == low]
        v = rng.choice(pool)
        v["lives"] -= 1
        if v["lives"] > 0:
            lines.append(f"🌪️ The **storm** took a life from {m(v['user_id'])} (sent the fewest chat messages this round) — "
                         f"**{v['lives']}** left.")
        else:
            _die(v, round_no, lines, f"🌪️ The **storm** swept {m(v['user_id'])} away (sent the fewest chat messages this round). "
                                     f"**Eliminated.**")
            dead.append(v["user_id"])

    # revives resolve last: the returning player skips this round's shots,
    # AFK penalty and storm entirely (they weren't here for it)
    revived = []
    for s in revives:
        shooter, target = P.get(s["shooter_id"]), P.get(s["target_id"])
        if shooter is None or target is None:
            continue
        if target["alive"] or target.get("revived") or target["user_id"] in revived:
            why = "still in" if target["alive"] else "already came back once"
            lines.append(f"💫 {m(shooter['user_id'])}'s Revive on {m(target['user_id'])} fizzled — {why}.")
            record(s, "late")
            continue
        back = revive_lives(s["kind"], max_lives)
        emoji, label, _ = REVIVES[s["kind"]]
        target.update(alive=1, lives=back, died_round=None, revived=1, shots=0, shield=0,
                      bounty=0, **{col: 0 for col in ITEM_COLS})
        revived.append(target["user_id"])
        record(s, "revived")
        lines.append(f"{emoji} {m(shooter['user_id'])} used a **{label}** — {m(target['user_id'])} is back "
                     f"with **{back}** {'life' if back == 1 else 'lives'}. Nobody returns twice.")
    if revived:
        dead = [d for d in dead if d not in revived]      # died and came back in the same round: never banned
    # bounty: unique top killer with 2+
    survivors = alive(rows)
    for p in rows:
        p["bounty"] = 0
    if survivors:
        top = max(p["kills"] for p in survivors)
        holders = [p for p in survivors if p["kills"] == top]
        if top >= 2 and len(holders) == 1:
            holders[0]["bounty"] = 1
            lines.append(f"🎯 **Bounty** on {m(holders[0]['user_id'])} ({top} kills) — "
                         f"finish them for two extra shots.")

    winners = []
    if len(survivors) == 1:
        winners = [survivors[0]["user_id"]]
    elif not survivors:
        winners = [p["user_id"] for p in rows if p.get("died_round") == round_no]

    # de-dupe dead while keeping order (a player can't die twice, but be safe)
    seen, uniq = set(), []
    for d in dead:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return {"lines": lines, "dead": uniq, "killers": killers, "winners": winners, "results": results,
            "revived": revived, "overkill": overkill}


def standings(rows):
    """Two lists for the board: survivors (kills desc, lives desc) and the
    fallen (latest death first). Shields are deliberately NOT included."""
    live = sorted(alive(rows), key=lambda p: (-p["kills"], -p["lives"], p["name"].lower()))
    fallen = sorted([p for p in rows if not p["alive"]],
                    key=lambda p: (-(p["died_round"] or 0), p["name"].lower()))
    return live, fallen


# ── the schedule: races start on the clock, not when the lobby fills ─────────
# Paul 9/16: "instead of starting when everyone joins, it should be scheduled 4
# times a day … 2am, 8am, 2pm and 8pm EST", and "if there's only 2 people it
# should delay till the next round, minimum 3".
#
# Everything below is pure arithmetic over a wall clock — the cog owns the
# Discord side. Times are LOCAL to `tz` (America/New_York, so a slot keeps its
# wall-clock time on Paul's clock through a DST change rather than sliding an
# hour).

# Paul 9/18: "let's make games every 4 hours instead of 6" — six a day, on the
# hour, keeping the 08:00 and 20:00 slots the old four already had.
SCHEDULE_SLOTS = ("00:00", "04:00", "08:00", "12:00", "16:00", "20:00")   # every 4 hours
SCHEDULE_TZ = "America/New_York"
FIRE_GRACE = 600        # a slot missed to downtime still fires if we're back within 10 min
WARN_OFFSETS = (900, 60)   # ping the lobby at T-15 min and T-1 min


def _zone(tz=None):
    from zoneinfo import ZoneInfo
    return ZoneInfo(tz or SCHEDULE_TZ)


def parse_slots(raw):
    """'2:00, 8:00,14:00 20:00' | ['02:00', …] -> ('02:00','08:00',…), sorted,
    de-duped. Raises ValueError on anything that isn't HH:MM."""
    if not raw:
        return tuple(SCHEDULE_SLOTS)
    parts = raw.replace(",", " ").split() if isinstance(raw, str) else list(raw)
    out = []
    for p in parts:
        hh, _, mm = str(p).strip().partition(":")
        h, m = int(hh), int(mm or 0)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"{p!r} is not a time of day")
        out.append(f"{h:02d}:{m:02d}")
    if not out:
        raise ValueError("no times given")
    return tuple(sorted(set(out)))


def _local_ts(day, hhmm, zone):
    """The unix time of `hhmm` on local date `day`. On the spring-forward day
    the 2:00 AM slot does not exist on the wall clock; zoneinfo maps it to the
    same instant as 3:00 AM, which is what we want — this just round-trips so
    the skip is deliberate rather than accidental."""
    from datetime import datetime, timedelta, timezone
    h, m = (int(x) for x in hhmm.split(":"))
    naive = datetime(day.year, day.month, day.day, h, m)
    dt = naive.replace(tzinfo=zone)
    back = dt.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if back != naive:                       # a gap: the clock jumped over this time
        dt = (naive + timedelta(hours=1)).replace(tzinfo=zone)
    return dt.timestamp()


def _day_slots(day, slots, zone):
    return sorted(_local_ts(day, s, zone) for s in slots)


def next_slot(now=None, slots=SCHEDULE_SLOTS, tz=None):
    """Unix time of the next slot strictly after `now`."""
    from datetime import datetime, timedelta
    now = now if now is not None else time.time()
    zone = _zone(tz)
    today = datetime.fromtimestamp(now, zone).date()
    for off in (0, 1, 2):
        for ts in _day_slots(today + timedelta(days=off), slots, zone):
            if ts > now:
                return ts
    raise ValueError("no slot found")       # unreachable with a non-empty slot list


def next_of_each(now=None, slots=SCHEDULE_SLOTS, tz=None):
    """The next occurrence of EVERY slot, in slot order — one timestamp per
    start time. The card renders them as Discord <t:...:t> stamps so a player
    reads the schedule on their own clock instead of decoding "EDT"
    (Paul 9/16: "use timestamps for this ... instead of EDT")."""
    from datetime import datetime, timedelta
    now = now if now is not None else time.time()
    zone = _zone(tz)
    today = datetime.fromtimestamp(now, zone).date()
    out = []
    for s in slots:
        ts = _local_ts(today, s, zone)
        if ts <= now:
            ts = _local_ts(today + timedelta(days=1), s, zone)
        out.append(ts)
    return out


def previous_slot(now=None, slots=SCHEDULE_SLOTS, tz=None):
    """Unix time of the most recent slot at or before `now`."""
    from datetime import datetime, timedelta
    now = now if now is not None else time.time()
    zone = _zone(tz)
    today = datetime.fromtimestamp(now, zone).date()
    best = None
    for off in (0, -1, -2):
        for ts in _day_slots(today + timedelta(days=off), slots, zone):
            if ts <= now and (best is None or ts > best):
                best = ts
        if best is not None:
            return best
    return best


def due_slot(now, last_fired=None, slots=SCHEDULE_SLOTS, tz=None, grace=FIRE_GRACE):
    """The slot that is owed right now, or None. `last_fired` is the slot we
    last acted on (fired OR rolled over), so a restart can't double-fire and a
    slot that passed while the bot was down is dropped once the grace is gone."""
    prev = previous_slot(now, slots, tz)
    if prev is None:
        return None
    if now - prev > grace:
        return None
    if last_fired and float(last_fired) >= prev:
        return None
    return prev


def warning_due(now, slot_ts, sent=(), offsets=WARN_OFFSETS):
    """The largest un-sent warning offset that is due (T-15, then T-1), or None.
    `sent` is the offsets already announced for THIS slot."""
    left = slot_ts - now
    if left < 0:
        return None
    for off in sorted(offsets, reverse=True):
        if left <= off and off not in set(sent):
            return off
    return None


def schedule_line(slot_ts, n_joined, need):
    """The one-line status the lobby card and /race schedule both print."""
    short = max(0, need - n_joined)
    who = (f"**{n_joined}/{need}** in — needs **{short}** more or it waits for the next one"
           if short else f"**{n_joined}/{need}** in — it's on")
    return f"🕗 Next race <t:{int(slot_ts)}:F> (<t:{int(slot_ts)}:R>) · {who}"
