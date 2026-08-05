"""Outward-facing URLs (utils/links.py).

These strings are pasted into every help embed and the new-server greeting. A
wrong path or a permission drift here isn't a crash — it's a server owner
hitting a 404 on their first interaction with the bot, so it gets a test.
"""
import os
import sys
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import links  # noqa: E402


class LinkTests(unittest.TestCase):

    def test_dashboard_deep_link_uses_the_real_route(self):
        # the dashboard serves /g/<gid>; /guild/<gid> would 404
        self.assertEqual(links.dashboard_url(1215140346800119868),
                         "https://dashboard.torvex.app/g/1215140346800119868")

    def test_dashboard_without_a_guild_is_the_bare_host(self):
        self.assertEqual(links.dashboard_url(), "https://dashboard.torvex.app")
        self.assertFalse(links.dashboard_url().endswith("/"))

    def test_invite_never_requests_administrator(self):
        perms = int(parse_qs(urlparse(links.invite_url()).query)["permissions"][0])
        self.assertEqual(perms & 0x8, 0, "invite must not ask for Administrator")

    def test_invite_keeps_both_scopes(self):
        scope = parse_qs(urlparse(links.invite_url()).query)["scope"][0]
        self.assertIn("bot", scope.split())
        self.assertIn("applications.commands", scope.split())

    def test_invite_deep_links_to_a_guild_when_given_one(self):
        q = parse_qs(urlparse(links.invite_url(42)).query)
        self.assertEqual(q["guild_id"], ["42"])
        self.assertNotIn("guild_id", parse_qs(urlparse(links.invite_url()).query))

    def test_every_public_url_is_https(self):
        for url in (links.DASHBOARD_URL, links.SITE_URL,
                    links.SUPPORT_INVITE, links.INVITE_URL):
            self.assertTrue(url.startswith("https://"), url)


class ConfigViewTests(unittest.TestCase):

    def test_view_always_offers_the_dashboard_and_support(self):
        labels = [b.label for b in links.config_view(7).children]
        self.assertEqual(labels, ["Open the Dashboard", "Support server"])

    def test_invite_button_is_opt_in(self):
        labels = [b.label for b in links.config_view(7, invite=True).children]
        self.assertIn("Add to your server", labels)

    def test_buttons_carry_guild_scoped_urls(self):
        urls = [b.url for b in links.config_view(7, invite=True).children]
        self.assertTrue(any("/g/7" in u for u in urls))
        self.assertTrue(any("guild_id=7" in u for u in urls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
