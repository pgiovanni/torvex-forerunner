"""Last to survive engine (utils/ban_race.py).

The rules players are told are one sentence; these tests pin the mechanics
that sentence hides, because every one of them decides who gets banned:

  * a shot costs one life, the ban lands at zero, and only then;
  * shots resolve simultaneously — a shooter who dies this round still fires;
  * a shield eats one whole shot and is gone; none work in sudden death;
  * backfire hits the shooter; Overload burns the shooter to deal 2;
    Transfuse moves a life across;
  * a kill pays a shield (or a shot when one is held), the bounty pays two —
    but killing someone who was AFK that round pays nothing at all;
  * the storm bleeds the quietest survivor and ignores shields;
  * the winner is the last one alive; nobody alive = draw among the last dead;
  * the store keeps one race per guild and never crosses guilds.
"""
import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="banrace")
os.environ["TORVEX_BANRACE_DB"] = os.path.join(_TMP, "ban_race.db")

from utils import ban_race as E  # noqa: E402


def P(uid, lives=3, **kw):
    d = dict(user_id=str(uid), name=f"p{uid}", lives=lives, shots=1, shield=0, overload=0,
             transfuse=0, kills=0, bounty=0, alive=1, died_round=None, banned=0,
             patch=0, medkit=0, skip_round=None, revived=0,
             revive_small=0, revive_medium=0, revive_full=0, revive_extra=0,
             bloodbag=0, paramedic=0, fieldhosp=0, ambrosia=0, reflect=0)
    d.update(kw)
    return d


def S(shooter, target, kind="shot"):
    return {"shooter_id": str(shooter), "target_id": str(target), "kind": kind}


class NoBackfire(random.Random):
    """random() always 0.99 so backfire never triggers; shuffle keeps order."""
    def random(self):
        return 0.99

    def shuffle(self, x):
        pass


class AlwaysBackfire(NoBackfire):
    def random(self):
        return 0.0


def resolve(rows, shots, round_no=1, rng=None, sudden=False, storm=False, msgs=None, afk=False,
            rows_extra=None, **kw):
    if rows_extra is not None:
        rows = rows + [rows_extra]
    args = dict(backfire=0.10, sudden=sudden, storm=storm, msgs=msgs or {}, max_lives=3, shot_cap=3,
                afk=afk)
    args.update(kw)
    return E.resolve_round(rows, shots, round_no, rng or NoBackfire(), **args)


class Damage(unittest.TestCase):
    def test_shot_takes_one_life_ban_only_at_zero(self):
        a, b = P(1), P(2)
        r = resolve([a, b], [S(1, 2)])
        self.assertEqual(b["lives"], 2)
        self.assertTrue(b["alive"])
        self.assertEqual(r["dead"], [])
        self.assertEqual(r["winners"], [])

    def test_focus_fire_kills_in_one_round_and_credits_the_finisher(self):
        a, b, c = P(1), P(2), P(3, lives=2)
        r = resolve([a, b, c], [S(1, 3), S(2, 3), S(3, 1)])   # c votes, so the kill pays
        self.assertFalse(c["alive"])
        self.assertEqual(c["died_round"], 1)
        self.assertEqual(r["dead"], ["3"])
        self.assertEqual(r["killers"]["3"], "2")     # second shot in order landed the kill
        self.assertEqual(b["kills"], 1)
        self.assertEqual(a["kills"], 0)

    def test_dead_mans_shot_still_fires(self):
        a, b = P(1, lives=1), P(2, lives=1)
        # a is killed by b's shot first in order, yet a's shot still lands
        r = resolve([a, b], [S(2, 1), S(1, 2)])
        self.assertFalse(a["alive"])
        self.assertFalse(b["alive"])
        self.assertEqual(sorted(r["winners"]), ["1", "2"])   # mutual destruction = draw

    def test_shot_at_a_corpse_from_an_earlier_round_is_wasted(self):
        # same-round corpses are overkill now (see Overkill below); a body from
        # a previous round is still just a wasted shot
        a, c = P(1), P(3)
        b = P(2, lives=0, alive=0, died_round=1)
        r = resolve([a, c], [S(1, 2), S(3, 2)], rows_extra=b, round_no=2)
        self.assertEqual(c["kills"], 0)
        self.assertTrue(any("Wasted" in ln for ln in r["lines"]))

    def test_last_alive_wins(self):
        a, b = P(1), P(2, lives=1)
        r = resolve([a, b], [S(1, 2)])
        self.assertEqual(r["winners"], ["1"])


class Shields(unittest.TestCase):
    def test_shield_eats_whole_shot_once(self):
        a, b = P(1), P(2, shield=1)
        resolve([a, b], [S(1, 2)])
        self.assertEqual(b["lives"], 3)
        self.assertEqual(b["shield"], 0)
        resolve([a, b], [S(1, 2)], round_no=2)
        self.assertEqual(b["lives"], 2)

    def test_shield_eats_overload_too(self):
        a, b = P(1), P(2, shield=1)
        resolve([a, b], [S(1, 2, "overload")])
        self.assertEqual(b["lives"], 3)
        self.assertEqual(a["lives"], 2)       # the shooter still paid

    def test_no_shields_in_sudden_death(self):
        a, b = P(1), P(2, shield=1)
        resolve([a, b], [S(1, 2)], sudden=True, expire_items=False)
        self.assertEqual(b["lives"], 2)
        self.assertEqual(b["shield"], 1)      # not consumed either (expiry off to isolate that)

    def test_second_shield_grant_becomes_a_patch(self):
        # was an extra shot until 9/12 — shots were arriving from everywhere
        p = P(1, shield=1, shots=1)
        line = E.grant(p, "shield", shot_cap=3)
        self.assertEqual((p["shield"], p["shots"], p["patch"]), (1, 1, 1))
        self.assertIn("Patch", line)


class PowerUps(unittest.TestCase):
    def test_backfire_hits_the_shooter(self):
        # Paul 9/21: "the backfire thing should go away except for overload" —
        # so the roll only ever happens on an Overload, which costs its shooter
        # 3 when it comes back: 1 to fire, then its own 2.
        a, b = P(1, overload=1), P(2)
        resolve([a, b], [S(1, 2, "overload")], rng=AlwaysBackfire())
        self.assertEqual(a["lives"], 0)
        self.assertEqual(b["lives"], 3)

    def test_a_plain_shot_never_backfires(self):
        a, b = P(1), P(2)
        resolve([a, b], [S(1, 2)], rng=AlwaysBackfire())
        self.assertEqual((a["lives"], b["lives"]), (3, 2))     # it just lands

    def test_backfire_self_kill_gives_no_credit(self):
        a, b = P(1, lives=1, overload=1), P(2)
        r = resolve([a, b], [S(1, 2, "overload")], rng=AlwaysBackfire())
        self.assertFalse(a["alive"])
        self.assertEqual(a["kills"], 0)
        self.assertNotIn("1", r["killers"])
        self.assertEqual(r["winners"], ["2"])

    def test_overload_burns_one_deals_two(self):
        a, b = P(1), P(2)
        resolve([a, b], [S(1, 2, "overload")])
        self.assertEqual(a["lives"], 2)
        self.assertEqual(b["lives"], 1)

    def test_overload_on_one_life_kills_the_shooter_but_still_fires(self):
        a, b = P(1, lives=1), P(2)
        r = resolve([a, b], [S(1, 2, "overload")])
        self.assertFalse(a["alive"])
        self.assertEqual(b["lives"], 1)
        self.assertIn("1", r["dead"])

    def test_transfuse_moves_a_life_capped_at_max(self):
        a, b = P(1), P(2, lives=1)
        resolve([a, b], [S(1, 2, "transfuse")])
        self.assertEqual(a["lives"], 2)
        self.assertEqual(b["lives"], 2)
        c = P(3)
        resolve([a, c], [S(1, 3, "transfuse")], round_no=2)
        self.assertEqual(c["lives"], 3)           # already full, capped
        self.assertEqual(a["lives"], 1)           # still paid

    def test_transfuse_refused_on_last_life_at_cast_time(self):
        a, b = P(1, lives=1, transfuse=1), P(2)
        self.assertIn("only have one", E.cast_error(a, b, "transfuse"))

    def test_cast_errors(self):
        a, b, dead = P(1, shots=1), P(2), P(3, alive=0, lives=0)
        self.assertIsNone(E.cast_error(a, b, "shot"))
        self.assertIn("yourself", E.cast_error(a, a, "shot"))
        self.assertIn("gone", E.cast_error(a, dead, "shot"))
        self.assertIn("out of the race", E.cast_error(dead, b, "shot"))
        self.assertIn("Overload", E.cast_error(a, b, "overload"))
        a["overload"] = 1
        a["shots"] = 0
        self.assertIn("out of shots", E.cast_error(a, b, "overload"))
        self.assertIsNone(E.cast_error(a, b, "shot"))   # None: caller may re-aim

    def test_spend(self):
        p = P(1, shots=2, overload=1, transfuse=1)
        E.spend(p, "shot"); self.assertEqual(p["shots"], 1)
        E.spend(p, "overload"); self.assertEqual((p["shots"], p["overload"]), (0, 0))
        E.spend(p, "transfuse"); self.assertEqual(p["transfuse"], 0)

    def test_roll_drop_only_known_kinds(self):
        rng = random.Random(7)
        for _ in range(50):
            self.assertIn(E.roll_drop(rng), E.POWERUPS)


class Heals(unittest.TestCase):
    def test_patch_heals_one_capped(self):
        p = P(1, lives=1, patch=3)
        ok, _ = E.use_self(p, "patch", 1, 3, voted_this_round=True)
        self.assertTrue(ok)
        self.assertEqual((p["lives"], p["patch"]), (2, 2))
        E.use_self(p, "patch", 1, 3, False)
        ok, text = E.use_self(p, "patch", 1, 3, False)
        self.assertFalse(ok)                         # full lives: refused, item kept
        self.assertIn("full", text)
        self.assertEqual((p["lives"], p["patch"]), (3, 1))

    def test_medkit_heals_two_and_forfeits_the_vote(self):
        p = P(1, lives=1, medkit=1)
        ok, text = E.use_self(p, "medkit", 4, 3, voted_this_round=True)
        self.assertFalse(ok)                         # already voted: no discount
        self.assertIn("already voted", text)
        ok, _ = E.use_self(p, "medkit", 4, 3, voted_this_round=False)
        self.assertTrue(ok)
        self.assertEqual((p["lives"], p["medkit"], p["skip_round"]), (3, 0, 4))
        # can't shoot this round, may shoot next
        self.assertIn("Medkit", E.cast_error(p, P(2), "shot", round_no=4))
        self.assertIsNone(E.cast_error(p, P(2), "shot", round_no=5))
        # and the AFK penalty leaves them alone this round
        q = P(2)
        resolve([p, q], [S(2, 1)], round_no=4, afk=True)
        self.assertEqual(p["lives"], 2)              # only q's shot landed

    def test_heal_drops_grant_items(self):
        p = P(1)
        E.grant(p, "patch", 3); E.grant(p, "medkit", 3)
        self.assertEqual((p["patch"], p["medkit"]), (1, 1))


class Reflect(unittest.TestCase):
    """🪞 Reflect (Paul 9/20: "there needs to be more defensive items, a
    reflect item would help"). A shot fired at the holder lands on the shooter
    instead. It used to pose as a backfire so nobody could tell the holder had
    one — 9/21 that cover went (backfire is Overload-only now) and Paul said
    "it's okay to say 'reflected'", so the feed names the mirror."""

    def test_a_shot_bounces_back_at_the_shooter(self):
        a, b = P(1, lives=3), P(2, lives=3, reflect=1)
        out = resolve([a, b], [S(1, 2)], afk=False)
        self.assertEqual(a["lives"], 2)                        # the shooter ate it
        self.assertEqual(b["lives"], 3)                        # the holder took nothing
        self.assertEqual(b["reflect"], 0)                      # and spent the mirror

    def test_the_line_says_it_was_reflected_and_who_it_hit(self):
        a, b = P(1, lives=3), P(2, lives=3, reflect=1)
        line = next(ln for ln in resolve([a, b], [S(1, 2)], afk=False)["lines"] if "🪞" in ln)
        self.assertIn("Reflected", line)
        self.assertIn(b["user_id"], line)                      # the mirror's holder
        self.assertIn(a["user_id"], line)                      # and who ate it
        self.assertNotIn("Backfire", line)                     # it stopped posing as one
        self.assertEqual(E.RESULT_TAG["reflect"], " → reflected back at you")

    def test_a_reflected_kill_pays_the_holder(self):
        a, b = P(1, lives=1), P(2, lives=3, reflect=1)
        out = resolve([a, b], [S(1, 2)], afk=False)
        self.assertFalse(a["alive"])
        self.assertEqual(b["kills"], 1)                        # the holder banks it
        self.assertNotIn(a["user_id"], out["killers"])         # death still reads self-inflicted
        self.assertEqual(b["shield"], 1)                       # and takes the kill pay
        self.assertIn("their own shot", " ".join(out["lines"]))

    def test_it_is_off_in_sudden_death_like_a_shield(self):
        a, b = P(1, lives=3), P(2, lives=3, reflect=1)
        resolve([a, b], [S(1, 2)], afk=False, sudden=True)
        self.assertEqual((a["lives"], b["lives"]), (3, 2))     # straight through
        self.assertEqual(b["reflect"], 1)                      # and not spent

    def test_a_second_mirror_becomes_a_patch(self):
        p = P(1)
        E.grant(p, "reflect", 3)
        self.assertEqual(p["reflect"], 1)
        E.grant(p, "reflect", 3)
        self.assertEqual((p["reflect"], p["patch"]), (1, 1))

    def test_it_saves_and_it_is_rarer_than_a_shield(self):
        self.assertIn("reflect", E.ITEM_COLS)                  # or it would never persist
        self.assertEqual(E.POWERUP_TIER["reflect"], "rare")
        self.assertLess(E.DROP_WEIGHTS["reflect"], E.DROP_WEIGHTS["shield"])


class HealOthers(unittest.TestCase):
    """The heal-an-ally ladder (Paul 9/18): transfuse 1 at the cost of 1,
    then ever better drops — uncommon 1 free, rare 2, super 3."""

    def test_the_ladder_climbs_with_the_drop_tier(self):
        self.assertEqual([E.heal_amount(k)[0]
                          for k in ("transfuse", "bloodbag", "paramedic", "fieldhosp", "ambrosia")],
                         [1, 1, 2, 3, 2])
        # only the common one costs the giver anything
        self.assertEqual([E.heal_amount(k)[1] for k in E.HEALS], [1, 0, 0, 0, 0])
        self.assertEqual(E.POWERUP_TIER["transfuse"], "common")
        self.assertEqual(E.POWERUP_TIER["bloodbag"], "uncommon")
        self.assertEqual(E.POWERUP_TIER["paramedic"], "rare")
        for kind in ("fieldhosp", "ambrosia"):                 # supers: sudden death only
            self.assertNotIn(kind, E.POWERUP_TIER)
            self.assertIn(kind, E.SUPER_WEIGHTS)
        # every rung says out loud that it heals someone else, not you
        for kind in E.HEALS:
            self.assertIn("ANOTHER player", E.blurb(kind))

    def test_free_heals_cost_the_giver_nothing_and_cap_at_max(self):
        for kind, gives in (("bloodbag", 1), ("paramedic", 2), ("fieldhosp", 3)):
            a, b = P(1, lives=3, **{kind: 1}), P(2, lives=1)
            resolve([a, b], [S(1, 2, kind)], max_lives=3)
            self.assertEqual(a["lives"], 3)                    # the giver never pays
            self.assertEqual(b["lives"], min(3, 1 + gives))    # capped at max

    def test_transfuse_still_pays_a_life(self):
        a, b = P(1, lives=3, transfuse=1), P(2, lives=1)
        out = resolve([a, b], [S(1, 2, "transfuse")], max_lives=3)
        self.assertEqual((a["lives"], b["lives"]), (2, 2))
        self.assertIn("down to 2", " ".join(out["lines"]))

    def test_a_heal_is_never_used_on_yourself(self):
        for kind in E.HEALS:
            p = P(1, **{kind: 1})
            err = E.cast_error(p, p, kind)
            self.assertIn("someone ELSE", err)
            self.assertIn("Patch", err)                        # points at the self-heals instead

    def test_a_heal_you_do_not_hold_is_refused(self):
        for kind, (_, label, _, _, _) in E.HEALS.items():
            self.assertIn(label, E.cast_error(P(1), P(2), kind))
        self.assertIsNone(E.cast_error(P(1, paramedic=1), P(2), "paramedic"))
        # ... and the lone costed rung is still refused on your last life
        self.assertIn("only have one", E.cast_error(P(1, lives=1, transfuse=1), P(2), "transfuse"))
        self.assertIsNone(E.cast_error(P(1, lives=1, bloodbag=1), P(2), "bloodbag"))

    def test_a_heal_lands_at_close_so_it_can_arrive_too_late(self):
        a, b, c = P(1, paramedic=1), P(2, lives=1), P(3)
        out = resolve([a, b, c], [S(3, 2), S(1, 2, "paramedic")], afk=False)
        self.assertFalse(b["alive"])                           # c's shot got there first
        self.assertIn("too late", " ".join(out["lines"]))
        self.assertEqual(a["paramedic"], 1)                    # spend() happens at cast, not here

    def test_healing_is_not_a_vote(self):
        a, b = P(1, lives=3, fieldhosp=1), P(2, lives=1)
        resolve([a, b], [S(1, 2, "fieldhosp")], afk=True, max_lives=3)
        self.assertEqual(a["lives"], 2)                        # healed an ally, still AFK
        self.assertEqual(b["lives"], 2)                        # 1 → 3 (capped), then their own AFK life

    def test_ambrosia_is_the_only_heal_that_passes_the_cap(self):
        """Paul 9/20: "a heal another person item that goes above max. golden
        apple equivalent." Same ceiling as the apple (max + GOLDAPPLE_OVER),
        aimed at someone else, and no other rung may ever exceed max."""
        a, b = P(1, lives=3, ambrosia=1), P(2, lives=3)
        resolve([a, b], [S(1, 2, "ambrosia")], max_lives=3)
        self.assertEqual(b["lives"], 3 + 2)                    # straight past the cap
        self.assertEqual(a["lives"], 3)                        # free to the giver
        # it stops at the apple's ceiling, never higher
        c, d = P(1, lives=3, ambrosia=1), P(2, lives=3 + E.GOLDAPPLE_OVER)
        resolve([c, d], [S(1, 2, "ambrosia")], max_lives=3)
        self.assertEqual(d["lives"], 3 + E.GOLDAPPLE_OVER)
        # and a topped-up target is never LOWERED by a later ladder heal
        e, f = P(1, lives=3, paramedic=1), P(2, lives=3 + E.GOLDAPPLE_OVER)
        resolve([e, f], [S(1, 2, "paramedic")], max_lives=3)
        self.assertEqual(f["lives"], 3 + E.GOLDAPPLE_OVER)
        # every other rung stops at max
        for kind in ("transfuse", "bloodbag", "paramedic", "fieldhosp"):
            self.assertEqual(E.HEALS[kind][4], 0)
        self.assertEqual(E.HEALS["ambrosia"][4], E.GOLDAPPLE_OVER)

    def test_ambrosia_follows_every_other_ally_heal_rule(self):
        # lands at round close, so it can be too late
        a, b, c = P(1, ambrosia=1), P(2, lives=1), P(3)
        out = resolve([a, b, c], [S(3, 2), S(1, 2, "ambrosia")], afk=False)
        self.assertFalse(b["alive"])
        self.assertIn("too late", " ".join(out["lines"]))
        # never on yourself, and never your vote
        p = P(1, lives=3, ambrosia=1)
        self.assertIn("someone ELSE", E.cast_error(p, p, "ambrosia"))
        g, h = P(1, lives=3, ambrosia=1), P(2, lives=1)
        resolve([g, h], [S(1, 2, "ambrosia")], afk=True, max_lives=3)
        self.assertEqual(g["lives"], 2)                        # healed an ally, still AFK

    def test_ambrosia_is_granted_to_the_kit_like_the_other_supers(self):
        p = P(1)
        line = E.grant_super(p, "ambrosia", 3, 3)
        self.assertEqual(p["ambrosia"], 1)
        self.assertIn("kit", line)
        self.assertIn("above the cap", line)
        self.assertIn("ambrosia", E.ITEM_COLS)                  # or it would never save

    def test_drops_hand_them_out_and_the_super_waits_in_the_kit(self):
        p = P(1)
        for kind in ("bloodbag", "paramedic"):
            line = E.grant(p, kind, 3)
            self.assertEqual(p[kind], 1)
            self.assertIn("heal someone else", line)
        for kind in ("fieldhosp", "ambrosia"):                 # both supers wait in the kit
            line = E.grant_super(p, kind, 3, 3)
            self.assertEqual(p[kind], 1)
            self.assertIn("kit", line)
        E.grant(p, "transfuse", 3)
        for kind in E.HEALS:                                   # casting one spends it
            E.spend(p, kind)
        self.assertEqual([p[k] for k in E.HEALS], [0, 0, 0, 0, 0])


class Drops(unittest.TestCase):
    def test_drops_scale_with_alive_players_never_zero_never_overlapping(self):
        # 0.5 per player: 10 alive -> 5, 3 alive -> 2 (round), 2 alive -> 1, 1 alive -> still 1
        self.assertEqual(E.drop_count(10, 0.5, 180, 10), 5)
        self.assertEqual(E.drop_count(3, 0.5, 180, 10), 2)
        self.assertEqual(E.drop_count(2, 0.5, 180, 10), 1)
        self.assertEqual(E.drop_count(1, 0.5, 180, 10), 1)
        self.assertEqual(E.drop_count(0, 0.5, 180, 10), 1)
        # capped so two drops are never grabbable at once: 30 s round, 10 s window -> 2 max
        self.assertEqual(E.drop_count(100, 0.5, 30, 10), 2)
        self.assertEqual(E.drop_count(100, 0.5, 180, 10), 13)

    def test_every_round_drops_spread_over_the_middle_plus_super_in_sudden_death(self):
        rng = random.Random(3)
        sched = E.drop_schedule(rng, 180, 10, 0.5, sudden=False)
        self.assertEqual(len(sched), 5)
        self.assertTrue(all(18 <= at <= 153 and not sup for at, _, sup in sched))
        ats = [at for at, _, _ in sched]
        self.assertEqual(ats, sorted(ats))
        # equal slots with jitter: consecutive drops are at least a slot apart minus jitter,
        # i.e. no two land inside the same slot
        slot = (153 - 18) / 5
        self.assertEqual(sorted(int((at - 18) // slot) for at in ats), [0, 1, 2, 3, 4])
        self.assertEqual(len(E.drop_schedule(rng, 30, 2, 0.5, sudden=False)), 1)     # never zero
        sd = E.drop_schedule(rng, 90, 4, 0.5, sudden=True)
        supers = [k for _, k, sup in sd if sup]
        self.assertEqual(len(supers), 1)
        self.assertIn(supers[0], E.SUPER)

    def test_every_powerup_lists_a_benefit_and_a_weakness(self):
        for table in (E.POWERUPS, E.SUPER):
            for kind, (emoji, name, blurb) in table.items():
                self.assertIn("✅", blurb, kind)
                self.assertIn("❌", blurb, kind)
                self.assertLess(blurb.index("✅"), blurb.index("❌"), kind)
                self.assertTrue(emoji and name)

    def test_pitfall_powerups_are_common_and_no_strings_ones_are_rare(self):
        # Paul 9/12: "the ones that have a pitfall should be more common; the
        # ones that don't have pitfalls are rares." Every power-up has a tier,
        # every common outweighs every rare, Overload > Extra shot in particular.
        self.assertEqual(set(E.POWERUP_TIER), set(E.POWERUPS))
        self.assertEqual(set(E.DROP_WEIGHTS), set(E.POWERUPS))
        common = [k for k, t in E.POWERUP_TIER.items() if t == "common"]
        uncommon = [k for k, t in E.POWERUP_TIER.items() if t == "uncommon"]
        rare = [k for k, t in E.POWERUP_TIER.items() if t == "rare"]
        # 9/26: the revives moved OUT of common — a revive has no strings
        self.assertEqual(sorted(common), ["medkit", "overload", "transfuse"])
        self.assertEqual(sorted(uncommon), ["bloodbag"])
        self.assertEqual(sorted(rare),
                         ["paramedic", "patch", "reflect", "revive_medium", "revive_small", "shield", "shot"])
        self.assertEqual(E.DROP_WEIGHTS["revive_small"], E.DROP_WEIGHTS["shield"])
        self.assertEqual(E.DROP_WEIGHTS["revive_medium"], E.DROP_WEIGHTS["revive_small"] / 2)
        # common > uncommon > rare, always
        self.assertGreater(E.TIERS["common"][2], E.TIERS["uncommon"][2])
        self.assertGreater(E.TIERS["uncommon"][2], E.TIERS["rare"][2])
        # the two big revives are supers; the extra one is golden-apple rare
        self.assertEqual(E.SUPER_WEIGHTS["revive_full"], 3)
        self.assertEqual(E.SUPER_WEIGHTS["revive_extra"], 1)
        # ... with the heal-an-ally family held out: it is scaled under everything
        # else (9/21), so its tier only sorts it against the other rungs
        plain = lambda ks: [k for k in ks if k not in E.HEALS]
        self.assertGreater(min(E.DROP_WEIGHTS[k] for k in plain(common)),
                           max(E.DROP_WEIGHTS[k] for k in plain(rare)))
        self.assertGreater(E.DROP_WEIGHTS["transfuse"], E.DROP_WEIGHTS["bloodbag"])
        self.assertGreater(E.DROP_WEIGHTS["bloodbag"], E.DROP_WEIGHTS["paramedic"])
        self.assertGreater(E.DROP_WEIGHTS["overload"], E.DROP_WEIGHTS["shot"])
        self.assertEqual(E.tier_of("overload"), ("⚪", "Common"))
        self.assertEqual(E.tier_of("shot")[1], "Rare")
        # the roll respects the weights: over many rolls commons dominate
        import collections
        rng = random.Random(7)
        n = collections.Counter(E.roll_drop(rng) for _ in range(6000))
        # (9/26: the revives left the common tier, so commons are ~55 % of the
        # full table rather than ~65 % — still the plurality, still each one
        # heavier than every rare; and with nobody out the revives are not in
        # the table at all, which puts commons back over 60 %)
        self.assertGreater(sum(n[k] for k in common), 0.50 * 6000)
        self.assertGreater(sum(n[k] for k in common), 1.25 * sum(n[k] for k in rare))
        n0 = collections.Counter(E.roll_drop(rng, E.drop_weights(12)) for _ in range(6000))
        self.assertGreater(sum(n0[k] for k in common), 0.60 * 6000)

    def test_heads_up_drops_favour_attack_and_self_heal(self):
        # Paul 9/21: "when there's two people left make it more likely to get
        # self heal and attack items." A full field's table is untouched.
        self.assertEqual(E.drop_weights(E.ALLY_ALIVE + 1, dead=1), E.DROP_WEIGHTS)
        self.assertEqual(E.drop_weights(E.ALLY_ALIVE + 1, supers=True, dead=1), E.SUPER_WEIGHTS)
        duel = E.drop_weights(2, dead=1)
        self.assertLessEqual(set(duel), set(E.POWERUPS))
        attack = sum(duel[k] for k in ("overload", "shot"))
        self_heal = sum(duel[k] for k in ("patch", "medkit"))
        total = sum(duel.values())
        self.assertGreater(attack + self_heal, 0.85 * total)
        for k in ("overload", "shot", "patch", "medkit"):      # every one of them gains
            self.assertGreater(duel[k] / total, E.DROP_WEIGHTS[k] / sum(E.DROP_WEIGHTS.values()), k)
        # the dead drops are gone: shields/mirrors are off in sudden death (two
        # alive is always sudden death), ally heals would heal your only enemy
        for k in ("shield", "reflect", "transfuse", "bloodbag", "paramedic"):
            self.assertNotIn(k, duel, k)
        # supers: the ally heals go too, the golden apple stays ~one in ten
        sup = E.drop_weights(2, supers=True, dead=1)
        self.assertLessEqual(set(sup), set(E.SUPER))
        for k in ("fieldhosp", "ambrosia"):
            self.assertNotIn(k, sup, k)
        self.assertAlmostEqual(sup["goldapple"] / sum(sup.values()), 0.10, delta=0.03)
        # and a heads-up round actually rolls against it
        import collections
        rng = random.Random(11)
        n = collections.Counter(E.roll_drop(rng, E.drop_weights(2)) for _ in range(3000))
        self.assertEqual(n["transfuse"], 0)
        self.assertGreater(n["overload"] + n["shot"] + n["patch"] + n["medkit"], 0.85 * 3000)
        kinds = [k for _, k, _ in E.drop_schedule(random.Random(5), 45, 2, 0.5, sudden=True)]
        self.assertTrue(all(k not in ("shield", "transfuse", "ambrosia") for k in kinds), kinds)

    def test_ally_heals_are_rarer_than_everything_else(self):
        # Paul 9/21: "ally items should be less common than everything else."
        # The invariant, in both tables: the commonest ally rung is still rarer
        # than the rarest thing anyone else can grab.
        for table in (E.DROP_WEIGHTS, E.SUPER_WEIGHTS):
            ally = [w for k, w in table.items() if k in E.HEALS]
            rest = [w for k, w in table.items() if k not in E.HEALS]
            self.assertTrue(ally and rest)
            self.assertLess(max(ally), min(rest))
        # and it holds by construction, not by hand-tuned numbers
        self.assertLess(E.ALLY_SCARCITY, 1)
        for kind in E.HEALS:
            self.assertEqual(E.KIND_WEIGHT[kind], E.ALLY_SCARCITY)
        import collections
        rng = random.Random(19)
        n = collections.Counter(E.roll_drop(rng, E.drop_weights(12)) for _ in range(8000))
        self.assertLess(sum(n[k] for k in E.HEALS), 0.10 * 8000)   # a treat, not filler
        self.assertGreater(sum(n[k] for k in E.HEALS), 0)          # but they do still drop

    def test_ally_heals_only_drop_with_a_crowd(self):
        # Paul 9/21: "ally items should come alive when there's more than 5
        # players … most people don't even use ally items yet."
        allies = set(E.HEALS)
        self.assertTrue(allies & set(E.drop_weights(E.ALLY_ALIVE + 1)))       # crowd: they drop
        self.assertTrue(allies & set(E.drop_weights(E.ALLY_ALIVE + 1, supers=True)))
        for alive in range(1, E.ALLY_ALIVE + 1):                              # thin field, nobody out: none of them
            self.assertFalse(allies & set(E.drop_weights(alive)), alive)
            self.assertFalse(allies & set(E.drop_weights(alive, supers=True)), alive)
        # their weight goes back to the items people actually fire, and nothing
        # else about the table moves (revives held out: they need someone dead)
        full, thin = E.drop_weights(E.ALLY_ALIVE + 1, dead=1), E.drop_weights(E.ALLY_ALIVE, dead=1)
        self.assertEqual(set(full) - set(thin), (allies | set(E.REVIVES)) & set(E.POWERUPS))
        for k in thin:
            self.assertEqual(thin[k], full[k], k)                             # raw weights unchanged
            self.assertGreater(thin[k] / sum(thin.values()), full[k] / sum(full.values()), k)
        import collections
        rng = random.Random(3)
        n = collections.Counter(E.roll_drop(rng, E.drop_weights(4)) for _ in range(3000))
        self.assertEqual(sum(n[k] for k in allies), 0)
        # the guide says so once, in the ally group header, from the constant
        self.assertIn(str(E.ALLY_ALIVE), E.ITEM_GROUPS["heal_ally"][2])

    def test_revives_and_ally_items_wait_for_the_endgame_in_a_small_field(self):
        # Paul 9/26 after race 20 (three players, two Small revives in round 2 with
        # nobody out): revives and ally items "should be only towards the end of
        # small races". A revive needs someone to revive, any race size.
        revives = set(E.REVIVES)
        allies = set(E.HEALS)
        for alive in (2, 3, 5, 8, 20):
            self.assertFalse(revives & set(E.drop_weights(alive)), alive)              # nobody out
            self.assertFalse(revives & set(E.drop_weights(alive, supers=True)), alive)
        # a crowd with someone out: revives drop, at the rare weight
        crowd = E.drop_weights(E.ALLY_ALIVE + 1, dead=1)
        self.assertTrue(revives & set(crowd))
        self.assertLess(sum(crowd.get(k, 0) for k in revives) / sum(crowd.values()), 0.15)
        # small field, someone out, but still more than LATE_ALIVE standing: wait
        for alive in range(E.LATE_ALIVE + 1, E.ALLY_ALIVE + 1):
            table = E.drop_weights(alive, dead=1)
            self.assertFalse((revives | allies) & set(table), alive)
            self.assertFalse((revives | allies) & set(E.drop_weights(alive, supers=True, dead=1)), alive)
        # endgame: someone out and LATE_ALIVE standing — both families come in
        late = E.drop_weights(E.LATE_ALIVE, dead=1)
        self.assertTrue(revives & set(late))
        self.assertTrue(allies & set(late))
        self.assertTrue(allies & set(E.drop_weights(E.LATE_ALIVE, supers=True, dead=2)))
        self.assertLess(max(late.get(k, 0) for k in allies), min(late[k] for k in late if k not in allies))
        # heads-up still rules on top: ally heals out, revives thinned but legal
        duel = E.drop_weights(E.DUEL_ALIVE, dead=1)
        self.assertFalse(allies & set(duel))
        self.assertTrue(revives & set(duel))
        self.assertLess(sum(duel.get(k, 0) for k in revives) / sum(duel.values()), 0.10)
        # the scheduler passes the count through: round 1 of a 3-player race never
        # offers a revive; the rows helper counts only the still-revivable dead
        kinds = [k for _, k, _ in E.drop_schedule(random.Random(2), 90, 3, 0.5, sudden=False, dead=0)]
        self.assertFalse(revives & set(kinds), kinds)
        rows = [P(1), P(2, alive=0, lives=0, died_round=1), P(3, alive=0, lives=0, revived=1)]
        self.assertEqual([p["user_id"] for p in E.revivable(rows)], ["2"])
        # the guide states both rules from the constants
        self.assertIn(str(E.LATE_ALIVE), E.ITEM_GROUPS["heal_ally"][2])
        self.assertIn(str(E.LATE_ALIVE), E.ITEM_GROUPS["revive"][2])
        self.assertIn(str(E.ALLY_ALIVE), E.ITEM_GROUPS["heal_ally"][2])

    def test_supers_go_to_the_kit_except_the_golden_apple(self):
        # Paul 9/26: "only item i've been okay with auto working is golden apple."
        p = P(1, lives=1, shots=0)
        E.grant_super(p, "fullheal", 3, 3)
        self.assertEqual(p["lives"], 1)                 # nothing happened yet
        self.assertEqual(p["fullheal"], 1)
        E.grant_super(p, "arsenal", 3, 3)
        self.assertEqual(p["shots"], 0)
        self.assertEqual(p["arsenal"], 1)
        E.grant_super(p, "nuke", 3, 3)
        self.assertEqual(p["nuke"], 1)
        E.grant_super(p, "goldapple", 3, 3)
        self.assertEqual(p["lives"], 5)                 # the ONE that fires on grab
        for kind in ("fullheal", "arsenal", "nuke", "extrashot"):
            self.assertIn(kind, E.ITEM_COLS)            # or they'd never persist
            self.assertIn(kind, E.SELF_USE)
        # used from the kit
        ok, _ = E.use_self(p, "arsenal", 1, 3, False, shot_cap=3)
        self.assertTrue(ok)
        self.assertEqual((p["shots"], p["arsenal"]), (3, 0))
        ok, _ = E.use_self(p, "nuke", 1, 3, False)
        self.assertTrue(ok)
        self.assertEqual(p["nuke"], 0)
        ok, _ = E.use_self(p, "fullheal", 1, 3, False)
        self.assertFalse(ok)                            # above max already (apple) — save it
        p["lives"] = 1
        # Paul 9/26: "i do like healing items making u lose a vote" — a Full heal
        # costs the vote like a Medkit: refused after you've fired, and once used
        # the shooting items are locked for the round
        self.assertFalse(E.use_self(p, "fullheal", 1, 3, True)[0])
        ok, _ = E.use_self(p, "fullheal", 1, 3, False)
        self.assertTrue(ok)
        self.assertEqual((p["lives"], p["fullheal"], p["skip_round"]), (3, 0, 1))
        p["arsenal"], p["extrashot"] = 1, 1
        self.assertFalse(E.use_self(p, "arsenal", 1, 3, False, shot_cap=3)[0])
        self.assertFalse(E.use_self(p, "extrashot", 1, 3, False, shot_cap=3)[0])
        self.assertIn("sat this round out", E.cast_error(p, P(9), "shot", round_no=1))
        ok, text = E.use_self(P(2, nuke=1), "nuke", 1, 3, False, nuked_this_round=True)
        self.assertFalse(ok)                            # one blast per round
        self.assertIn("already armed", text)

    def test_extra_shot_is_a_kit_item_used_the_round_you_mean_to_fire(self):
        # Paul 9/26: "make extra shot not auto use. it's gotta be strategic. i
        # clicked it and automatically got the shot.. it should have to be
        # picked in powerups."
        p = P(1, shots=1)
        line = E.grant(p, "shot", 3)
        self.assertEqual(p["shots"], 1)                 # the bank did NOT move
        self.assertEqual(p["extrashot"], 1)
        self.assertIn("kit", line)
        ok, text = E.use_self(p, "extrashot", 2, 3, False, shot_cap=3)
        self.assertTrue(ok, text)
        self.assertEqual((p["shots"], p["extrashot"]), (2, 0))
        # held past a round close like every other item; the SHOT it gives does not
        rows = [p, P(2)]
        E.grant(p, "shot", 3)
        E.open_round(rows)
        self.assertEqual((p["shots"], p["extrashot"]), (1, 1))
        # capped at shot_cap + 1, refused after a Medkit, refused when empty
        p["shots"] = 4
        self.assertFalse(E.use_self(p, "extrashot", 3, 3, False, shot_cap=3)[0])
        p["shots"], p["skip_round"] = 1, 3
        self.assertFalse(E.use_self(p, "extrashot", 3, 3, False, shot_cap=3)[0])
        self.assertFalse(E.use_self(P(3), "extrashot", 3, 3, False)[0])

    def test_golden_apple_goes_above_max_and_nothing_pulls_you_back_down(self):
        # Paul 9/26: the apple heals MORE than the item below it (Full heal, to
        # max) — it is a full heal plus GOLDAPPLE_OVER over the cap, from any
        # lives. Race 20: at 1 of 5 the old +2 rule landed him on 3 and the
        # card still said "above the cap".
        p = P(1, lives=1)
        line = E.grant_super(p, "goldapple", 3, 3)
        self.assertEqual(p["lives"], 3 + E.GOLDAPPLE_OVER)   # 1 -> 5: full heal + 2 over
        self.assertIn("2 above the cap of 3", line)
        q = P(3, lives=3)
        E.grant_super(q, "goldapple", 3, 3)
        self.assertEqual(q["lives"], 5)                 # from full: still max + 2
        E.grant_super(p, "goldapple", 3, 3)
        self.assertEqual(p["lives"], 5)                 # a second apple never stacks past max + 2
        # and it beats every self-heal below it from 1 life
        r = P(4, lives=1, fullheal=1)
        E.use_self(r, "fullheal", 1, 3, False)
        self.assertLess(r["lives"], p["lives"])
        p["fullheal"] = 1
        self.assertFalse(E.use_self(p, "fullheal", 1, 3, False)[0])   # full heal never lowers: refused above max
        self.assertEqual(p["lives"], 5)
        # patch / medkit refuse at or above max; transfuse never lowers the target
        self.assertFalse(E.use_self(p, "patch", 1, 3, False)[0])
        a = P(2, transfuse=1)
        resolve([a, p], [S(2, 1, "transfuse")])
        self.assertEqual(p["lives"], 5)
        self.assertEqual(a["lives"], 2)                 # still paid
        # a shot still takes 1 — it's a buffer, not armor
        resolve([a, p], [S(2, 1)], round_no=2)
        self.assertEqual(p["lives"], 4)

    def test_golden_apple_is_the_rarest_super(self):
        self.assertIn("goldapple", E.SUPER)
        plain = {k: w for k, w in E.SUPER_WEIGHTS.items() if k not in E.HEALS}
        low = min(plain.values())
        rarest = sorted(k for k, w in plain.items() if w == low)
        # 9/13 the extra revive — the apple's equivalent is as rare as the apple.
        # 🍯 Ambrosia was the third of them until 9/21, when the ally items were
        # scaled under everything else; it is rarer than the apple now.
        self.assertEqual(rarest, ["goldapple", "revive_extra"])
        self.assertLess(E.SUPER_WEIGHTS["ambrosia"], E.SUPER_WEIGHTS["goldapple"])
        self.assertEqual(set(E.SUPER_WEIGHTS), set(E.SUPER))
        rng = random.Random(11)
        n = sum(E.roll_drop(rng, E.SUPER_WEIGHTS) == "goldapple" for _ in range(5000))
        self.assertTrue(250 < n < 480, n)                # ≈ 1 in 14 of supers
        # only ever offered in sudden death (supers are), never in a regular drop
        sched = E.drop_schedule(random.Random(1), 180, 10, 0.5, sudden=False)
        self.assertFalse(any(k == "goldapple" for _, k, _ in sched))

    def test_nuke_hits_everyone_else_and_credits_kills(self):
        a, b, c, d = P(1), P(2, lives=1), P(3, shield=1), P(4)
        # b casts a (wasted) self-shot so it counts as voting and the nuke kill pays
        r = resolve([a, b, c, d], [S(1, 1, "nuke"), S(2, 2)], round_no=6)
        self.assertEqual(a["lives"], 3)
        self.assertFalse(b["alive"])
        self.assertEqual(r["killers"]["2"], "1")
        self.assertEqual((c["lives"], c["shield"]), (3, 0))    # shield ate it (not sudden death)
        self.assertEqual(d["lives"], 2)
        self.assertEqual(a["kills"], 1)

    def test_nuke_counts_as_voting(self):
        a, b = P(1), P(2)
        resolve([a, b], [S(1, 1, "nuke"), S(2, 1)], afk=True)
        self.assertEqual(a["lives"], 2)      # b's shot only, no AFK hit


class Rewards(unittest.TestCase):
    def test_kill_pays_a_shield_or_a_patch_when_held(self):
        a, b, c = P(1), P(2, lives=1), P(3, lives=1)
        resolve([a, b, c], [S(1, 2), S(2, 1)])       # b votes, so the kill pays
        self.assertEqual(a["shield"], 1)
        # 9/13: that shield expires at the next close, so "already shielded" at pay
        # time now means two kills in ONE round — the second pays a Patch
        a, b, c = P(1, shots=2), P(2, lives=1), P(3, lives=1)
        r = resolve([a, b, c], [S(1, 2), S(1, 3), S(2, 1), S(3, 1)], round_no=2)
        self.assertEqual((a["shield"], a["patch"]), (1, 1))
        self.assertTrue(any("Patch for the kill" in ln for ln in r["lines"]))

    def test_bounty_placed_on_unique_top_killer_and_paid_out(self):
        a, b, c, d = P(1, kills=2), P(2), P(3), P(4)
        r = resolve([a, b, c, d], [])
        self.assertEqual(a["bounty"], 1)
        self.assertTrue(any("Bounty" in ln for ln in r["lines"]))
        # tie at the top = no bounty
        b["kills"] = 2
        resolve([a, b, c, d], [], round_no=2)
        self.assertEqual((a["bounty"], b["bounty"]), (0, 0))
        # finishing the bounty holder banks two extra shots
        a["bounty"], a["lives"], c["shots"] = 1, 1, 0
        resolve([a, b, c, d], [S(3, 1), S(1, 2)], round_no=3)
        self.assertFalse(a["alive"])
        self.assertEqual(c["shots"], 2)

    def test_bounty_needs_two_kills(self):
        a, b, c = P(1, kills=1), P(2), P(3)
        resolve([a, b, c], [])
        self.assertEqual(a["bounty"], 0)


class Afk(unittest.TestCase):
    def test_not_voting_costs_a_life_shield_or_not(self):
        a, b, c = P(1), P(2, shield=1), P(3)
        r = resolve([a, b, c], [S(1, 3)], afk=True, expire_items=False)
        self.assertEqual(a["lives"], 3)          # voted
        self.assertEqual(b["lives"], 2)          # afk, shield untouched (expiry off to isolate that)
        self.assertEqual(b["shield"], 1)
        self.assertEqual(c["lives"], 1)          # shot AND afk
        self.assertEqual(sum("didn't vote" in ln for ln in r["lines"]), 2)

    def test_killing_an_afk_player_pays_nothing(self):
        # c never votes: a's kill on c earns no shield, no shot, no kill
        # credit — and no bounty, even though c carried one.
        a, b, c = P(1), P(2), P(3, lives=1, bounty=1)
        shots = a["shots"]
        r = resolve([a, b, c], [S(1, 3), S(2, 1)], afk=True)
        self.assertFalse(c["alive"])
        self.assertEqual(r["killers"]["3"], "1")     # still named as the shooter
        self.assertEqual((a["shield"], a["shots"], a["kills"]), (0, shots, 0))
        self.assertTrue(any("AFK" in ln and "no reward" in ln for ln in r["lines"]))
        self.assertFalse(any("Bounty claimed" in ln for ln in r["lines"]))
        # same kill on a player who DID vote pays as normal
        a, b, c = P(1), P(2), P(3, lives=1)
        resolve([a, b, c], [S(1, 3), S(3, 2)], afk=True)
        self.assertEqual((a["shield"], a["kills"]), (1, 1))

    def test_medkit_round_is_not_afk_so_the_kill_still_pays(self):
        a, c = P(1), P(3, lives=1, skip_round=1)
        resolve([a, c], [S(1, 3)], afk=True)
        self.assertEqual((a["shield"], a["kills"]), (1, 1))

    def test_afk_no_reward_applies_even_with_the_penalty_off(self):
        a, b, c = P(1), P(2), P(3, lives=1)
        resolve([a, b, c], [S(1, 3)], afk=False)
        self.assertEqual((a["shield"], a["kills"]), (0, 0))
        self.assertEqual(b["lives"], 3)              # penalty off: b untouched

    def test_transfuse_alone_does_not_count_as_voting(self):
        a, b = P(1, transfuse=1), P(2)
        resolve([a, b], [S(1, 2, "transfuse")], afk=True)
        self.assertEqual(a["lives"], 1)          # -1 transfuse, -1 afk

    def test_afk_death_has_no_killer_and_storm_skips_the_afk(self):
        a, b, c, d = P(1, lives=1), P(2), P(3), P(4)
        r = resolve([a, b, c, d], [S(2, 3), S(3, 2), S(4, 3)], round_no=2, afk=True, storm=True,
                    msgs={"1": 0, "2": 0, "3": 9, "4": 9})
        self.assertFalse(a["alive"])
        self.assertNotIn("1", r["killers"])
        # storm skipped afk-hit a; quietest of the rest is b
        self.assertEqual(b["lives"], 1)          # shot by c, then the storm
        self.assertEqual(c["lives"], 1)          # shot by b and d
        self.assertEqual(d["lives"], 3)


class Storm(unittest.TestCase):
    def test_storm_hits_the_quietest_and_ignores_shields(self):
        a, b, c = P(1, shield=1), P(2), P(3)
        r = resolve([a, b, c], [], round_no=2, storm=True, msgs={"1": 0, "2": 5, "3": 9},
                    expire_items=False)
        self.assertEqual(a["lives"], 2)
        self.assertEqual(a["shield"], 1)
        self.assertTrue(any("storm" in ln for ln in r["lines"]))

    def test_storm_can_eliminate_without_kill_credit(self):
        a, b, c = P(1, lives=1), P(2), P(3)
        r = resolve([a, b, c], [], round_no=2, storm=True, msgs={"1": 0, "2": 1, "3": 1})
        self.assertFalse(a["alive"])
        self.assertIn("1", r["dead"])
        self.assertNotIn("1", r["killers"])

    def test_storm_stays_out_of_the_final_two(self):
        a, b = P(1), P(2)
        resolve([a, b], [], round_no=5, storm=True, msgs={"1": 0, "2": 9})
        self.assertEqual(a["lives"], 3)


class Rounds(unittest.TestCase):
    def test_open_round_hands_out_exactly_one_shot_every_round(self):
        # 9/13 (Paul): "one shot per round, they don't compile" — leftovers
        # used to ride along into later rounds. 9/21: the PURGE round (three
        # shots for everyone, once a race) is gone, so one is the only answer.
        a, b, dead = P(1, shots=0), P(2, shots=3), P(3, alive=0, shots=0)
        self.assertEqual(E.open_round([a, b, dead]), [])
        self.assertEqual((a["shots"], b["shots"], dead["shots"]), (1, 1, 0))
        self.assertEqual(E.open_round([a, b]), [])
        self.assertEqual((a["shots"], b["shots"]), (1, 1))
        self.assertFalse(hasattr(E, "pick_purge_round"))

    def test_sudden_death_threshold(self):
        rows = [P(i) for i in range(6)]
        self.assertFalse(E.is_sudden_death(rows, 5))
        rows[0]["alive"] = 0
        self.assertTrue(E.is_sudden_death(rows, 5))

    def test_recommended_lives_scale_with_players(self):
        # Paul 9/12: "scale lives with the amount of people playing — recommended, not forced"
        # Paul 9/15: "default life count should be 5 and scaled from there instead of 3"
        # Paul 9/16: "there should be no upper limit to it ... the parameters
        # scale the same" — the ladder keeps climbing instead of flattening at 12
        self.assertEqual([E.recommended_lives(n) for n in (0, 3, 5, 6, 10, 15, 20, 32, 100)],
                         [5, 5, 5, 5, 6, 7, 9, 12, 29])
        # nobody ever drops below the base, and the ladder only ever climbs
        ladder = [E.recommended_lives(n) for n in range(0, 200)]
        self.assertEqual(min(ladder), E.BASE_LIVES)
        self.assertEqual(ladder, sorted(ladder))
        self.assertGreater(E.recommended_lives(400), E.recommended_lives(200))
        self.assertEqual(E.DEFAULTS["lives"], E.BASE_LIVES)
        # explicit lives survive create_race untouched; auto is a flag the cog acts on at start
        r = E.create_race(9191, 3, 9, settings={"lives": 5, "lives_auto": False, "min_players": 15})
        self.assertEqual((r["settings"]["lives"], r["settings"]["lives_auto"]), (5, False))
        E.update_race(r["id"], status="aborted")

    def test_join_rules(self):
        now = time.time()
        self.assertIsNone(E.join_error(now - 30 * 86400, now, 7))
        self.assertIn("days", E.join_error(now - 86400, now, 7))
        self.assertIn("Bots", E.join_error(now - 999 * 86400, now, 7, is_bot=True))
        self.assertIn("can't ban", E.join_error(now - 999 * 86400, now, 7, bannable=False, mode="real"))
        self.assertIsNone(E.join_error(now - 999 * 86400, now, 7, bannable=False, mode="ghost"))

    def test_standings_hide_nothing_but_order_by_kills_then_lives(self):
        rows = [P(1, kills=0, lives=3), P(2, kills=2, lives=1), P(3, alive=0, died_round=2),
                P(4, alive=0, died_round=3)]
        live, fallen = E.standings(rows)
        self.assertEqual([p["user_id"] for p in live], ["2", "1"])
        self.assertEqual([p["user_id"] for p in fallen], ["4", "3"])


class Store(unittest.TestCase):
    def test_one_race_per_guild_and_guild_isolation(self):
        r1 = E.create_race(100, 1, 9)
        with self.assertRaises(ValueError):
            E.create_race(100, 1, 9)
        r2 = E.create_race(200, 2, 9)
        self.assertEqual(E.active_race(100)["id"], r1["id"])
        self.assertEqual(E.active_race(200)["id"], r2["id"])
        E.update_race(r1["id"], status="finished", winner_ids=["5"])
        self.assertIsNone(E.active_race(100))
        self.assertEqual(E.get_race(r1["id"])["winner_ids"], ["5"])
        self.assertEqual(E.get_race(r1["id"])["settings"]["lives"], E.DEFAULTS["lives"])
        self.assertEqual(E.get_race(r1["id"])["settings"]["mode"], "ghost")   # real bans are opt-in
        E.update_race(r2["id"], status="aborted")

    def test_players_shots_and_retarget_round_trip(self):
        r = E.create_race(300, 3, 9, settings={"lives": 2, "mode": "ghost"})
        rid = r["id"]
        self.assertEqual(r["settings"]["lives"], 2)
        E.join(rid, 1, "one", 2)
        E.join(rid, 2, "two", 2)
        E.join(rid, 1, "one again", 2)       # idempotent
        self.assertEqual(len(E.players(rid)), 2)
        E.update_race(rid, status="running", round_no=1)
        E.cast(rid, 1, 1, 2)
        self.assertTrue(E.retarget(rid, 1, 1, 2))
        self.assertFalse(E.retarget(rid, 1, 2, 1))   # never cast anything
        self.assertEqual(len(E.shots(rid, 1)), 1)
        self.assertEqual(E.shots(rid, 2), [])
        rows = E.players(rid)
        rows[1]["lives"], rows[1]["alive"], rows[1]["died_round"], rows[1]["banned"] = 0, 0, 1, 1
        E.save_players(rid, rows)
        self.assertEqual(E.player(rid, 2)["banned"], 1)
        self.assertEqual(E.player(rid, 2)["medkit"], 0)      # migrated columns present
        self.assertEqual(E.leave(rid, 1), 1)
        self.assertIsNone(E.player(rid, 1))
        E.update_race(rid, status="aborted")

    def test_latest_race_and_player_breakdown(self):
        r = E.create_race(777, 3, 9, settings={"lives": 3, "mode": "ghost"})
        rid = r["id"]
        E.join(rid, 1, "one", 3)
        E.join(rid, 2, "two", 3)
        E.update_race(rid, status="running", round_no=1)
        E.cast(rid, 1, 1, 2)                          # one fires at two
        E.cast(rid, 1, 2, 1, "overload")              # two overloads one
        E.log(rid, 1, f"🩸 {E.m(1)}'s shot hit {E.m(2)} — **2** lives left.")
        E.log(rid, 1, f"🛡️ {E.m(1)} grabbed a **shield**.")
        E.log(rid, 2, f"{E.m(2)} used a **Patch** — 🩹 Patched up — **3** lives.")
        E.log(rid, 2, f"😴 {E.m(1)} didn't vote — loses a life. **2** left.")
        E.update_race(rid, status="finished")
        self.assertIsNone(E.active_race(777))
        self.assertEqual(E.latest_race(777)["id"], rid)
        r2 = E.create_race(777, 3, 9, settings={"lives": 3, "mode": "ghost"})
        E.update_race(r2["id"], status="aborted")
        self.assertEqual([r["id"] for r in E.guild_races(777)], [r2["id"], rid])   # newest first
        self.assertEqual(E.guild_races(778), [])                                     # guild-scoped
        # player one: cast first, then what happened, per round; two's cast at one is NOT shown
        story = E.timeline(1, E.player_shots(rid, 1), E.player_log(rid, 1))
        self.assertEqual([r for r, _ in story], [1, 2])
        r1 = story[0][1]
        self.assertEqual(r1[0], f"🎯 fired at {E.m(2)}")
        self.assertTrue(any("shot hit" in ln for ln in r1))
        self.assertTrue(any("grabbed a **shield**" in ln for ln in r1))
        self.assertFalse(any("overloaded" in ln for ln in r1))
        self.assertTrue(any("didn't vote" in ln for ln in story[1][1]))
        # player two: their overload cast shows, plus the patch use
        story2 = E.timeline(2, E.player_shots(rid, 2), E.player_log(rid, 2))
        self.assertEqual(story2[0][1][0], f"💥 overloaded at {E.m(1)}")
        self.assertTrue(any("used a **Patch**" in ln for ln in story2[1][1]))
        # nobody else: empty
        self.assertEqual(E.timeline(9, E.player_shots(rid, 9), E.player_log(rid, 9)), [])
        # the open chart: everything, grouped by round, in order
        chart = E.by_round(E.race_log(rid))
        self.assertEqual([r for r, _ in chart], [1, 2])
        self.assertEqual((len(chart[0][1]), len(chart[1][1])), (2, 2))

    def test_alltime_counts_finished_races_only_and_ranks_by_wins_then_kills(self):
        g = 4242
        def race(winner, rows, status="finished", rounds=3):
            r = E.create_race(g, 3, 9, settings={"lives": 3, "mode": "ghost"})
            for uid, name, kills, died in rows:
                E.join(r["id"], uid, name, 3)
                E.update_player(r["id"], uid, kills=kills, alive=0 if died else 1, died_round=died,
                                lives=0 if died else 1)
            E.update_race(r["id"], status=status, round_no=rounds, finished_at=1000.0,
                          winner_ids=[str(winner)] if winner else [])
            E.cast(r["id"], 1, rows[0][0], rows[-1][0])
            return r["id"]
        r1 = race(1, [(1, "ann", 2, None), (2, "bob", 1, 2), (3, "cy", 0, 1)])
        r2 = race(2, [(1, "ann", 0, 1), (2, "bobby", 3, None)])
        race(None, [(1, "ann", 9, None)], status="aborted")             # ignored
        E.create_race(g, 3, 9)                                          # lobby, ignored
        d = E.alltime(g)
        self.assertEqual([r["id"] for r in d["races"]], [r2, r1])
        self.assertEqual(d["races"][0]["winner_names"], ["bobby"])      # latest name wins
        ann, bob, cy = d["players"]["1"], d["players"]["2"], d["players"]["3"]
        self.assertEqual((ann["races"], ann["wins"], ann["kills"], ann["outs"]), (2, 1, 2, 1))
        self.assertEqual(ann["rounds"], 3 + 1)                          # survived r1 (3), out r1 in r2
        self.assertEqual(ann["shots"], 2)                               # one cast per race
        self.assertEqual((bob["wins"], bob["kills"], bob["name"]), (1, 4, "bobby"))
        self.assertEqual(cy["shots"], 0)
        board = E.leaderboard(d["players"])
        self.assertEqual([uid for uid, _ in board], ["2", "1", "3"])   # 1 win each: bob 4 kills > ann 2
        self.assertEqual(E.alltime(4343), {"races": [], "players": {}})

    def test_fit_rounds_keeps_the_newest_that_fit(self):
        story = [(r, ["x" * 500]) for r in range(1, 31)]
        shown, dropped = E.fit_rounds(story, max_fields=24, max_chars=5200)
        self.assertEqual(dropped, 20)                       # 10 × 500 fit under 5200
        self.assertEqual([r for r, _ in shown], list(range(21, 31)))
        shown, dropped = E.fit_rounds([(1, ["y" * 3000])])
        self.assertEqual(dropped, 0)
        self.assertTrue(shown[0][1].endswith(" …") and len(shown[0][1]) <= 1000)
        self.assertEqual(E.fit_rounds([]), ([], 0))

    def test_min_players_stored_and_floored(self):
        r = E.create_race(500, 5, 9, settings={"min_players": 12})
        self.assertEqual(r["settings"]["min_players"], 12)
        E.update_race(r["id"], status="aborted")
        r = E.create_race(500, 5, 9, settings={"min_players": 1})
        self.assertEqual(r["settings"]["min_players"], E.MIN_PLAYERS)
        E.update_race(r["id"], status="aborted")

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            E.create_race(400, 4, 9, settings={"mode": "chaos"})




class Tracking(unittest.TestCase):
    """9/12: "a better way to track how power-ups are used, especially double
    shots (shot 1 here and shot 2 here)" — numbered casts, results written
    back, re-aims kept, the kit digest; plus the two frequency fixes and the
    sudden-death scaler."""

    def test_two_shots_in_a_round_are_named_in_the_results(self):
        a, b, c = P(1, shots=2), P(2), P(3)
        s1 = dict(S(1, 2), id=11, seq=1, extra=0)
        s2 = dict(S(1, 3), id=12, seq=2, extra=1)
        r = resolve([a, b, c], [s1, s2])
        self.assertTrue(any("'s shot 1 hit" in ln for ln in r["lines"]), r["lines"])
        self.assertTrue(any("'s shot 2 (extra) hit" in ln for ln in r["lines"]), r["lines"])
        self.assertEqual(r["results"], {11: "hit", 12: "hit"})

    def test_a_lone_shot_stays_unnumbered(self):
        a, b = P(1), P(2)
        r = resolve([a, b], [dict(S(1, 2), id=5, seq=1, extra=0)])
        self.assertTrue(any("'s shot hit" in ln and "shot 1" not in ln for ln in r["lines"]))
        self.assertEqual(r["results"], {5: "hit"})

    def test_results_cover_kill_shield_backfire_and_waste(self):
        a, b, c = P(1, shots=3), P(2, lives=1), P(3, shield=1)
        r = resolve([a, b, c], [dict(S(1, 2), id=1, seq=1), dict(S(1, 3), id=2, seq=2, extra=1),
                                dict(S(2, 1), id=3, seq=1)])
        self.assertEqual(r["results"][1], "kill")
        self.assertEqual(r["results"][2], "shielded")
        self.assertEqual(r["results"][3], "hit")      # b fires before dying (simultaneous)
        # already-dead target = wasted; backfire = backfire
        d = P(4, alive=0, lives=0)
        r2 = resolve([a, d], [dict(S(1, 4), id=9, seq=1)], round_no=2)
        self.assertEqual(r2["results"][9], "wasted")
        x, y = P(7, overload=1), P(8)
        r3 = resolve([x, y], [dict(S(7, 8, "overload"), id=21, seq=1)], rng=AlwaysBackfire())
        self.assertEqual(r3["results"][21], "backfire")
        self.assertEqual(x["lives"], 0)               # 1 to fire it, then its own 2

    def test_every_shot_result_has_a_timeline_tag(self):
        # a stray inline comment once swallowed the "wasted" and "self" entries,
        # so those shots rendered with no outcome at all in /lastrace player:
        for res in ("hit", "kill", "shielded", "backfire", "reflect", "wasted", "self",
                    "transfused", "healed", "late", "nuke", "overkill", "revived"):
            self.assertTrue(E.RESULT_TAG.get(res), res)

    def test_rows_without_ids_or_seq_still_resolve(self):
        a, b = P(1), P(2)
        r = resolve([a, b], [S(1, 2)])
        self.assertEqual(r["results"], {})
        self.assertEqual(b["lives"], 2)

    def test_cast_numbers_and_flags_extra_past_the_allowance(self):
        r = E.create_race(9100, 3, 9, settings={"lives": 3, "mode": "ghost"})
        rid = r["id"]
        E.join(rid, 1, "one", 3); E.join(rid, 2, "two", 3); E.join(rid, 3, "three", 3)
        E.update_race(rid, status="running", round_no=1)
        i1 = E.cast(rid, 1, 1, 2)                       # allowance 1: shot 1
        i2 = E.cast(rid, 1, 1, 3)                       # shot 2 = extra
        t = E.cast(rid, 1, 1, 2, "transfuse")           # not a banked shot: unnumbered
        self.assertEqual((i1["seq"], i1["extra"]), (1, 0))
        self.assertEqual((i2["seq"], i2["extra"]), (2, 1))
        self.assertEqual((t["seq"], t["extra"]), (None, 0))
        # a caller that hands out a bigger allowance: three free, the fourth extra
        for k in range(3):
            self.assertEqual(E.cast(rid, 2, 2, 1, allowance=3)["extra"], 0)
        self.assertEqual(E.cast(rid, 2, 2, 1, allowance=3)["extra"], 1)
        # overloads count in the numbering too
        self.assertEqual(E.cast(rid, 3, 3, 1, "overload")["seq"], 1)
        self.assertEqual(E.cast(rid, 3, 3, 2)["seq"], 2)
        # results written back by id
        E.mark_shots({i1["id"]: "hit", i2["id"]: "kill"})
        rows = {sh["seq"]: sh for sh in E.shots(rid, 1) if sh["kind"] == "shot"}
        self.assertEqual((rows[1]["result"], rows[2]["result"]), ("hit", "kill"))
        E.mark_shots({})                                  # no-op
        E.update_race(rid, status="aborted")

    def test_retarget_keeps_the_original_aim(self):
        r = E.create_race(9101, 3, 9, settings={"lives": 3, "mode": "ghost"})
        rid = r["id"]
        E.join(rid, 1, "one", 3); E.join(rid, 2, "two", 3); E.join(rid, 3, "three", 3)
        E.update_race(rid, status="running", round_no=1)
        E.cast(rid, 1, 1, 2)
        mv = E.retarget(rid, 1, 1, 3)
        self.assertEqual(mv, {"seq": 1, "kind": "shot", "was": "2", "prev_target_id": "2"})
        mv2 = E.retarget(rid, 1, 1, 2)                   # back again: original aim still 2
        self.assertEqual((mv2["was"], mv2["prev_target_id"]), ("3", "2"))
        self.assertIsNone(E.retarget(rid, 1, 2, 1))     # nothing cast
        # An overload spends the round's only shot, so /vote's plain re-aim (no
        # seq) has to find it — otherwise overloading leaves nothing to move.
        E.cast(rid, 1, 2, 1, "overload")
        mv_ov = E.retarget(rid, 1, 2, 3)
        self.assertEqual((mv_ov["seq"], mv_ov["kind"], mv_ov["was"]), (1, "overload", "1"))
        sh = E.shots(rid, 1)[0]
        self.assertEqual((sh["target_id"], sh["prev_target_id"]), ("2", "2"))
        # change a SPECIFIC shot by number (the "Change shot N" buttons), overloads too
        E.cast(rid, 1, 1, 3, "overload")                 # shot 2 = the overload
        E.cast(rid, 1, 1, 3)                             # shot 3
        self.assertEqual([f["seq"] for f in E.fired_shots(rid, 1, 1)], [1, 2, 3])
        mv3 = E.retarget(rid, 1, 1, 2, seq=2)
        self.assertEqual((mv3["seq"], mv3["kind"], mv3["was"]), (2, "overload", "3"))
        self.assertIsNone(E.retarget(rid, 1, 1, 2, seq=9))
        by_seq = {f["seq"]: f for f in E.fired_shots(rid, 1, 1)}
        self.assertEqual((by_seq[2]["target_id"], by_seq[2]["prev_target_id"]), ("2", "3"))
        self.assertEqual(by_seq[3]["target_id"], "3")   # untouched
        E.update_race(rid, status="aborted")

    def test_timeline_numbers_double_shots_and_shows_outcomes(self):
        r = E.create_race(9102, 3, 9, settings={"lives": 3, "mode": "ghost"})
        rid = r["id"]
        E.join(rid, 1, "one", 3); E.join(rid, 2, "two", 3); E.join(rid, 3, "three", 3)
        E.update_race(rid, status="running", round_no=1)
        i1 = E.cast(rid, 1, 1, 2)
        i2 = E.cast(rid, 1, 1, 3)
        E.retarget(rid, 1, 1, 2)                        # shot 2 re-aimed 3 → 2
        E.mark_shots({i1["id"]: "hit", i2["id"]: "kill"})
        E.cast(rid, 2, 1, 3)                            # a lone shot next round
        story = dict(E.timeline(1, E.player_shots(rid, 1), E.player_log(rid, 1)))
        r1 = story[1]
        self.assertEqual(r1[0], f"🎯 **Shot 1** — fired at {E.m(2)} → hit")
        self.assertEqual(r1[1], f"🎯 **Shot 2 (extra)** — fired at {E.m(2)} (re-aimed from {E.m(3)}) → **KILL**")
        self.assertEqual(story[2][0], f"🎯 fired at {E.m(3)}")
        E.update_race(rid, status="aborted")

    def test_powerup_digest_counts_grabs_and_uses(self):
        r = E.create_race(9103, 3, 9, settings={"lives": 3, "mode": "ghost"})
        rid = r["id"]
        E.join(rid, 1, "one", 3); E.join(rid, 2, "two", 3)
        E.update_race(rid, status="running", round_no=1)
        E.log(rid, 1, f"🛡️ {E.m(1)} grabbed a **shield**.", kind="grab", user_id=1)
        E.log(rid, 1, f"🔫 {E.m(1)} grabbed an **extra shot**.", kind="grab", user_id=1)
        E.log(rid, 1, f"🛡️ {E.m(1)} grabbed a **shield**.", kind="grab", user_id=1)
        E.log(rid, 2, f"{E.m(1)} used a **Patch** — 🩹 Patched up — **3** lives.", kind="use", user_id=1)
        E.log(rid, 2, f"🛡️ {E.m(1)}'s **shield** ate {E.m(2)}'s shot.", kind="resolve")
        E.cast(rid, 1, 1, 2)
        E.cast(rid, 1, 1, 2)                            # the extra one
        E.cast(rid, 2, 1, 2, "overload")
        d = E.powerup_digest(rid, 1)
        self.assertEqual(d["grabbed"], {"shield": 2, "extra shot": 1})
        self.assertEqual(d["used"], {"Patch": 1, "Overload": 1, "Extra shot": 1, "Shield": 1})
        line = E.digest_line(d)
        self.assertTrue(line.startswith("🎒 grabbed 3 (shield ×2, extra shot) · used 4 ("), line)
        self.assertEqual(E.digest_line(E.powerup_digest(rid, 2)), "")
        # old, untagged rows (pre-9/12) still reach the player's story via the mention
        E.log(rid, 3, f"😴 {E.m(2)} didn't vote — loses a life. **2** left.")
        self.assertTrue(any("didn't vote" in ln for _, ls in E.timeline(2, [], E.player_log(rid, 2)) for ln in ls))
        E.update_race(rid, status="aborted")

    def test_extra_shot_drops_rarer_than_the_other_rares(self):
        self.assertLess(E.DROP_WEIGHTS["shot"], E.DROP_WEIGHTS["shield"])
        self.assertEqual(E.DROP_WEIGHTS["shield"], E.DROP_WEIGHTS["patch"])
        import collections
        rng = random.Random(3)
        n = collections.Counter(E.roll_drop(rng) for _ in range(8000))
        self.assertLess(n["shot"], 0.7 * n["shield"])

    def test_sudden_death_scales_with_the_field(self):
        # 9/12: a fixed 5 meant a 6-player race hit sudden death in round 2
        # 9/16: uncapped too — the last quarter of a 40-player field is 10,
        # not 5, so a big race doesn't spend half of itself in sudden death
        self.assertEqual([E.recommended_sudden_death(n) for n in (3, 6, 8, 9, 12, 15, 16, 20, 40)],
                         [2, 2, 2, 3, 3, 4, 4, 5, 10])
        self.assertEqual(E.recommended_sudden_death(0), 2)


class Expiry(unittest.TestCase):
    """9/13 (Paul, final): power-ups stay until the RACE ends. The optional
    use-it-or-lose-it mode still works when asked for."""

    def test_unused_items_stay_by_default(self):
        a, b = P(1, shield=1, overload=1, patch=1, medkit=1, transfuse=1, revive_small=1), P(2)
        r = resolve([a, b], [S(2, 1)])                       # b shoots a: the shield pops, the rest stays
        self.assertEqual((a["shield"], a["overload"], a["patch"], a["medkit"], a["transfuse"], a["revive_small"]),
                         (0, 1, 1, 1, 1, 1))
        self.assertFalse(any("expired" in ln for ln in r["lines"]))

    def test_optional_expiry_mode_clears_unused_items_but_a_kill_shield_survives(self):
        a, b = P(1, overload=1, patch=1, medkit=1, transfuse=1, revive_medium=1), P(2, lives=1)
        r = resolve([a, b], [S(1, 2), S(2, 1)], expire_items=True)   # b votes, so the kill pays
        self.assertEqual((a["overload"], a["patch"], a["medkit"], a["transfuse"], a["revive_medium"]), (0, 0, 0, 0, 0))
        self.assertEqual(a["shield"], 1)
        self.assertTrue(any("expired" in ln for ln in r["lines"]))


class Revive(unittest.TestCase):
    """9/13 (Paul): "add a revive item"."""

    def test_revive_brings_a_dead_player_back_with_one_life_after_everything_else(self):
        a, b, c = P(1, revive_small=1, shots=1), P(2, alive=0, lives=0, died_round=1), P(3)
        r = resolve([a, b, c], [S(1, 3), S(3, 1), S(1, 2, "revive_small")], round_no=2, storm=True, msgs={})
        self.assertTrue(b["alive"])
        self.assertEqual((b["lives"], b["revived"], b["shots"]), (1, 1, 0))
        self.assertNotIn("2", r["dead"])
        self.assertEqual(r["revived"], ["2"])
        self.assertEqual(r["winners"], [])
        self.assertTrue(any("is back" in ln for ln in r["lines"]))

    def test_each_revive_tier_brings_back_the_right_lives(self):
        for kind, want in (("revive_small", 1), ("revive_medium", 2), ("revive_full", 3), ("revive_extra", 4)):
            a, b, c = P(1, **{kind: 1}), P(2, alive=0, lives=0, died_round=1), P(3)
            resolve([a, b, c], [S(1, 3), S(3, 1), S(1, 2, kind)])
            self.assertEqual((b["alive"], b["lives"]), (1, want), kind)
        self.assertEqual(E.revive_lives("revive_medium", 1), 1)   # never above a 1-life race's max on medium

    def test_nobody_returns_twice(self):
        a, b, c = P(1, revive_full=1), P(2, alive=0, lives=0, revived=1), P(3)
        r = resolve([a, b, c], [S(1, 3), S(3, 1), S(1, 2, "revive_full")])
        self.assertFalse(b["alive"])
        self.assertEqual(r["revived"], [])
        self.assertTrue(any("fizzled" in ln for ln in r["lines"]))

    def test_dying_and_being_revived_in_the_same_round_means_no_ban(self):
        a, b, c = P(1, revive_small=1), P(2, revive_small=1), P(3, lives=1)
        r = resolve([a, b, c], [S(1, 3), S(3, 1), S(2, 3, "revive_small")])
        self.assertTrue(c["alive"])
        self.assertEqual(c["lives"], 1)
        self.assertNotIn("3", r["dead"])
        self.assertEqual(r["killers"].get("3"), "1")       # the kill still counts for a

    def test_cast_rules(self):
        a, dead, alive_, twice = P(1, revive_small=1), P(2, alive=0, lives=0), P(3), P(4, alive=0, revived=1)
        self.assertIsNone(E.cast_error(a, dead, "revive_small"))
        self.assertIn("still in", E.cast_error(a, alive_, "revive_small"))
        self.assertIn("twice", E.cast_error(a, twice, "revive_small"))
        self.assertIn("alive", E.cast_error(a, a, "revive_small"))
        a["revive_small"] = 0
        self.assertIn("don't hold", E.cast_error(a, dead, "revive_small"))
        p = P(5)
        E.grant(p, "revive_medium", shot_cap=3)
        self.assertEqual(p["revive_medium"], 1)
        E.spend(p, "revive_medium")
        self.assertEqual(p["revive_medium"], 0)
        E.grant_super(p, "revive_extra", 3, 3)            # super revives go to the kit, not instant
        self.assertEqual(p["revive_extra"], 1)
        self.assertEqual(p["lives"], 3)


class Overkill(unittest.TestCase):
    """Damage past a victim's last life, inside one round (Paul 9/15). The
    killing blow's excess plus every shot that lands on them once they're
    already down — pile-ons used to resolve as "wasted"."""

    def test_reward_table(self):
        self.assertIsNone(E.overkill_reward(0))
        self.assertIsNone(E.overkill_reward(1))            # 1 over is flavour only
        self.assertEqual(E.overkill_reward(2)[1:], ("OVERKILL", 1))
        self.assertEqual(E.overkill_reward(3)[1:], ("BRUTAL", 2))
        self.assertEqual(E.overkill_reward(4)[1:], ("OBLITERATED", 3))
        self.assertEqual(E.overkill_reward(20)[2], E.OVERKILL_MAX_PATCHES)   # capped

    def test_one_over_pays_nothing(self):
        # overload's 2 into a one-life player: 1 over, under the threshold
        a, b = P(1), P(2, lives=1)
        r = resolve([a, b], [S(1, 2, "overload"), S(2, 1)])
        self.assertEqual(r["overkill"], {"2": 1})
        self.assertEqual(a["patch"], 0)
        self.assertEqual(a["shield"], 1)                   # the plain kill pay still lands

    def test_two_over_pays_one_patch_to_the_killer(self):
        a, b, c, v = P(1), P(2), P(3), P(4, lives=1)
        r = resolve([a, b, c, v], [S(1, 4), S(2, 4), S(3, 4), S(4, 1)])
        self.assertEqual(r["overkill"], {"4": 2})          # two shots landed on the corpse
        self.assertEqual(a["patch"], 1)                    # killer collects for the whole pile
        self.assertEqual(a["overkill"], 2)
        self.assertEqual(b["patch"], 0)                    # pilers get named, not paid
        self.assertEqual(c["patch"], 0)

    def test_the_pile_is_capped(self):
        rows = [P(i) for i in range(1, 7)] + [P(7, lives=1)]
        shots = [S(i, 7) for i in range(1, 7)] + [S(7, 1)]
        resolve(rows, shots)
        self.assertEqual(rows[0]["overkill"], 5)           # 5 shots past the last life
        self.assertEqual(rows[0]["patch"], E.OVERKILL_MAX_PATCHES)

    def test_overload_excess_and_a_pile_on_add_up(self):
        a, b, v = P(1), P(2), P(3, lives=1)
        r = resolve([a, b, v], [S(1, 3, "overload"), S(2, 3), S(3, 1)])
        self.assertEqual(r["overkill"], {"3": 2})          # 1 excess + 1 pile-on
        self.assertEqual(a["patch"], 1)

    def test_an_afk_victim_still_pays_nothing(self):
        # the whole point of the AFK rule: no free anything off someone absent
        a, b, c, v = P(1), P(2), P(3), P(4, lives=1)
        resolve([a, b, c, v], [S(1, 4), S(2, 4), S(3, 4)], afk=True)
        self.assertEqual(a["patch"], 0)
        self.assertEqual(a["shield"], 0)
        self.assertEqual(a.get("overkill", 0), 0)
        self.assertEqual(a["kills"], 0)

    def test_the_card_says_an_afk_pile_on_pays_nothing(self):
        a, b, v = P(1), P(2), P(3, lives=1)
        r = resolve([a, b, v], [S(1, 3), S(2, 3)], afk=True)
        pile = [ln for ln in r["lines"] if "already down" in ln]
        self.assertTrue(pile)
        self.assertIn("no reward given, player AFK", pile[0])
        self.assertEqual(a.get("overkill", 0) + b.get("overkill", 0), 0)

    def test_a_shot_at_last_round_s_corpse_is_still_wasted(self):
        a, v = P(1), P(2, lives=0, alive=0, died_round=1)
        s = S(1, 2)
        s["id"] = 7
        r = resolve([a, v], [s], round_no=2)
        self.assertEqual(r["results"][7], "wasted")
        self.assertEqual(r["overkill"], {})

    def test_pile_on_shots_are_recorded_as_overkill_not_wasted(self):
        a, b, v = P(1), P(2), P(3, lives=1)
        first, pile = S(1, 3), S(2, 3)
        first["id"], pile["id"] = 1, 2
        r = resolve([a, b, v], [first, pile, S(3, 1)])
        self.assertEqual(r["results"][1], "kill")
        self.assertEqual(r["results"][2], "overkill")

    def test_it_survives_a_save_and_reload(self):
        # the running total is the hook an XP system would read later
        race = E.create_race("g-overkill", "c", "h")
        E.join(race["id"], 1, "one", 3)
        rows = E.players(race["id"])
        rows[0]["overkill"] = 6
        E.save_players(race["id"], rows)
        self.assertEqual(E.player(race["id"], 1)["overkill"], 6)


class LateJoin(unittest.TestCase):
    """Paul 9/16: "anyone can join at anytime" + "give them a handicap based on
    how many rounds there are"."""

    def test_the_handicap_is_one_life_per_round_already_played(self):
        self.assertEqual([E.late_join_lives(5, r) for r in (0, 1, 2, 3, 4, 5, 9)],
                         [5, 4, 3, 2, 1, 1, 1])
        self.assertEqual(E.late_join_lives(12, 3), 9)
        self.assertEqual(E.late_join_lives(1, 0), 1)
        self.assertEqual(E.late_join_lives(5, -2), 5)     # nonsense input can't hand out extra

    def test_the_round_they_walk_in_on_cannot_punish_them(self):
        # no shot cast, everyone silent: the AFK penalty and the storm both
        # skip the newcomer, and hit the player who was already here
        race = E.create_race("g-late", "c", "h")
        E.join(race["id"], 1, "veteran", 5)
        E.join(race["id"], 2, "veteran2", 5)
        E.join(race["id"], 3, "newcomer", 2, joined_round=4, shots=1)
        rows = E.players(race["id"])
        r = resolve(rows, [], round_no=4, rng=random.Random(1), storm=True, afk=True, max_lives=5)
        by = {p["name"]: p for p in rows}
        self.assertEqual(by["newcomer"]["lives"], 2, r["lines"])
        self.assertLess(by["veteran"]["lives"], 5)
        self.assertLess(by["veteran2"]["lives"], 5)

    def test_from_the_next_round_they_are_a_player_like_any_other(self):
        race = E.create_race("g-late2", "c", "h")
        E.join(race["id"], 1, "veteran", 5)
        E.join(race["id"], 2, "newcomer", 2, joined_round=4, shots=1)
        rows = E.players(race["id"])
        r = resolve(rows, [], round_no=5, rng=random.Random(1), afk=True, max_lives=5)
        by = {p["name"]: p for p in rows}
        self.assertEqual(by["newcomer"]["lives"], 1, r["lines"])   # AFK now costs them

    def test_the_late_row_round_trips(self):
        race = E.create_race("g-late3", "c", "h")
        E.join(race["id"], 7, "newcomer", 3, joined_round=2, shots=1)
        p = E.player(race["id"], 7)
        self.assertEqual((p["joined_round"], p["shots"], p["lives"]), (2, 1, 3))
        # a normal join is not a late one
        E.join(race["id"], 8, "regular", 5)
        self.assertIsNone(E.player(race["id"], 8)["joined_round"])


class ScheduleTests(unittest.TestCase):
    """The clock, not the lobby, starts a race (Paul 9/16). These pin the two
    things that can silently ruin a night: firing the same slot twice across a
    restart, and the slots sliding an hour when the US changes its clocks."""

    TZ = "America/New_York"

    def at(self, s):
        """'2026-09-16 20:00' local -> unix time."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(self.TZ)).timestamp()

    def local(self, ts):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo(self.TZ)).strftime("%Y-%m-%d %H:%M")

    def test_the_default_is_a_race_every_four_hours(self):
        # Paul 9/18: "let's make games every 4 hours instead of 6"
        self.assertEqual(E.SCHEDULE_SLOTS,
                         ("00:00", "04:00", "08:00", "12:00", "16:00", "20:00"))
        mins = [int(s[:2]) * 60 + int(s[3:]) for s in E.SCHEDULE_SLOTS]
        gaps = [(b - a) % (24 * 60) for a, b in zip(mins, mins[1:] + mins[:1])]
        self.assertEqual(gaps, [240] * 6)      # evenly spaced, wrapping midnight

    def test_the_slots_parse(self):
        self.assertEqual(E.parse_slots("0:00, 4:00 8:00,12:00 16:00,20:00"),
                         ("00:00", "04:00", "08:00", "12:00", "16:00", "20:00"))
        self.assertEqual(E.parse_slots(None), tuple(E.SCHEDULE_SLOTS))
        self.assertEqual(E.parse_slots(["20:00", "8:00", "20:00"]), ("08:00", "20:00"))
        for bad in ("25:00", "8:61", "lunch"):
            with self.assertRaises(ValueError):
                E.parse_slots(bad)

    def test_next_slot_walks_the_day(self):
        self.assertEqual(self.local(E.next_slot(self.at("2026-09-16 18:28"), tz=self.TZ)),
                         "2026-09-16 20:00")
        self.assertEqual(self.local(E.next_slot(self.at("2026-09-16 16:00"), tz=self.TZ)),
                         "2026-09-16 20:00")   # exactly on a slot -> the NEXT one
        self.assertEqual(self.local(E.next_slot(self.at("2026-09-16 23:30"), tz=self.TZ)),
                         "2026-09-17 00:00")   # over midnight

    def test_previous_slot_looks_back_over_midnight(self):
        self.assertEqual(self.local(E.previous_slot(self.at("2026-09-17 01:30"), tz=self.TZ)),
                         "2026-09-17 00:00")
        self.assertEqual(self.local(E.previous_slot(self.at("2026-09-17 00:00") - 60, tz=self.TZ)),
                         "2026-09-16 20:00")   # just before midnight, yesterday's last slot
        self.assertEqual(self.local(E.previous_slot(self.at("2026-09-16 20:00"), tz=self.TZ)),
                         "2026-09-16 20:00")   # on the slot, it IS the slot

    def test_the_slots_hold_their_wall_clock_across_dst(self):
        # EDT -> EST (fall back, 2026-11-01) and EST -> EDT (spring forward,
        # 2027-03-14). 8pm must stay 8pm on Paul's clock, not slide to 7 or 9.
        for day in ("2026-10-31", "2026-11-01", "2026-11-02", "2027-03-13", "2027-03-15"):
            got = self.local(E.next_slot(self.at(f"{day} 19:00"), tz=self.TZ))
            self.assertEqual(got, f"{day} 20:00", day)

    def test_spring_forward_does_not_lose_a_slot_inside_the_gap(self):
        # 2027-03-14 02:00 does not exist locally. No DEFAULT slot sits in the
        # gap any more, but a guild can still set one on the dashboard, so the
        # arithmetic has to hold: the race happens once, the instant the clock
        # reaches 03:00.
        slots = ("02:00", "08:00")
        ts = E.next_slot(self.at("2027-03-14 01:30"), slots, tz=self.TZ)
        self.assertEqual(self.local(ts), "2027-03-14 03:00")
        self.assertEqual(self.local(E.next_slot(ts, slots, tz=self.TZ)), "2027-03-14 08:00")

    def test_a_slot_is_owed_once_and_only_once(self):
        slot = self.at("2026-09-16 20:00")
        self.assertIsNone(E.due_slot(slot - 1, None, tz=self.TZ))       # not yet
        self.assertEqual(E.due_slot(slot, None, tz=self.TZ), slot)      # on the second
        self.assertEqual(E.due_slot(slot + 120, None, tz=self.TZ), slot)
        # already acted on it: a restart 10 s later must not fire it again
        self.assertIsNone(E.due_slot(slot + 10, slot, tz=self.TZ))
        self.assertIsNone(E.due_slot(slot + 10, str(slot), tz=self.TZ))  # config round-trips as a string

    def test_a_slot_missed_to_downtime_expires(self):
        slot = self.at("2026-09-16 20:00")
        self.assertEqual(E.due_slot(slot + E.FIRE_GRACE - 5, None, tz=self.TZ), slot)
        self.assertIsNone(E.due_slot(slot + E.FIRE_GRACE + 5, None, tz=self.TZ))

    def test_warnings_fire_biggest_first_and_never_twice(self):
        slot = self.at("2026-09-16 20:00")
        self.assertIsNone(E.warning_due(slot - 3600, slot))
        self.assertEqual(E.warning_due(slot - 900, slot), 900)
        self.assertEqual(E.warning_due(slot - 300, slot, sent=[900]), None)
        self.assertEqual(E.warning_due(slot - 60, slot, sent=[900]), 60)
        self.assertIsNone(E.warning_due(slot - 30, slot, sent=[900, 60]))
        self.assertIsNone(E.warning_due(slot + 5, slot))

    def test_next_of_each_gives_one_stamp_per_slot(self):
        # the card renders these as <t:...:t>, so each must be that slot's next
        # occurrence — same time of day, today or tomorrow, never in the past
        now = self.at("2026-09-16 18:28")
        got = E.next_of_each(now, tz=self.TZ)
        self.assertEqual([self.local(t) for t in got],
                         ["2026-09-17 00:00", "2026-09-17 04:00", "2026-09-17 08:00",
                          "2026-09-17 12:00", "2026-09-17 16:00", "2026-09-16 20:00"])
        self.assertTrue(all(t > now for t in got))
        # just after a slot, that slot rolls to tomorrow and the rest stay today
        got = E.next_of_each(self.at("2026-09-16 08:01"), tz=self.TZ)
        self.assertEqual([self.local(t) for t in got],
                         ["2026-09-17 00:00", "2026-09-17 04:00", "2026-09-17 08:00",
                          "2026-09-16 12:00", "2026-09-16 16:00", "2026-09-16 20:00"])

    def test_the_line_says_how_many_more_are_needed(self):
        slot = self.at("2026-09-16 20:00")
        self.assertIn("needs **1** more", E.schedule_line(slot, 2, 3))
        self.assertIn("it's on", E.schedule_line(slot, 4, 3))


if __name__ == "__main__":
    unittest.main()
