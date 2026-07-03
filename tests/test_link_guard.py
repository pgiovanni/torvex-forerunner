"""Standalone LinkGuard detector harness — exercises the REAL scan() logic
against synthetic messages/embeds, no live Discord.

The scan/normalize/hostname functions are pure (no discord objects), so this
imports the cog module and calls them directly. Focus: the vectors AutoMod can't
see — masked links, unfurled embeds, proxied image URLs, scheme-less domains —
plus the purple-team "hidden behind another domain so Discord embeds it" attack.

Run on any box with discord.py importable (the module imports discord at top):
    /opt/peepos-reclaimer/venv/bin/python tests/test_link_guard.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.link_guard as lg  # noqa: E402

DOMAINS = lg.load_base_domains()
assert len(DOMAINS) > 60, f"base corpus looks short: {len(DOMAINS)}"

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


def hits(content, embeds=None, allow=()):
    return lg.scan(content, embeds or [], DOMAINS, allow)


# 1) plain grabber link in content
r = hits("check this out https://grabify.link/abc123")
check("plain grabify link in content", "grabify.link" in r and "content" in r["grabify.link"]["vectors"])

# 2) scheme-less bare domain (no http://) — substring path
r = hits("go to grabify.link/xyz right now")
check("scheme-less grabber domain", "grabify.link" in r)

# 3) markdown-masked link with blank/invisible text — the classic hide trick
r = hits("[⠀](https://grabify.link/track.jpg)")
check("masked link target detected", "grabify.link" in r)
check("masked link flagged hidden", r["grabify.link"]["hidden"] is True)

# 4) HIDDEN EMBED (purple-team): domain ONLY in the unfurled embed, not in text.
#    Simulates Discord adding an image embed sourced from the tracker.
embed = {"type": "image", "url": "https://grabify.link/pic.png",
         "image": {"url": "https://grabify.link/pic.png"}}
r = hits("look at my cat pic!", [embed])
check("hidden-embed: matched via embed", "grabify.link" in r)
check("hidden-embed: flagged hidden (not in text)", r["grabify.link"]["hidden"] is True)

# 5) Discord PROXIED image URL — origin domain encoded in the proxy path
proxy = {"image": {"url": "https://grabify.link/x.png",
                   "proxy_url": "https://images-ext-1.discordapp.net/external/AbC/https/grabify.link/x.png"}}
r = hits("nothing to see", [proxy])
check("proxied origin domain in embed path", "grabify.link" in r)

# 6) percent-encoded origin still matches after unquote
r = hits("https://images-ext-1.discordapp.net/external/z/https%3A%2F%2Fiplogger.org%2Fabc")
check("percent-encoded origin (unquote)", "iplogger.org" in r)

# 7) subdomain of a canary host — suffix match
r = hits("https://x7g2.canarytokens.com/traffic/abc/post.jsp")
check("canary subdomain suffix match", "canarytokens.com" in r or "canarytokens" in r)

# 8) bare-token rule (no dot) matches inside a hostname
r = hits("https://tinyurl.com/2p8xyz")
check("bare-token 'tinyurl' substring", "tinyurl" in r)

# 9) iplogger family
r = hits("https://iplogger.org/1a2b3")
check("iplogger.org", "iplogger.org" in r)

# 10) clean message — no false positive
r = hits("here's the github https://github.com/pgiovanni/peeposreclaimer and a youtube https://youtu.be/dQw4")
check("clean links do not trip", r == {})

# 11) allow-list suppresses a base domain
r = hits("https://tinyurl.com/legit", allow=["tinyurl"])
check("allow-list suppresses match", "tinyurl" not in r)

# 12) look-alike does NOT falsely match a different TLD via suffix
#     (substring parity means bit.ly-in-text still could; assert the safe hostname case)
r = hits("https://mybitpay.com/checkout")
check("unrelated domain doesn't match bit.ly by suffix", "bit.ly" not in r)

# 13) embed description carrying the link (unfurl of a page that mentions it)
embed = {"title": "Free Nitro", "description": "claim at https://fortnitechat.site/gift"}
r = hits("free stuff", [embed])
check("embed description link", "fortnitechat.site" in r)

# 14) multiple domains in one message
r = hits("https://grabify.link/a and https://iplogger.org/b")
check("multiple domains found", "grabify.link" in r and "iplogger.org" in r)

# 15) defang output is un-clickable
check("defang neutralizes", lg.defang("https://grabify.link") == "hxxps://grabify[.]link")

# --- severity tiering (HIGH = quarantine + taunt; LOW = gentle) --------------
SHORT = lg.load_shortener_rules()
check("shorteners category loaded", {"bit.ly", "tinyurl", "shorturl"} <= SHORT)

# 16) a real tracker domain = HIGH
check("grabber link is HIGH severity",
      lg.classify_severity(hits("https://grabify.link/x"), SHORT) == "high")

# 17) shortener-only = LOW (protects a legit member posting bit.ly)
check("shortener-only is LOW severity",
      lg.classify_severity(hits("check https://bit.ly/abc"), SHORT) == "low")

# 18) hidden-embed shortener STILL escalates to HIGH (deliberate masking)
hid = lg.scan("cat pic", [{"image": {"url": "https://bit.ly/x"}}], DOMAINS, [])
check("hidden shortener escalates to HIGH", lg.classify_severity(hid, SHORT) == "high")

# 19) shortener + tracker together = HIGH (any tracker wins)
check("mixed hit is HIGH",
      lg.classify_severity(hits("https://bit.ly/a https://iplogger.org/b"), SHORT) == "high")

# 20) default taunt gifs are real Tenor links (autoplay in Discord)
check("taunt gifs are tenor view urls",
      all(g.startswith("https://tenor.com/view/") for g in lg.DEFAULT_TAUNT_GIFS)
      and len(lg.DEFAULT_TAUNT_GIFS) == 2)

# --- boundary-safe matching: short shortener rules must not spam FPs ----------
# 21) a short rule like x.co must NOT match inside a longer label
r = hits("let's just relax.com and chill, also max.com")
check("x.co does NOT match inside relax.com/max.com", "x.co" not in r)
# 22) but a genuine x.co host IS caught
r = hits("shortened here https://x.co/AbCdE")
check("x.co matches a real x.co host", "x.co" in r)
# 23) t.co must not trip on ordinary words
r = hits("visit root.com or at.com sometime")
check("t.co does NOT match root.com/at.com", "t.co" not in r)
# 24) grabber domains still match at a trailing-punctuation boundary
r = hits("look: grabify.link.")
check("grabber still matches before a sentence period", "grabify.link" in r)

# --- expanded shortener corpus (LOW severity, per request) -------------------
SHORTX = lg.load_shortener_rules()
check("expanded shorteners loaded", {"t.co", "goo.gl", "cutt.ly", "tiny.cc"} <= SHORTX)
check("a new shortener stays LOW severity",
      lg.classify_severity(hits("see https://t.co/abc"), SHORTX) == "low")

# --- DNS-origin detection: unknown vanity resolving onto a known tracker IP ---
check("load_category('grabify') includes grabify.link", "grabify.link" in lg.load_category("grabify"))
ch = lg.candidate_hostnames("see https://new-vanity.lol/x and [m](https://foo.bar/y)", [])
check("candidate_hostnames pulls hosts from text + masked", {"new-vanity.lol", "foo.bar"} <= ch)
m = lg.match_tracker_ip("new-vanity.lol", {"52.173.151.229"}, {"52.173.151.229", "104.247.81.99"})
check("unknown host on a tracker IP is flagged", m is not None and m["resolved_ip"] == "52.173.151.229")
check("an IP-origin finding classifies HIGH",
      m is not None and lg.classify_severity({"new-vanity.lol": m}, SHORTX) == "high")
check("host NOT on a tracker IP stays clean",
      lg.match_tracker_ip("normal.com", {"93.184.216.34"}, {"52.173.151.229"}) is None)
# CDN-exclusion: a Cloudflare IP must never be learned as a tracker origin
check("cloudflare IP flagged as shared CDN", lg._is_shared_cdn("104.26.8.202") is True)
check("dedicated azure origin NOT shared CDN", lg._is_shared_cdn("52.173.151.229") is False)
check("private IP excluded from tracker set", lg._is_shared_cdn("10.0.0.5") is True)

print()
if _fails:
    print(f"{len(_fails)} FAILED: {_fails}")
    sys.exit(1)
print(f"all {_total} scenarios passed  ({len(DOMAINS)} base domains loaded)")
