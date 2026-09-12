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
             patch=0, medkit=0, skip_round=None)
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


def resolve(rows, shots, round_no=1, rng=None, sudden=False, storm=False, msgs=None, afk=False, **kw):
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

    def test_shot_at_a_corpse_is_wasted(self):
        a, b, c = P(1), P(2, lives=1), P(3)
        r = resolve([a, b, c], [S(1, 2), S(3, 2)])
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
        resolve([a, b], [S(1, 2)], sudden=True)
        self.assertEqual(b["lives"], 2)
        self.assertEqual(b["shield"], 1)      # not consumed either

    def test_second_shield_grant_becomes_a_shot(self):
        p = P(1, shield=1, shots=1)
        E.grant(p, "shield", shot_cap=3)
        self.assertEqual(p["shield"], 1)
        self.assertEqual(p["shots"], 2)


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
        self.assertEqual(sorted(common), ["medkit", "overload", "transfuse"])
        self.assertEqual(sorted(rare), ["patch", "shield", "shot"])
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
        self.assertEqual(min(E.SUPER_WEIGHTS, key=E.SUPER_WEIGHTS.get), "goldapple")
        self.assertEqual(set(E.SUPER_WEIGHTS), set(E.SUPER))
        rng = random.Random(11)
        n = sum(E.roll_drop(rng, E.SUPER_WEIGHTS) == "goldapple" for _ in range(5000))
        self.assertTrue(350 < n < 650, n)                # ≈ 1 in 10
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
    def test_kill_pays_a_shield_or_a_shot_when_held(self):
        a, b, c = P(1), P(2, lives=1), P(3, lives=1)
        resolve([a, b, c], [S(1, 2), S(2, 1)])       # b votes, so the kill pays
        self.assertEqual(a["shield"], 1)
        shots = a["shots"]
        resolve([a, c], [S(1, 3), S(3, 3)], round_no=2)   # c votes (self-shot: can't pop a's shield)
        self.assertEqual(a["shield"], 1)
        self.assertEqual(a["shots"], shots + 1)

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
        r = resolve([a, b, c], [S(1, 3)], afk=True)
        self.assertEqual(a["lives"], 3)          # voted
        self.assertEqual(b["lives"], 2)          # afk, shield untouched
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
        r = resolve([a, b, c], [], round_no=2, storm=True, msgs={"1": 0, "2": 5, "3": 9})
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
    def test_open_round_banks_one_shot_up_to_cap_and_purge_gives_three(self):
        a, b, dead = P(1, shots=0), P(2, shots=3), P(3, alive=0, shots=0)
        self.assertEqual(E.open_round([a, b, dead], 1, purge_round=4, shot_cap=3), [])
        self.assertEqual((a["shots"], b["shots"], dead["shots"]), (1, 3, 0))
        lines = E.open_round([a, b], 4, purge_round=4, shot_cap=3)
        self.assertTrue(lines and "PURGE" in lines[0])
        self.assertEqual((a["shots"], b["shots"]), (3, 3))

    def test_sudden_death_threshold(self):
        rows = [P(i) for i in range(6)]
        self.assertFalse(E.is_sudden_death(rows, 5))
        rows[0]["alive"] = 0
        self.assertTrue(E.is_sudden_death(rows, 5))

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


if __name__ == "__main__":
    unittest.main()
