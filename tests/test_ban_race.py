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
             revive_small=0, revive_medium=0, revive_full=0, revive_extra=0)
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
        a, b = P(1), P(2)
        resolve([a, b], [S(1, 2)], rng=AlwaysBackfire())
        self.assertEqual(a["lives"], 2)
        self.assertEqual(b["lives"], 3)

    def test_backfire_self_kill_gives_no_credit(self):
        a, b = P(1, lives=1), P(2)
        r = resolve([a, b], [S(1, 2)], rng=AlwaysBackfire())
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
        rare = [k for k, t in E.POWERUP_TIER.items() if t == "rare"]
        self.assertEqual(sorted(common), ["medkit", "overload", "revive_small", "transfuse"])
        self.assertEqual(sorted(rare), ["patch", "revive_medium", "shield", "shot"])
        # the two big revives are supers; the extra one is golden-apple rare
        self.assertEqual(E.SUPER_WEIGHTS["revive_full"], 3)
        self.assertEqual(E.SUPER_WEIGHTS["revive_extra"], 1)
        self.assertGreater(min(E.DROP_WEIGHTS[k] for k in common), max(E.DROP_WEIGHTS[k] for k in rare))
        self.assertGreater(E.DROP_WEIGHTS["overload"], E.DROP_WEIGHTS["shot"])
        self.assertEqual(E.tier_of("overload"), ("⚪", "Common"))
        self.assertEqual(E.tier_of("shot")[1], "Rare")
        # the roll respects the weights: over many rolls commons dominate
        import collections
        rng = random.Random(7)
        n = collections.Counter(E.roll_drop(rng) for _ in range(6000))
        self.assertGreater(sum(n[k] for k in common), 0.65 * 6000)

    def test_super_effects(self):
        p = P(1, lives=1, shots=0)
        E.grant_super(p, "fullheal", 3, 3)
        self.assertEqual(p["lives"], 3)
        E.grant_super(p, "arsenal", 3, 3)
        self.assertEqual(p["shots"], 3)

    def test_golden_apple_goes_above_max_and_nothing_pulls_you_back_down(self):
        p = P(1, lives=2)
        E.grant_super(p, "goldapple", 3, 3)
        self.assertEqual(p["lives"], 4)                 # above the cap of 3
        E.grant_super(p, "goldapple", 3, 3)
        self.assertEqual(p["lives"], 5)                 # capped at max + 2
        E.grant_super(p, "goldapple", 3, 3)
        self.assertEqual(p["lives"], 5)
        E.grant_super(p, "fullheal", 3, 3)
        self.assertEqual(p["lives"], 5)                 # full heal never lowers
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
        low = min(E.SUPER_WEIGHTS.values())
        rarest = sorted(k for k, w in E.SUPER_WEIGHTS.items() if w == low)
        self.assertEqual(rarest, ["goldapple", "revive_extra"])      # 9/13: the extra revive is as rare
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
    def test_open_round_hands_out_exactly_one_shot_and_purge_gives_three(self):
        # 9/13 (Paul): "one shot per round, they don't compile" — a leftover
        # purge shot used to ride along for three more rounds
        a, b, dead = P(1, shots=0), P(2, shots=3), P(3, alive=0, shots=0)
        self.assertEqual(E.open_round([a, b, dead], 1, purge_round=4, shot_cap=3), [])
        self.assertEqual((a["shots"], b["shots"], dead["shots"]), (1, 1, 0))
        lines = E.open_round([a, b], 4, purge_round=4, shot_cap=3)
        self.assertTrue(lines and "PURGE" in lines[0])
        self.assertEqual((a["shots"], b["shots"]), (3, 3))

    def test_sudden_death_threshold(self):
        rows = [P(i) for i in range(6)]
        self.assertFalse(E.is_sudden_death(rows, 5))
        rows[0]["alive"] = 0
        self.assertTrue(E.is_sudden_death(rows, 5))

    def test_recommended_lives_scale_with_players(self):
        # Paul 9/12: "scale lives with the amount of people playing — recommended, not forced"
        self.assertEqual([E.recommended_lives(n) for n in (0, 3, 5, 6, 10, 15, 20, 32, 100)],
                         [2, 2, 3, 3, 4, 5, 7, 10, 10])
        # explicit lives survive create_race untouched; auto is a flag the cog acts on at start
        r = E.create_race(9191, 3, 9, settings={"lives": 5, "lives_auto": False, "min_players": 15})
        self.assertEqual((r["settings"]["lives"], r["settings"]["lives_auto"]), (5, False))
        E.update_race(r["id"], status="aborted")

    def test_purge_round_never_round_one(self):
        rng = random.Random(1)
        for n in (3, 8, 30, 200):
            for _ in range(20):
                self.assertGreaterEqual(E.pick_purge_round(rng, n), 2)

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
        self.assertEqual(E.get_race(r1["id"])["settings"]["lives"], 3)
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
        x, y = P(7), P(8)
        r3 = resolve([x, y], [dict(S(7, 8), id=21, seq=1)], rng=AlwaysBackfire())
        self.assertEqual(r3["results"][21], "backfire")
        self.assertEqual(x["lives"], 2)

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
        # purge round: three are free, the fourth is extra
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
        self.assertEqual([E.recommended_sudden_death(n) for n in (3, 6, 8, 9, 12, 15, 16, 20, 40)],
                         [2, 2, 2, 3, 3, 4, 4, 5, 5])
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


if __name__ == "__main__":
    unittest.main()
