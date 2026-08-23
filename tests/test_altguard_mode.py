"""Per-guild AltGuard mode resolution (multi-server Phase 2a).

The assertions that matter are the refusals: `observe` must never be able to
touch a role, a remote guild must not reach an enforcing mode before the gate
speaks per-guild, and a stranger's server must never inherit the operator's
role ids.

    PYTHONIOENCODING=utf-8 python tests/test_altguard_mode.py
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from utils import altguard_mode as m  # noqa: E402

HOME = 1215140346800119868
REMOTE = 1538553728070717492
ENV_IDS = {
    "quarantine_role_id": 111,
    "verify_channel_id": 222,
    "modlog_channel_id": 333,
    "almost_role_id": 444,
    "default_role_ids": [555],
}


class ConfiguredMode(unittest.TestCase):
    def test_explicit_modes(self):
        for want in m.MODES:
            self.assertEqual(m.configured_mode({"altguard_mode": want, "altguard_enabled": 1}), want)

    def test_case_and_whitespace(self):
        self.assertEqual(m.configured_mode({"altguard_mode": " Gate ", "altguard_enabled": 1}), "gate")

    def test_disabled_is_off(self):
        self.assertEqual(m.configured_mode({}), "off")
        self.assertEqual(m.configured_mode({"altguard_enabled": 0, "altguard_mode": "gate"}), "gate",
                         "an explicit mode is honoured; enable_key gates elsewhere")

    def test_legacy_boolean_reads_as_gate(self):
        # Servers configured before the ladder existed keep what they chose.
        self.assertEqual(m.configured_mode({"altguard_enabled": 1, "quarantine_on_join": 1}), "gate")
        self.assertEqual(m.configured_mode({"altguard_enabled": 1, "quarantine_on_join": 0}), "observe")

    def test_garbage_mode_falls_back(self):
        self.assertEqual(m.configured_mode({"altguard_mode": "nuke-everyone", "altguard_enabled": 1}),
                         "observe")


class EffectiveMode(unittest.TestCase):
    def test_home_guild_gets_what_it_asks_for(self):
        cfg = {"altguard_enabled": 1, "altguard_mode": "gate"}
        self.assertEqual(m.effective_mode(HOME, cfg, HOME, remote_enforce=False), "gate")

    def test_home_guild_id_type_does_not_matter(self):
        cfg = {"altguard_enabled": 1, "altguard_mode": "gate"}
        self.assertEqual(m.effective_mode(str(HOME), cfg, HOME, remote_enforce=False), "gate")
        self.assertEqual(m.effective_mode(HOME, cfg, str(HOME), remote_enforce=False), "gate")

    def test_remote_enforcing_modes_degrade_to_observe(self):
        for want in m.ENFORCING:
            cfg = {"altguard_enabled": 1, "altguard_mode": want}
            self.assertEqual(m.effective_mode(REMOTE, cfg, HOME, remote_enforce=False), "observe",
                             f"{want} must not enforce remotely before the gate is per-guild")
            self.assertTrue(m.is_degraded(REMOTE, cfg, HOME, remote_enforce=False))

    def test_remote_enforce_flag_lifts_the_degrade(self):
        cfg = {"altguard_enabled": 1, "altguard_mode": "gate"}
        self.assertEqual(m.effective_mode(REMOTE, cfg, HOME, remote_enforce=True), "gate")
        self.assertFalse(m.is_degraded(REMOTE, cfg, HOME, remote_enforce=True))

    def test_remote_observe_is_never_degraded(self):
        cfg = {"altguard_enabled": 1, "altguard_mode": "observe"}
        self.assertEqual(m.effective_mode(REMOTE, cfg, HOME, remote_enforce=False), "observe")
        self.assertFalse(m.is_degraded(REMOTE, cfg, HOME, remote_enforce=False))

    def test_off_stays_off_everywhere(self):
        self.assertEqual(m.effective_mode(REMOTE, {}, HOME, remote_enforce=True), "off")
        self.assertEqual(m.effective_mode(HOME, {"altguard_mode": "off"}, HOME), "off")


class Capabilities(unittest.TestCase):
    def test_observe_never_touches_roles(self):
        self.assertFalse(m.acts_on_roles("observe"))
        self.assertFalse(m.holds_on_join("observe"))

    def test_off_never_touches_roles(self):
        self.assertFalse(m.acts_on_roles("off"))
        self.assertFalse(m.holds_on_join("off"))

    def test_assist_acts_but_does_not_hold_at_the_door(self):
        self.assertTrue(m.acts_on_roles("assist"))
        self.assertFalse(m.holds_on_join("assist"),
                         "assist screens AFTER the partner bot's grant")

    def test_gate_holds(self):
        self.assertTrue(m.acts_on_roles("gate"))
        self.assertTrue(m.holds_on_join("gate"))


class ResolveIds(unittest.TestCase):
    def test_home_uses_the_operator_environment(self):
        cfg = {"quarantine_role_id": 999, "modlog_channel_id": 888}
        self.assertEqual(m.resolve_ids(HOME, cfg, HOME, ENV_IDS), ENV_IDS)

    def test_home_config_cannot_repoint_the_operators_roles(self):
        got = m.resolve_ids(HOME, {"quarantine_role_id": 666}, HOME, ENV_IDS)
        self.assertEqual(got["quarantine_role_id"], 111)

    def test_remote_uses_its_own_config_only(self):
        cfg = {"quarantine_role_id": "777", "modlog_channel_id": "778",
               "verify_channel_id": 779, "altguard_default_roles": ["1", "x", 2]}
        got = m.resolve_ids(REMOTE, cfg, HOME, ENV_IDS)
        self.assertEqual(got["quarantine_role_id"], 777)
        self.assertEqual(got["modlog_channel_id"], 778)
        self.assertEqual(got["verify_channel_id"], 779)
        self.assertEqual(got["default_role_ids"], [1, 2], "junk ids dropped, not crashed on")

    def test_remote_never_inherits_home_ids(self):
        got = m.resolve_ids(REMOTE, {}, HOME, ENV_IDS)
        for k, v in got.items():
            self.assertIn(v, (None, []), f"{k} leaked a home id into a remote guild")

    def test_altguard_modlog_overrides_the_shared_one(self):
        cfg = {"modlog_channel_id": 1, "altguard_modlog_channel_id": 2}
        self.assertEqual(m.resolve_ids(REMOTE, cfg, HOME, ENV_IDS)["modlog_channel_id"], 2)


class Requirements(unittest.TestCase):
    def test_enforcing_mode_needs_a_quarantine_role(self):
        missing = m.missing_requirements("gate", {"modlog_channel_id": 5})
        self.assertIn("a quarantine role", missing)

    def test_observe_needs_only_somewhere_to_report(self):
        self.assertEqual(m.missing_requirements("observe", {"modlog_channel_id": 5}), [])
        self.assertIn("a mod-log channel to report to", m.missing_requirements("observe", {}))

    def test_off_requires_nothing(self):
        self.assertEqual(m.missing_requirements("off", {}), [])

    def test_fully_configured_gate_is_satisfied(self):
        self.assertEqual(m.missing_requirements("gate", ENV_IDS), [])


class PartnerRoles(unittest.TestCase):
    def test_mixed_types_and_junk(self):
        self.assertEqual(m.partner_role_ids({"partner_roles": ["1", 2, " 3 ", "x", None]}), {1, 2, 3})

    def test_absent_is_empty(self):
        self.assertEqual(m.partner_role_ids({}), set())


class JoinRiskSignals(unittest.TestCase):
    def test_ordinary_member_produces_nothing(self):
        self.assertEqual(m.join_risk_signals(400, True, 1, 60), [],
                         "observe must stay silent on normal joins, not post on every member")

    def test_new_account(self):
        self.assertIn("account only **2d** old", m.join_risk_signals(2, True, 1, 60))

    def test_no_avatar(self):
        self.assertIn("no profile picture", m.join_risk_signals(400, False, 1, 60))

    def test_unknown_avatar_state_is_not_reported(self):
        self.assertEqual(m.join_risk_signals(400, None, 1, 60), [])

    def test_join_burst(self):
        got = m.join_risk_signals(400, True, 6, 60)
        self.assertEqual(len(got), 1)
        self.assertIn("possible raid", got[0])

    def test_burst_threshold_is_inclusive(self):
        self.assertEqual(m.join_risk_signals(400, True, 4, 60, burst_threshold=5), [])
        self.assertTrue(m.join_risk_signals(400, True, 5, 60, burst_threshold=5))

    def test_signals_stack(self):
        self.assertEqual(len(m.join_risk_signals(1, False, 9, 60)), 3)

    def test_missing_age_is_not_reported(self):
        self.assertEqual(m.join_risk_signals(None, True, 1, 60), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
