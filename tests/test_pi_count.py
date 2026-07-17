import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cogs.pi_count import compute_pi_digits, normalize  # noqa: E402

PI_100 = ("3141592653589793238462643383279502884197169399375105820974944592307816406286"
          "208998628034825342117067")


class TestComputePiDigits(unittest.TestCase):
    def test_first_100_digits(self):
        self.assertEqual(compute_pi_digits(100), PI_100)

    def test_short(self):
        self.assertEqual(compute_pi_digits(1), "3")
        self.assertEqual(compute_pi_digits(5), "31415")

    def test_tail_is_correct_when_extended(self):
        # the last digits of a longer run must agree with a shorter prefix
        self.assertTrue(compute_pi_digits(1000).startswith(compute_pi_digits(500)))


class TestNormalize(unittest.TestCase):
    def test_plain_digit(self):
        self.assertEqual(normalize("9"), "9")

    def test_leading_pi_with_point(self):
        self.assertEqual(normalize("3.14"), "314")

    def test_bare_point_prefix(self):
        self.assertEqual(normalize(".1"), "1")

    def test_spacing_and_commas(self):
        self.assertEqual(normalize("3.141 592,653"), "3141592653")

    def test_chat_rejected(self):
        self.assertIsNone(normalize("lol"))
        self.assertIsNone(normalize("that's all i know"))
        self.assertIsNone(normalize("digits of pi are 22/7"))

    def test_emoji_rejected(self):
        self.assertIsNone(normalize("<a:Plotge:1121140751808598016>"))
        self.assertIsNone(normalize("They are😭"))

    def test_empty_rejected(self):
        self.assertIsNone(normalize(""))
        self.assertIsNone(normalize(" . "))

    def test_unicode_digits_rejected(self):
        self.assertIsNone(normalize("１２"))  # fullwidth １２

    def test_negative_and_mixed_rejected(self):
        self.assertIsNone(normalize("-3"))
        self.assertIsNone(normalize("3a"))


class TestChainMatching(unittest.TestCase):
    """Replays the validation rule the cog applies (pure string comparison)."""

    def _replay(self, contents):
        pi = compute_pi_digits(200)
        pos, rejected = 0, []
        for content in contents:
            norm = normalize(content)
            if norm and pi[pos:pos + len(norm)] == norm:
                pos += len(norm)
            else:
                rejected.append(content)
        return pos, rejected

    def test_real_channel_opening(self):
        # exactly how the live channel started (mrdudebro1 + elxox07 alternating)
        pos, rejected = self._replay(["3", ".1", "4", "1", "5", "9", "2", "6", "5", "3", "lol"])
        self.assertEqual(pos, 10)
        self.assertEqual(rejected, ["lol"])

    def test_multi_digit_messages(self):
        pos, rejected = self._replay(["3.14", "15926535", "8979"])
        self.assertEqual(pos, 15)
        self.assertEqual(rejected, [])

    def test_wrong_digit_rejected_and_position_holds(self):
        pos, rejected = self._replay(["3", "1", "5"])  # 5 is wrong (expects 4)
        self.assertEqual(pos, 2)
        self.assertEqual(rejected, ["5"])

    def test_restart_attempt_rejected(self):
        # hehe_.00's "3.14" mid-chain must not reset anything
        pos, rejected = self._replay(["3", "1", "4", "1", "3.14"])
        self.assertEqual(pos, 4)
        self.assertEqual(rejected, ["3.14"])


if __name__ == "__main__":
    unittest.main()
