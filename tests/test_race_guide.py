"""The /powerup reference card — cogs/ban_race.reference_embed and its guards.

This card is what a player gets when there's no kit to show (no race, a lobby,
someone else's round, or they're out), so it has to render in every one of
those states and it must never 400. An over-long embed is a 400 on the whole
message, and a 400 inside a button handler shows the player "didn't respond in
time" with nothing to act on — which is how the 9/13 revive tiers killed the
Power-ups button. The guide grows every time a power-up is added, so the size
guard is tested with a deliberately bloated table too.

Run on any box with discord.py importable:
    /opt/peepos-reclaimer/venv/bin/python tests/test_race_guide.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.ban_race as br          # noqa: E402
import utils.ban_race as engine     # noqa: E402

_fails = []
_total = 0


def check(name, got, want):
    global _total
    _total += 1
    if got != want:
        _fails.append(f"{name}: got {got!r}, want {want!r}")


def ok(name, cond):
    check(name, bool(cond), True)


def names(e):
    return [f.name for f in e.fields]


def text(e):
    return "\n".join([e.description or ""] + [f"{f.name}\n{f.value}" for f in e.fields])


LONG_HEADER = ("**The lobby is open — the race hasn't started yet.** Hit **Join** on the card in "
               "#last-man-standing-arena. Next one is **<t:1758400000:R>** in "
               "#last-man-standing-arena.")

# ── it renders in every state, and always inside Discord's caps ──────────
for label, settings, header in (
        ("defaults", None, None),
        ("no race", None, "**No race is running right now.** Here's the whole game anyway:"),
        ("lobby, long header", dict(engine.DEFAULTS), LONG_HEADER),
        ("real mode, scaled race",
         dict(engine.DEFAULTS, mode="real", lives=19, sudden_death_at=15, sudden_auto=True),
         LONG_HEADER),
):
    e = br.reference_embed(settings, header=header)
    check(f"{label}: within the 6000 cap", br.embed_len(e) <= br.EMBED_TOTAL_LIMIT, True)
    check(f"{label}: at most 25 fields", len(e.fields) <= 25, True)
    check(f"{label}: no field over 1024", max(len(f.value) for f in e.fields) <= br.FIELD_LIMIT, True)
    ok(f"{label}: explains shots", any(n.startswith("Shots, Overload") for n in names(e)))
    ok(f"{label}: covers overkill", any(n.startswith("Overkill") for n in names(e)))
    # ORDER, top to bottom (Paul 9/20: "this is so out of order"): what a drop
    # is and what a rarity means, then the items group by group, and only then
    # the shot mechanics they ride on.
    order = names(e)
    check(f"{label}: primer first", order[0], "Drops & rarity")
    first_group = min(order.index(f"{g} {t}") for _k, g, t, _n, _i in engine.grouped_items())
    ok(f"{label}: items before the shot rules",
       first_group < order.index("Shots, Overload & backfire"))
    ok(f"{label}: shot rules before overkill",
       order.index("Shots, Overload & backfire") < order.index("Overkill"))
    # It is the POWER-UP card now, not the rulebook (Paul 9/20) — the race
    # rules live on the lobby card, and a wiki is coming to the dashboard.
    ok(f"{label}: no race tutorial", "How it works" not in names(e))

# ── the three questions this card exists to answer ──────────────────────
e = br.reference_embed(None)
body = text(e)
ok("says an Overload rides on a shot", "rides on" in body and "Overload" in body)
ok("says where to arm an Overload", "Power-ups" in body)
# 9/21: backfire belongs to the Overload only — the card has to say so, or
# players keep asking why their plain shot "might" turn on them
ok("says backfire is an Overload risk only", "Overload risk only" in body)
ok("says a plain shot never backfires", "plain shot never turns on you" in body)
ok("says a backfire redirects the Overload onto you", "redirects the Overload onto YOU" in body)
ok("says the target takes nothing", "target takes nothing" in body)
ok("says a dead shooter still fires", "still fires" in body)
ok("says heals are not your vote", "not your vote" in body)

# the backfire chance is read from the race, not hard-coded
check("backfire % from defaults", "Backfire (10%)" in text(br.reference_embed(None)), True)
check("backfire % from the race's own setting",
      "Backfire (25%)" in text(br.reference_embed(dict(engine.DEFAULTS, backfire=0.25))), True)

# ── grouped by type, every item listed exactly once ─────────────────────
e = br.reference_embed(None)
body = text(e)
for kind in set(engine.POWERUPS) | set(engine.SUPER):
    ok(f"{kind}: has a group", kind in engine.ITEM_GROUP)
    ok(f"{kind}: has a short blurb", kind in engine.BRIEF)
    ok(f"{kind}: on the card", engine.item_face(kind)[1] in body)
for _k, g_emoji, title, _note, items in engine.grouped_items():
    ok(f"group {title}: has items", bool(items))
    # the group name IS a field name — Discord bolds it, which is the label
    # Paul asked for ("it should just be a label shooting, then each item")
    ok(f"group {title}: is its own field", f"{g_emoji} {title}" in names(e))
ok("says what common means", "**Common**" in body and "**Super**" in body)
ok("marks the super-rares", "🌟🌟" in body)
ok("ally heals say it once, in the header", body.count("still owe a shot") <= 2)

# ── the size guard: a bloated table yields a short line, never a 400 ────
_real = engine.BRIEF
try:
    engine.BRIEF = {k: (v[0] + " " + "x" * 300, v[1] + " " + "y" * 300) for k, v in _real.items()}
    e = br.reference_embed(None, header=LONG_HEADER)
    check("bloated: still within the cap", br.embed_len(e) <= br.EMBED_TOTAL_LIMIT, True)
    ok("bloated: still says what overkill is", any(n.startswith("Overkill") for n in names(e)))
    ok("bloated: still opens with the primer", names(e)[0] == "Drops & rarity")
    ok("bloated: points at the drops for the detail", "grab one and read it" in text(e))
finally:
    engine.BRIEF = _real

# ── one source for the rules: the lobby card and the guide share it ─────
race = {"settings": dict(engine.DEFAULTS), "status": "lobby"}
lobby = br.lobby_embed(race, [], "Torvex")
how = br.how_it_works(race["settings"])
check("lobby card uses how_it_works", how in [f.value for f in lobby.fields], True)

print(f"{_total - len(_fails)}/{_total} race-guide checks passed")
for f in _fails:
    print("  FAIL", f)
sys.exit(1 if _fails else 0)
