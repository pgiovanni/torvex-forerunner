"""Reaction-role templates + per-panel exclusivity.

Templates are the one-command path for a brand new server, so the data itself is
the thing most likely to be wrong: a duplicate name silently collapses two
buttons, more than 25 buttons won't post at all, and a template that claims to be
pick-one but isn't marked exclusive hands members contradictory roles.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.role_templates import MAX_BUTTONS, TEMPLATES, template_choices  # noqa: E402


class TemplateDataTests(unittest.TestCase):

    def test_every_template_fits_on_one_message(self):
        for key, t in TEMPLATES.items():
            self.assertLessEqual(len(t["roles"]), MAX_BUTTONS, key)

    def test_no_duplicate_role_names_within_a_template(self):
        for key, t in TEMPLATES.items():
            names = [r[0].casefold() for r in t["roles"]]
            self.assertEqual(len(names), len(set(names)), key)

    def test_pick_one_sets_are_marked_exclusive(self):
        # a member has one age, one name colour and one DM preference
        for key in ("age", "colours", "dms"):
            self.assertTrue(TEMPLATES[key]["exclusive"], f"{key} must be pick-one")

    def test_multi_pick_sets_are_not_exclusive(self):
        for key in ("pronouns", "regions", "notifications", "platforms"):
            self.assertFalse(TEMPLATES[key]["exclusive"], f"{key} must allow several")

    def test_colours_are_valid_and_roles_are_named(self):
        for key, t in TEMPLATES.items():
            for name, emoji, colour in t["roles"]:
                self.assertTrue(name.strip(), key)
                self.assertTrue(emoji, f"{key}/{name} needs an emoji")
                self.assertTrue(0 <= colour <= 0xFFFFFF, f"{key}/{name}")

    def test_choices_cover_every_template_and_fit_discord(self):
        keys = [k for k, _ in template_choices()]
        self.assertEqual(sorted(keys), sorted(TEMPLATES))
        self.assertLessEqual(len(keys), 25, "slash choices cap at 25")

    def test_titles_and_blurbs_are_present(self):
        for key, t in TEMPLATES.items():
            self.assertTrue(t["title"].strip(), key)
            self.assertLessEqual(len(t["blurb"]), 4096, key)

    def test_full_palette_is_exactly_the_button_cap_and_pick_one(self):
        # the whole point of the set: as many colours as ONE panel can hold
        t = TEMPLATES["colours_full"]
        self.assertEqual(len(t["roles"]), MAX_BUTTONS)
        self.assertTrue(t["exclusive"])

    def test_sexuality_allows_several(self):
        self.assertFalse(TEMPLATES["sexuality"]["exclusive"])

    def test_json_export_mirrors_the_templates(self):
        # the dashboard builds its "start from a template" picker from this
        from utils.role_templates import as_json
        import json
        j = as_json()
        self.assertEqual(sorted(j), sorted(TEMPLATES))
        for key, t in TEMPLATES.items():
            self.assertEqual(j[key]["exclusive"], t["exclusive"])
            self.assertEqual([r["name"] for r in j[key]["roles"]], [r[0] for r in t["roles"]])
            self.assertEqual([r["colour"] for r in j[key]["roles"]], [r[2] for r in t["roles"]])
        json.dumps(j)          # must be plain data


class PlanTests(unittest.TestCase):
    """plan_template_role decides create / reuse / blocked."""

    def setUp(self):
        import cogs.role_menu as rm
        self.plan = rm.plan_template_role

    def test_missing_role_is_created(self):
        self.assertEqual(self.plan(None, _R(10)), "create")

    def test_existing_role_below_the_bot_is_reused(self):
        self.assertEqual(self.plan(_R(3), _R(10)), "reuse")

    def test_existing_role_above_the_bot_is_blocked(self):
        # would render a button the bot can never fulfil
        self.assertEqual(self.plan(_R(11), _R(10)), "blocked")

    def test_role_equal_to_the_bots_top_role_is_blocked(self):
        # Discord requires STRICTLY higher to assign
        self.assertEqual(self.plan(_R(10), _R(10)), "blocked")


class _R:
    """Minimal stand-in ordered like discord.Role."""
    def __init__(self, position):
        self.position = position

    def __ge__(self, other):
        return self.position >= other.position


class EmojiTests(unittest.TestCase):
    """parse_emoji: any emoji they like, but never one that breaks the panel."""

    def setUp(self):
        import cogs.role_menu as rm
        self.parse = rm.parse_emoji
        # the bot is in the server owning 123..., not the one owning 999...
        self.known = {123456789012345678: "<a:spin:123456789012345678>"}
        self.resolve = self.known.get

    def test_blank_means_no_emoji_and_is_not_an_error(self):
        for raw in (None, "", "   "):
            value, err = self.parse(raw, self.resolve)
            self.assertIsNone(value)
            self.assertIsNone(err)

    def test_standard_emoji_passes_through(self):
        self.assertEqual(self.parse("🎉", self.resolve), ("🎉", None))

    def test_multi_codepoint_emoji_survives(self):
        # flags and ZWJ sequences are several codepoints; truncating breaks them
        for e in ("🏳️‍🌈", "👍🏽", "🇬🇧"):
            value, err = self.parse(e, self.resolve)
            self.assertEqual(value, e)
            self.assertIsNone(err)

    def test_custom_emoji_the_bot_can_use_is_accepted(self):
        value, err = self.parse("<:spin:123456789012345678>", self.resolve)
        self.assertIsNone(err)
        self.assertEqual(value, "<a:spin:123456789012345678>",
                         "should store the resolved form, fixing the animated flag")

    def test_bare_id_from_developer_mode_works(self):
        value, err = self.parse("123456789012345678", self.resolve)
        self.assertEqual(value, "<a:spin:123456789012345678>")
        self.assertIsNone(err)

    def test_custom_emoji_from_a_server_we_are_not_in_is_refused(self):
        # the important one: Discord rejects the whole component, so this would
        # break every button on the panel, not just this one
        value, err = self.parse("<:nope:999999999999999999>", self.resolve)
        self.assertIsNone(value)
        self.assertIn("steal-emoji", err)

    def test_shortcode_text_is_refused_with_guidance(self):
        value, err = self.parse(":shrug:", self.resolve)
        self.assertIsNone(value)
        self.assertTrue(err)

    def test_plain_text_is_refused(self):
        for raw in ("not an emoji", "x" * 40, "lol"):
            value, err = self.parse(raw, self.resolve)
            self.assertIsNone(value, raw)
            self.assertTrue(err, raw)


class ExclusivityMigrationTests(unittest.TestCase):
    """The exclusive column is added to databases that predate it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "role_menus.db")

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, fn):
        """Open, use, commit, CLOSE. Windows won't delete a file SQLite still
        holds open, and `with sqlite3.connect(...)` commits without closing."""
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            out = fn(c)
            c.commit()
            return out
        finally:
            c.close()

    def _old_schema(self):
        def build(c):
            c.execute("""CREATE TABLE panels(
                           panel_id INTEGER PRIMARY KEY AUTOINCREMENT,
                           guild_id TEXT, channel_id TEXT, message_id TEXT,
                           title TEXT, description TEXT)""")
            c.execute("""CREATE TABLE panel_roles(
                           panel_id INTEGER, role_id TEXT, label TEXT,
                           emoji TEXT, pos INTEGER)""")
            c.execute("INSERT INTO panels(guild_id,title) VALUES('1','Old Panel')")
        self._run(build)

    def _migrate(self):
        """The cog's migration step, run against the throwaway DB."""
        def migrate(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(panels)")}
            if "exclusive" not in cols:
                c.execute("ALTER TABLE panels ADD COLUMN exclusive INTEGER DEFAULT 0")
        self._run(migrate)

    def test_existing_panels_survive_and_default_to_shared(self):
        self._old_schema()
        self._migrate()
        row = self._run(lambda c: c.execute("SELECT * FROM panels").fetchone())
        self.assertEqual(row["title"], "Old Panel")
        self.assertFalse(row["exclusive"], "old panels must not become pick-one")

    def test_panel_roles_gain_a_colour_column_for_not_yet_created_roles(self):
        import cogs.role_menu as rm
        self._old_schema()
        orig = rm.DB_PATH
        rm.DB_PATH = self.path
        try:
            rm._init()
            rm._init()         # idempotent
        finally:
            rm.DB_PATH = orig
            import gc
            gc.collect()       # `with _conn()` commits but never closes; Windows
                               # won't delete the temp dir while it's open
        cols = self._run(lambda c: [r[1] for r in c.execute("PRAGMA table_info(panel_roles)")])
        self.assertEqual(cols.count("colour"), 1)

    def test_unresolved_rows_never_become_buttons(self):
        # a dashboard row with no role yet must not render (nothing to grant)
        import cogs.role_menu as rm
        self._old_schema()
        def seed(c):
            c.execute("ALTER TABLE panel_roles ADD COLUMN colour INTEGER")
            c.execute("INSERT INTO panel_roles VALUES(1,'42','Real','🎉',0,NULL)")
            c.execute("INSERT INTO panel_roles VALUES(1,NULL,'Pending','✨',1,255)")
            return [r["label"] for r in rm._button_rows(c, 1)]
        self.assertEqual(self._run(seed), ["Real"])

    def test_migration_is_idempotent(self):
        self._old_schema()
        self._migrate()
        self._migrate()          # a restart runs it again
        cols = self._run(lambda c: [r[1] for r in c.execute("PRAGMA table_info(panels)")])
        self.assertEqual(cols.count("exclusive"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
