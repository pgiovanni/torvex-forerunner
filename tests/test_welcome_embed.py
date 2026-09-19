"""Welcome card — cogs/automation.build_welcome and its two validators.

The welcome is the one message a server shows every single member exactly once,
and every failure mode here is silent: a bad colour, an `attachment://` image or
a 257-character title is a 400 on the whole send, and `_send` swallows it. So
the builder is pure and these are its unit tests.

Run on any box with discord.py importable:
    /opt/peepos-reclaimer/venv/bin/python tests/test_welcome_embed.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.automation as auto  # noqa: E402

_fails = []
_total = 0


def check(name, got, want):
    global _total
    _total += 1
    if got != want:
        _fails.append(f"{name}: got {got!r}, want {want!r}")


class _Avatar:
    url = "https://cdn.discordapp.com/avatars/42/abc.png"


class _Guild:
    id = 1
    name = "Movie freakss"
    member_count = 2405


class _Member:
    id = 42
    mention = "<@42>"
    display_name = "starlght"
    name = "starlght0476"
    guild = _Guild()
    display_avatar = _Avatar()


M = _Member()
PLAIN = {"welcome_message": "Heyy {mention}, welcome to {server}! You are #{count}."}
CARD = dict(PLAIN, welcome_embed=1, welcome_embed_title="Welcome, {user}!",
            welcome_embed_color="#57F287",
            welcome_embed_image="https://example.com/pepe.gif",
            welcome_embed_footer="Member #{count}")


# ── colours: a typo must never cost the welcome ─────────────────────────
check("colour: #rrggbb", auto.parse_color("#57F287"), 0x57F287)
check("colour: bare hex", auto.parse_color("57f287"), 0x57F287)
check("colour: #rgb shorthand", auto.parse_color("#f0c"), 0xFF00CC)
check("colour: nonsense falls back", auto.parse_color("blurple please"), auto.EMBED_FALLBACK_COLOR)
check("colour: empty falls back", auto.parse_color(""), auto.EMBED_FALLBACK_COLOR)
check("colour: None falls back", auto.parse_color(None), auto.EMBED_FALLBACK_COLOR)
check("colour: out of range falls back", auto.parse_color("1000000"), auto.EMBED_FALLBACK_COLOR)

# ── images: only what Discord will actually fetch ─────────────────────
check("image: https kept", auto.safe_image_url("https://a.test/x.gif"), "https://a.test/x.gif")
check("image: http kept", auto.safe_image_url("http://a.test/x.gif"), "http://a.test/x.gif")
check("image: whitespace trimmed", auto.safe_image_url("  https://a.test/x.gif "), "https://a.test/x.gif")
check("image: attachment:// dropped", auto.safe_image_url("attachment://x.gif"), "")
check("image: data: dropped", auto.safe_image_url("data:image/png;base64,AAAA"), "")
check("image: javascript: dropped", auto.safe_image_url("javascript:alert(1)"), "")
check("image: empty stays empty", auto.safe_image_url(""), "")

# ── plain mode is unchanged by the card work ───────────────────────
content, embed = auto.build_welcome(PLAIN, M)
check("plain: no embed", embed, None)
check("plain: tokens substituted", content,
      "Heyy <@42>, welcome to Movie freakss! You are #2405.")
check("plain: ping is the template's own {mention}, not a second one",
      auto.build_welcome(dict(PLAIN, welcome_ping=1), M)[0].count("<@42>"), 1)
check("plain: empty message posts nothing", auto.build_welcome({}, M), ("", None))

# ── card mode ──────────────────────────────────────────
content, e = auto.build_welcome(CARD, M)
check("card: ping rides in the content, where it actually notifies", content, "<@42>")
check("card: title rendered", e.title, "Welcome, starlght!")
check("card: body becomes the description", e.description,
      "Heyy <@42>, welcome to Movie freakss! You are #2405.")
check("card: colour applied", e.colour.value, 0x57F287)
check("card: image set", e.image.url, "https://example.com/pepe.gif")
check("card: footer rendered", e.footer.text, "Member #2405")
check("card: avatar thumbnail on by default", e.thumbnail.url, M.display_avatar.url)

check("card: ping can be turned off",
      auto.build_welcome(dict(CARD, welcome_ping=0), M)[0], "")
check("card: thumbnail can be turned off",
      auto.build_welcome(dict(CARD, welcome_embed_thumb=0), M)[1].thumbnail.url, None)
check("card: bad image is dropped, card still posts",
      auto.build_welcome(dict(CARD, welcome_embed_image="attachment://x.png"), M)[1].image.url, None)
check("card: title over 256 is cut, not 400'd",
      len(auto.build_welcome(dict(CARD, welcome_embed_title="x" * 400), M)[1].title), 256)
check("card: title-only card is still a card",
      auto.build_welcome({"welcome_embed": 1, "welcome_embed_title": "Hi"}, M)[1].description, None)
check("card: image-only card is still a card",
      bool(auto.build_welcome({"welcome_embed": 1,
                               "welcome_embed_image": "https://a.test/x.gif"}, M)[1]), True)
check("card: nothing configured posts nothing, not a bare coloured bar",
      auto.build_welcome({"welcome_embed": 1}, M), ("", None))
check("card: nothing configured, ping on — still nothing",
      auto.build_welcome({"welcome_embed": 1, "welcome_ping": 1}, M), ("", None))

# ── the @everyone guard the template can never get past ──────────────
check("render caps at 2000 chars", len(auto.render("x" * 3000, M)), 2000)

print(f"{_total - len(_fails)}/{_total} welcome-card checks passed")
for f in _fails:
    print("  FAIL", f)
sys.exit(1 if _fails else 0)
