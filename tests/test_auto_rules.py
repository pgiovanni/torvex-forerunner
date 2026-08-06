"""Automation-rule engine harness — exercises the REAL evaluator, no live Discord.

`check_condition`, `rule_matches`, `make_ctx` and `render` are pure, so this
imports the cog module and calls them directly.

Focus is the direction of failure. This is the one feature where a server admin
can, from a web form, make the bot ban someone — so what's checked here is not
"does the happy path work" but "does everything unknown, malformed or missing
resolve to NOT firing". A rule that fails to run is an annoyance; a rule that
runs when it shouldn't is a server.

Run:
    /opt/peepos-reclaimer/venv/bin/python tests/test_auto_rules.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.auto_rules as ar  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


def ctx(**kw):
    base = dict(user_id=42, is_bot=False, roles=[100, 200])
    base.update(kw)
    return ar.make_ctx(**base)


def rule(conds, match="all", **kw):
    r = {"id": "t", "name": "t", "on": 1, "trigger": "message",
         "match": match, "conditions": conds, "actions": [{"t": "delete"}]}
    r.update(kw)
    return r


# ── 1. fail-closed: anything unrecognised must be False ──────────────────────
check("unknown condition type is False",
      ar.check_condition({"t": "from_the_future", "v": 1}, ctx()) is False)

check("NOT of an unknown type is still False — inversion can't manufacture a match",
      ar.check_condition({"t": "from_the_future", "v": 1, "not": 1}, ctx()) is False)

check("garbage value doesn't raise, just fails",
      ar.check_condition({"t": "length_gte", "v": "not a number"}, ctx()) is False)

check("missing value doesn't raise",
      ar.check_condition({"t": "mentions_gte"}, ctx()) is False)

check("condition with no type is False",
      ar.check_condition({}, ctx()) is False)

# ── 2. the big one: a rule with no conditions never matches ──────────────────
check("no conditions -> no match (ALL)", ar.rule_matches(rule([]), ctx()) is False)
check("no conditions -> no match (ANY)",
      ar.rule_matches(rule([], match="any"), ctx()) is False)
check("conditions key missing entirely -> no match",
      ar.rule_matches({"id": "x", "match": "all"}, ctx()) is False)

# ── 3. AND / OR ──────────────────────────────────────────────────────────────
has_role_100 = {"t": "has_role", "v": ["100"]}
has_role_999 = {"t": "has_role", "v": ["999"]}

check("ALL: both true -> match",
      ar.rule_matches(rule([has_role_100, {"t": "is_bot", "not": 1}]), ctx()) is True)
check("ALL: one false -> no match",
      ar.rule_matches(rule([has_role_100, has_role_999]), ctx()) is False)
check("ANY: one true -> match",
      ar.rule_matches(rule([has_role_100, has_role_999], match="any"), ctx()) is True)
check("ANY: none true -> no match",
      ar.rule_matches(rule([has_role_999], match="any"), ctx()) is False)
check("unknown match mode defaults to ALL, not ANY",
      ar.rule_matches(rule([has_role_100, has_role_999], match="???"), ctx()) is False)

# ── 4. int/str id comparison (the classic silent-never-matches bug) ──────────
check("int ids in ctx match string ids in the rule",
      ar.check_condition({"t": "has_role", "v": ["100"]}, ctx(roles=[100])) is True)
check("string ids in ctx match int ids in the rule",
      ar.check_condition({"t": "has_role", "v": [100]}, ctx(roles=["100"])) is True)
check("is_user compares as strings",
      ar.check_condition({"t": "is_user", "v": ["42"]}, ctx(user_id=42)) is True)
check("role_is compares as strings",
      ar.check_condition({"t": "role_is", "v": [55]}, ctx(role_id="55")) is True)
check("in_channel compares as strings",
      ar.check_condition({"t": "in_channel", "v": ["900"]}, ctx(channel_id=900)) is True)

# ── 5. content matching ──────────────────────────────────────────────────────
c = ctx(content="Hey check out DISCORD.GG/abcd right now")
check("contains is case-insensitive",
      ar.check_condition({"t": "content_contains", "v": ["check out"]}, c) is True)
check("contains: no false hit",
      ar.check_condition({"t": "content_contains", "v": ["nonsense"]}, c) is False)
check("invite detected regardless of case",
      ar.check_condition({"t": "has_invite", "v": None}, c) is True)
check("link detected on a bare domain",
      ar.check_condition({"t": "has_link", "v": None},
                         ctx(content="go to example.com/x")) is True)
check("plain prose is not a link",
      ar.check_condition({"t": "has_link", "v": None},
                         ctx(content="i went home. then i slept")) is False)
check("an email is not treated as a link",
      ar.check_condition({"t": "has_link", "v": None},
                         ctx(content="mail me at bob@example.com")) is False)
check("equals is exact after trim/case",
      ar.check_condition({"t": "content_equals", "v": ["Hello"]},
                         ctx(content="  hello ")) is True)
check("equals does not match a substring",
      ar.check_condition({"t": "content_equals", "v": ["hello"]},
                         ctx(content="hello there")) is False)
check("starts_with works",
      ar.check_condition({"t": "content_starts", "v": ["!rank"]},
                         ctx(content="!RANK me")) is True)
check("length_gte counts the real content",
      ar.check_condition({"t": "length_gte", "v": 5}, ctx(content="12345")) is True)
check("empty content doesn't accidentally match contains",
      ar.check_condition({"t": "content_contains", "v": ["x"]}, ctx(content="")) is False)

# ── 6. inversion ─────────────────────────────────────────────────────────────
check("NOT flips a true condition",
      ar.check_condition({"t": "has_role", "v": ["100"], "not": 1}, ctx()) is False)
check("NOT flips a false condition",
      ar.check_condition({"t": "has_role", "v": ["999"], "not": 1}, ctx()) is True)

# ── 7. account age ───────────────────────────────────────────────────────────
check("account_new true for a young account",
      ar.check_condition({"t": "account_new", "v": 7}, ctx(account_days=2.5)) is True)
check("account_new false for an old one",
      ar.check_condition({"t": "account_new", "v": 7}, ctx(account_days=400)) is False)
check("account_new false when the age is unknown — never guess against a member",
      ar.check_condition({"t": "account_new", "v": 7}, ctx(account_days=None)) is False)

# ── 8. emoji ─────────────────────────────────────────────────────────────────
check("standard emoji matches", ar._emoji_eq("🎨", "🎨") is True)
check("custom emoji matches by name, colons ignored",
      ar._emoji_eq(":peepoHappy:", "peepoHappy") is True)
check("different emoji don't match", ar._emoji_eq("🎨", "🔥") is False)
check("emoji_is is False when the event carries none",
      ar.check_condition({"t": "emoji_is", "v": ["🎨"]}, ctx(emoji=None)) is False)

# ── 9. render: a template must not reach attributes or ping everyone ─────────
class FakeGuild:
    name = "Torvex"
    member_count = 100


class FakeMember:
    mention = "<@42>"
    display_name = "Bob"
    name = "bob"


out = ar.render("hi {mention} in {server} #{count}", FakeMember(), FakeGuild())
check("tokens substitute", out == "hi <@42> in Torvex #100")
check("an unknown token is left alone, not evaluated",
      ar.render("{guild.owner.id}", FakeMember(), FakeGuild()) == "{guild.owner.id}")
check("format-style injection is inert",
      ar.render("{0.__class__}", FakeMember(), FakeGuild()) == "{0.__class__}")
check("render is length-capped",
      len(ar.render("x" * 5000, FakeMember(), FakeGuild())) == 2000)
check("render survives a None template",
      ar.render(None, FakeMember(), FakeGuild()) == "")

# ── 10. the safety constants are actually set ────────────────────────────────
check("punitive set is the three that remove or silence someone",
      ar.PUNITIVE == {"kick", "ban", "timeout"})
check("breaker cap is small enough to matter", 0 < ar.BREAKER_MAX <= 10)
check("breaker window is a real window", ar.BREAKER_WINDOW >= 30)
check("every condition the dashboard can save has an implementation here",
      set(ar.CHECKS) >= {
          "has_role", "is_user", "is_bot", "account_new", "in_channel", "in_voice",
          "content_contains", "content_equals", "content_starts", "has_attachment",
          "has_image", "has_link", "has_invite", "mentions_gte", "length_gte",
          "role_is", "emoji_is"})

print(f"\n{_total - len(_fails)}/{_total} passed")
if _fails:
    print("FAILED: " + ", ".join(_fails))
    sys.exit(1)
