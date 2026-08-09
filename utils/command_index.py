"""Condensed command index for the AI cog — what the bot can actually do.

The AI is stateless and has no tool access, so before this module it knew
exactly one thing about the bot it lives in: the sentence "You are Peepo's
Reclaimer, the server's bot." Asked "how do I check my level", it had nothing
to answer from — and a model with no grounding does not say "I don't know", it
invents a plausible-looking slash command. A wrong /command coming from the
bot itself reads as authoritative, which is worse than silence.

So every request now carries a one-line-per-command index built from
`docs/commands.json`, the artifact `tools/gen_command_docs.py` generates from
the LIVE registered command tree plus an AST pass over `cogs/`. That provenance
is the point: the list cannot drift from what Discord actually enforces, and it
is regenerated rather than hand-maintained.

Two halves, both pure (no discord import, so this runs in the local venv):

  * `build_block()` — the text handed to the model. ~15KB / ~3.9k tokens for
    187 commands, against 113KB for the raw JSON, because the parameter tables,
    cooldowns and cog ownership are dropped. Those belong on the docs page; the
    model only needs to point someone at the right command.
  * `unknown_citations()` — the check applied to the ANSWER. Prompt rules are a
    request; a set-membership test is a fact. Every /token the model emits is
    resolved against the real command set, so a hallucinated name is caught
    mechanically instead of being trusted.
"""
import json
import os
import re

# Signatures are quoted verbatim, so <required> / [optional] reach the model
# exactly as the docs page and Discord's own picker show them.
_HEADER = (
    "COMMANDS THIS BOT HAS (complete list, {n} commands):"
)

# A command citation in free text: "/ask", "/msglog history", "/activity user".
# The lookbehind keeps URLs (dashboard.torvex.app/docs/commands), dates (8/9)
# and prose ("and/or") from reading as commands. Up to three words are captured
# because groups nest two deep; the resolver picks the longest real prefix.
_CITE = re.compile(r"(?<![\w./])/([a-z0-9][a-z0-9-]*(?:\s+[a-z0-9-]+){0,2})")


def load(path):
    """Parse the generated docs JSON. Returns None when it isn't there — the
    caller degrades to "I don't have the command list" rather than guessing."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("commands"), list):
        return None
    return data


def _tier(cmd):
    """Access label, omitted entirely for everyone-commands so the common case
    costs no tokens. This is what stops the model telling a regular member to
    run /altguard-sweep."""
    perms = cmd.get("permissions") or []
    return "/".join(perms)


def build_block(data):
    """One line per command: signature — description [access]."""
    cmds = sorted(data.get("commands") or [], key=lambda c: c.get("path", ""))
    lines = []
    for c in cmds:
        sig = (c.get("signature") or f"/{c.get('path', '')}").strip()
        desc = (c.get("description") or "").strip()
        tier = _tier(c)
        line = f"{sig} — {desc}" if desc else sig
        if tier:
            line += f"  [{tier}]"
        lines.append(line)
    return _HEADER.format(n=len(lines)) + "\n" + "\n".join(lines)


def known(data):
    """(paths, roots) — every command path, plus every top-level group name.
    Roots let the checker tell "invented a whole command" apart from "used a
    real group with a subcommand that doesn't exist"; both are wrong, but the
    second is the one a real member is most likely to try and fail on."""
    paths, roots = set(), set()
    for c in data.get("commands") or []:
        p = (c.get("path") or "").strip().lower()
        if p:
            paths.add(p)
        r = (c.get("root") or p.split(" ")[0]).strip().lower()
        if r:
            roots.add(r)
    return paths, roots


def unknown_citations(text, paths, roots):
    """Every /command in `text` that does not exist. Longest-prefix resolution:
    "/ask about the weather" is a citation of /ask followed by prose, not a
    citation of a three-word command."""
    bad = set()
    for m in _CITE.finditer((text or "").lower()):
        words = m.group(1).split()
        for n in range(min(3, len(words)), 0, -1):
            if " ".join(words[:n]) in paths:
                break
        else:
            # nothing matched: report the group+sub when the group is real,
            # otherwise just the invented name
            if words[0] in roots and len(words) > 1:
                bad.add(f"{words[0]} {words[1]}")
            else:
                bad.add(words[0])
    return bad


class CommandIndex:
    """Loads once, reloads when the file changes on disk.

    Keyed on (mtime_ns, size) so a regenerated docs JSON takes effect without a
    bot restart — same idea as the dashboard's command_docs(), but not on
    float seconds alone: two writes inside one clock tick compare equal that
    way and the reload is silently skipped (caught by the reload test, which
    passed on Windows and failed on Linux). Size moves whenever the command
    set does, so the pair closes that window. `available` is False when the
    artifact is missing, which the prompt turns into an explicit "say you don't
    have the list" instead of an invitation to improvise.
    """

    def __init__(self, path):
        self.path = path
        self._stamp = None
        self._block = ""
        self._paths = set()
        self._roots = set()
        self._count = 0

    def _refresh(self):
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            if self._stamp is not None:      # file vanished — keep last good copy
                return
            stamp = None
        if stamp is not None and stamp == self._stamp:
            return
        data = load(self.path)
        if data is None:
            self._stamp = stamp
            return
        self._stamp = stamp
        self._block = build_block(data)
        self._paths, self._roots = known(data)
        self._count = len(data.get("commands") or [])

    @property
    def available(self):
        self._refresh()
        return bool(self._paths)

    @property
    def count(self):
        self._refresh()
        return self._count

    def block(self):
        self._refresh()
        return self._block

    def unknown(self, text):
        self._refresh()
        if not self._paths:
            return set()
        return unknown_citations(text, self._paths, self._roots)
