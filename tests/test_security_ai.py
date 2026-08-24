"""Security AI — the tests that matter are the seals.

The model must never see a real identity; the output must never carry one it
wasn't given; the tiers must run exactly their pass structure; entitlements
and fair-use caps must fail closed.
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils import security_ai as sai

SUBJECT = "222222222222222222"
MATCH = "111111111111111111"
FOREIGN_ID = "999999999999999999"

SIGNALS = [
    {"class": "device_match", "band": "strong", "accounts": [MATCH]},
    {"class": "connection_anonymizer"},
    {"class": "environment_masked"},
]


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class Entitlements(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db)

    def test_no_row_means_no_tier(self):
        self.assertIsNone(sai.tier_for(1, db=self.db))

    def test_grant_and_expiry(self):
        sai.grant(1, "advanced", days=30, db=self.db, now=1000)
        self.assertEqual(sai.tier_for(1, db=self.db, now=1000 + 86400), "advanced")
        self.assertIsNone(sai.tier_for(1, db=self.db, now=1000 + 31 * 86400))

    def test_no_expiry_grant(self):
        sai.grant(2, "elite", days=0, db=self.db, now=1000)
        self.assertEqual(sai.tier_for(2, db=self.db, now=10**12), "elite")

    def test_regrant_overwrites(self):
        sai.grant(3, "standard", days=0, db=self.db)
        sai.grant(3, "elite", days=0, db=self.db)
        self.assertEqual(sai.tier_for(3, db=self.db), "elite")

    def test_revoke(self):
        sai.grant(4, "standard", days=0, db=self.db)
        sai.revoke(4, db=self.db)
        self.assertIsNone(sai.tier_for(4, db=self.db))

    def test_unknown_tier_refused(self):
        with self.assertRaises(ValueError):
            sai.grant(5, "platinum", days=0, db=self.db)

    def test_case_cap_fails_closed(self):
        cap = sai.CASE_CAPS["standard"]
        for _ in range(cap):
            self.assertTrue(sai.take_case(6, "standard", db=self.db, now=1000))
        self.assertFalse(sai.take_case(6, "standard", db=self.db, now=1000))
        # a new month starts a new budget
        self.assertTrue(sai.take_case(6, "standard", db=self.db, now=1000 + 32 * 86400))

    def test_unknown_tier_cap_is_zero(self):
        self.assertFalse(sai.take_case(7, "nope", db=self.db))


class CaseBuilding(unittest.TestCase):
    """The input seal: the model sees placeholders and enums, nothing else."""

    def test_no_raw_ids_in_case_text(self):
        text, mapping = sai.build_case(SUBJECT, SIGNALS, "hold")
        self.assertNotIn(SUBJECT, text)
        self.assertNotIn(MATCH, text)
        self.assertIn("Account A", text)
        self.assertIn("Account B", text)

    def test_mapping_covers_subject_and_matches(self):
        _, mapping = sai.build_case(SUBJECT, SIGNALS, "hold")
        self.assertEqual(mapping["A"], SUBJECT)
        self.assertEqual(mapping["B"], MATCH)

    def test_unknown_signal_class_dropped(self):
        text, _ = sai.build_case(SUBJECT, [{"class": "brand_new_thing", "accounts": [FOREIGN_ID]}], "hold")
        self.assertNotIn(FOREIGN_ID, text)
        self.assertNotIn("brand_new_thing", text)

    def test_local_facts_render_and_nothing_else(self):
        text, _ = sai.build_case(SUBJECT, SIGNALS, "hold",
                                 {"matched_account_banned_here": True,
                                  "matched_account_cleared": True,
                                  "some_random_key": True})
        self.assertIn("ban list", text)
        self.assertIn("released by this server's moderators", text)
        self.assertNotIn("some_random_key", text)

    def test_same_account_in_two_signals_gets_one_placeholder(self):
        sigs = [{"class": "device_match", "band": "strong", "accounts": [MATCH]},
                {"class": "device_near_miss", "band": "weak", "accounts": [MATCH]}]
        _, mapping = sai.build_case(SUBJECT, sigs, "hold")
        self.assertEqual(len(mapping), 2)  # A=subject, B=match — not C

    def test_signal_lines_render_mentions(self):
        lines = sai.signal_lines(SIGNALS)
        self.assertIn(f"<@{MATCH}>", lines[0])
        self.assertEqual(len(lines), 3)


class OutputLint(unittest.TestCase):
    """The output seal."""

    MAP = {"A": SUBJECT, "B": MATCH}

    def test_known_placeholder_becomes_mention(self):
        out, flagged = sai.lint_output("Account A matches Account B.", self.MAP)
        self.assertIn(f"<@{SUBJECT}>", out)
        self.assertIn(f"<@{MATCH}>", out)
        self.assertFalse(flagged)

    def test_hallucinated_placeholder_flagged(self):
        out, flagged = sai.lint_output("Account Z did it.", self.MAP)
        self.assertNotIn("Account Z", out)
        self.assertTrue(flagged)

    def test_ip_and_hex_stripped(self):
        out, flagged = sai.lint_output("from 73.150.32.143 hash deadbeefdeadbeef", self.MAP)
        self.assertNotIn("73.150.32.143", out)
        self.assertNotIn("deadbeefdeadbeef", out)
        self.assertTrue(flagged)

    def test_foreign_discord_id_stripped_allowlisted_kept(self):
        out, flagged = sai.lint_output(f"ids {FOREIGN_ID} and {MATCH}", self.MAP)
        self.assertNotIn(FOREIGN_ID, out)
        self.assertIn(MATCH, out)
        self.assertTrue(flagged)

    def test_empty_output_safe(self):
        out, flagged = sai.lint_output(None, self.MAP)
        self.assertEqual(out, "")


class TierOrchestration(unittest.TestCase):
    """Pass structure per tier, with a scripted provider."""

    def _chat(self, replies):
        calls = []

        async def chat(system, user):
            calls.append((system, user))
            return replies[len(calls) - 1]
        return chat, calls

    def test_standard_is_one_pass(self):
        chat, calls = self._chat(["Looks bad.\nRecommendation: HOLD — matched device."])
        text, passes, rec = run(sai.assess(chat, "standard", "case"))
        self.assertEqual(passes, 1)
        self.assertEqual(rec, "HOLD")

    def test_advanced_stops_after_release(self):
        chat, calls = self._chat(["Fine.\nRecommendation: RELEASE — generic hardware."])
        text, passes, rec = run(sai.assess(chat, "advanced", "case"))
        self.assertEqual(passes, 1)
        self.assertEqual(rec, "RELEASE")

    def test_advanced_double_pass_on_hold_and_skeptic_rules(self):
        chat, calls = self._chat([
            "Suspicious.\nRecommendation: HOLD — device match.",
            "Actually a household shape.\nRecommendation: RELEASE — weak corroboration.",
        ])
        text, passes, rec = run(sai.assess(chat, "advanced", "case"))
        self.assertEqual(passes, 2)
        self.assertEqual(rec, "RELEASE")
        self.assertIn("Adversarial second pass", text)
        # the skeptic saw the draft
        self.assertIn("Draft assessment", calls[1][1])

    def test_elite_is_three_passes_judge_rules(self):
        chat, calls = self._chat([
            "Same person because device.",
            "Innocent because CGNAT.",
            "Weighed both.\nRecommendation: HOLD — local ban match.",
        ])
        text, passes, rec = run(sai.assess(chat, "elite", "case"))
        self.assertEqual(passes, 3)
        self.assertEqual(rec, "HOLD")
        self.assertIn("For evasion", text)
        self.assertIn("For innocence", text)
        # the judge saw both arguments
        self.assertIn("CGNAT", calls[2][1])

    def test_unknown_tier_raises(self):
        chat, _ = self._chat([])
        with self.assertRaises(ValueError):
            run(sai.assess(chat, "free", "case"))

    def test_recommendation_extraction_case_insensitive(self):
        self.assertEqual(sai.recommendation_in("recommendation: hold — x"), "HOLD")
        self.assertIsNone(sai.recommendation_in("no verdict line"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
