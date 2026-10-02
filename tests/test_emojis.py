"""/steal-emoji — which CDN copies get tried when an emoji is over Discord's
256KB upload cap, and the name cleaning. No live Discord, no network: the
cog's `_fetch` is replaced with a table of fake CDN answers.

Why: an animated emoji that is 324KB as a GIF was refused outright, though
the same emoji is 119KB as an animated WebP. The fallback order is the fix.

Run on any box with discord.py importable:
    /opt/peepos-reclaimer/venv/bin/python tests/test_emojis.py
Exits non-zero on any failure.
"""
import asyncio
import inspect
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import discord  # noqa: E402
from cogs.emojis import Emojis, MAX_EMOJI_BYTES, _clean_name  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


BIG = b"x" * (MAX_EMOJI_BYTES + 1)
EID = "1335074490509557840"
CDN = f"https://cdn.discordapp.com/emojis/{EID}"


def uploads(table, animated):
    """Run _fetch_emoji against a fake CDN: {url: bytes}, anything else 404s."""
    cog = Emojis(bot=None)
    asked = []

    async def fake_fetch(session, url, cap=0, redirects=True):
        asked.append(url)
        return table.get(url)

    cog._fetch = fake_fetch
    return asyncio.run(cog._fetch_emoji(None, EID, animated)), asked


# ── fits already: one upload, nothing else asked for ─────────────────────
out, asked = uploads({f"{CDN}.gif": b"gif"}, True)
check("small animated emoji = the GIF, one request", out == [b"gif"] and asked == [f"{CDN}.gif"])
out, asked = uploads({f"{CDN}.png": b"png"}, False)
check("small still = the PNG, one request", out == [b"png"] and asked == [f"{CDN}.png"])

# ── the 324KB GIF case ───────────────────────────────────────────────────
out, _ = uploads({f"{CDN}.gif": BIG, f"{CDN}.webp?animated=true": b"webp",
                  f"{CDN}.gif?size=96": BIG, f"{CDN}.gif?size=64": b"gif64"}, True)
check("oversized GIF: WebP first, then the smaller GIF that fits", out == [b"webp", b"gif64"])
out, _ = uploads({f"{CDN}.gif": BIG, f"{CDN}.webp?animated=true": BIG,
                  f"{CDN}.gif?size=96": BIG, f"{CDN}.gif?size=64": BIG}, True)
check("nothing under the cap = nothing to upload", out == [])
check("an over-cap copy is never offered", all(len(d) <= MAX_EMOJI_BYTES for d in out))

# ── stills ───────────────────────────────────────────────────────────────
out, _ = uploads({f"{CDN}.png": BIG, f"{CDN}.png?size=128": b"png128"}, False)
check("oversized still: the 128px PNG", out == [b"png128"])

# ── bare id guessed animated, but it's a still ───────────────────────────
out, asked = uploads({f"{CDN}.png": b"png"}, True)
check("bare id that isn't animated falls back to PNG", out == [b"png"])
out, _ = uploads({f"{CDN}.png": BIG, f"{CDN}.png?size=128": b"png128",
                  f"{CDN}.webp?animated=true": b"webp"}, True)
check("...and then shrinks as a still, not as an animation", out == [b"png128"])

# ── gone ─────────────────────────────────────────────────────────────────
out, _ = uploads({}, True)
check("deleted emoji = nothing", out == [])

# ── Discord's hourly hold: give up fast, don't sit it out ────────────────
import cogs.emojis as emojis_mod  # noqa: E402


class HeldGuild:
    """A server whose emoji uploads Discord has put on hold."""
    finished = False

    async def create_custom_emoji(self, **kwargs):
        await asyncio.sleep(5)          # the library waiting out the hold
        HeldGuild.finished = True
        return "emoji"


class OpenGuild:
    async def create_custom_emoji(self, **kwargs):
        return f"<:{kwargs['name']}:1>"


async def held_case():
    emojis_mod.UPLOAD_WAIT = 0.05
    cog = Emojis(bot=None)
    try:
        await cog._create(HeldGuild(), name="x", image=b"", reason="")
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
    await asyncio.sleep(0.1)
    ok = await cog._create(OpenGuild(), name="fox", image=b"", reason="")
    return timed_out, ok

timed_out, ok = asyncio.run(held_case())
check("an upload on hold gives up instead of hanging", timed_out)
check("...and the dropped upload never lands later", not HeldGuild.finished)
check("a normal upload still goes straight through", ok == "<:fox:1>")
check("the hold message says to try later", "try again later" in emojis_mod.HELD.lower())

# ── names ────────────────────────────────────────────────────────────────
check("filename with spaces/dashes cleans up", _clean_name("my cool-cat (1)") == "mycoolcat1")
check("a one-letter name is refused", _clean_name("a") == "")
check("names cap at 32", len(_clean_name("x" * 50)) == 32)

# ── the command's shape ──────────────────────────────────────────────────
# emoji OR file, never both: two subcommands, each with its one required field
subs = {c.name: c for c in Emojis.steal.commands}
check("/steal-emoji has exactly emoji + file", set(subs) == {"emoji", "file"})
params = inspect.signature(subs["emoji"].callback).parameters
check("/steal-emoji emoji: emoji is required, no file field",
      params["emoji"].default is inspect.Parameter.empty and "file" not in params)
params = inspect.signature(subs["file"].callback).parameters
check("/steal-emoji file: an attachment is required, no emoji field",
      params["file"].annotation is discord.Attachment
      and params["file"].default is inspect.Parameter.empty and "emoji" not in params)
check("both still need Manage Expressions at run time",
      all(c.checks for c in subs.values()))

print(f"\n{_total - len(_fails)}/{_total} passed")
sys.exit(1 if _fails else 0)
