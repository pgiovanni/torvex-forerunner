#!/usr/bin/env python3
"""Generate docs/commands.md from the LIVE Discord command tree + the cog sources.

Run it on the VPS (it needs the bot token from .env and the deployed cogs):

    python3 tools/gen_command_docs.py                 # -> docs/commands.md
    python3 tools/gen_command_docs.py --out /tmp/x.md
    python3 tools/gen_command_docs.py --json /tmp/ref.json   # also dump the data

Why it reads Discord rather than only the source: the registered tree is what
members actually see. Option types, required flags, choices and value ranges all
come back from the API exactly as Discord enforces them, so the table can't drift
from reality the way a hand-written one does.

Two things the API can NOT tell us, so they come from an AST pass over cogs/:
  * which cog owns a command;
  * whether a permission gate is real. `@app_commands.default_permissions(...)`
    only sets default VISIBILITY and a server admin can override it in
    Integrations; `@app_commands.checks.has_permissions(...)` is what actually
    enforces at call time. A command carrying only the former is overridable,
    and the generator flags it.
"""
import argparse, ast, io, json, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API = "https://discord.com/api/v10"

OPT_TYPE = {1: "subcommand", 2: "subcommand-group", 3: "string", 4: "integer",
            5: "boolean", 6: "user", 7: "channel", 8: "role",
            9: "mentionable", 10: "number", 11: "attachment"}

CHANNEL_TYPES = {0: "text", 2: "voice", 4: "category", 5: "announcement",
                 13: "stage", 15: "forum"}

PERM_BITS = [(1 << 3, "Administrator"), (1 << 1, "Kick Members"),
             (1 << 2, "Ban Members"), (1 << 4, "Manage Channels"),
             (1 << 5, "Manage Server"), (1 << 13, "Manage Messages"),
             (1 << 28, "Manage Roles"), (1 << 30, "Manage Expressions"),
             (1 << 40, "Timeout Members"), (1 << 0, "Create Invite"),
             (1 << 6, "Add Reactions"), (1 << 27, "Manage Nicknames"),
             (1 << 17, "Mention Everyone"), (1 << 34, "Manage Threads")]

PERM_LABEL = {"administrator": "Administrator", "ban_members": "Ban Members",
              "kick_members": "Kick Members", "manage_guild": "Manage Server",
              "manage_channels": "Manage Channels",
              "manage_messages": "Manage Messages",
              "manage_roles": "Manage Roles",
              "moderate_members": "Timeout Members",
              "manage_emojis": "Manage Expressions",
              "manage_emojis_and_stickers": "Manage Expressions",
              "manage_nicknames": "Manage Nicknames",
              "mention_everyone": "Mention Everyone",
              "create_instant_invite": "Create Invite",
              "add_reactions": "Add Reactions", "manage_threads": "Manage Threads"}

SECTIONS = [
    ("Moderation",        ["moderation", "mod_log"]),
    ("Security",          ["altguard", "antinuke", "security", "quarantine_lock",
                           "link_guard", "recon_watch", "verify_prune",
                           "server_backup"]),
    ("Server setup",      ["setup", "automation", "role_menu", "emojis",
                           "pi_count", "suggestions", "tickets"]),
    ("Members & invites", ["invites", "level_roles", "economy", "activity", "stats"]),
    ("Games & fun",       ["fun", "games", "rpg", "pvp", "wordle", "chess_cog",
                           "trading", "gear", "gifts", "peepo"]),
    ("AI",                ["ai"]),
    ("Help",              ["help"]),
]

FRIENDLY = {
    "activity": "Activity graphs", "ai": "AI", "altguard": "AltGuard (alt detection)",
    "antinuke": "Anti-nuke", "automation": "Automation", "chess_cog": "Chess",
    "economy": "Economy & levels", "emojis": "Emojis", "fun": "Fun",
    "games": "Games vs the bot", "gear": "Gear", "gifts": "Gifts", "help": "Help",
    "invites": "Invites", "level_roles": "Level roles", "link_guard": "LinkGuard",
    "mod_log": "Mod log & message archive", "moderation": "Moderation",
    "peepo": "Peepo", "pi_count": "Pi counting", "pvp": "PvP",
    "quarantine_lock": "Quarantine lock", "recon_watch": "Recon watch",
    "role_menu": "Reaction roles", "rpg": "RPG", "security": "Security config",
    "server_backup": "Server backup", "setup": "Setup", "stats": "Stats",
    "suggestions": "Suggestions", "tickets": "Tickets", "trading": "Trading",
    "verify_prune": "Verify prune", "wordle": "Wordle",
}


# --------------------------------------------------------------- live tree
def env(key, path):
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def fetch_live(envfile):
    token = (env("DISCORD_TOKEN", envfile) or env("TOKEN", envfile)
             or env("BOT_TOKEN", envfile))
    if not token:
        sys.exit("no bot token found in " + envfile)

    def get(path):
        req = urllib.request.Request(API + path, headers={
            "Authorization": "Bot " + token,
            "User-Agent": "DiscordBot (peeposreclaimer, 1.0)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    me = get("/users/@me")
    return me, get("/applications/%s/commands" % me["id"])


# ------------------------------------------------------------- AST helpers
def _dotted(n):
    """Dotted name of a node. Unwraps Call so `app_commands.Group(name=...)`
    resolves to "app_commands.Group" — without this, group detection silently
    finds nothing and every subcommand loses its cog."""
    if isinstance(n, ast.Call):
        n = n.func
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
    return ".".join(reversed(parts))


def _perm_args(node):
    out = [kw.arg for kw in node.keywords
           if isinstance(kw.value, ast.Constant) and kw.value.value is True]
    out += [str(a.value) for a in node.args if isinstance(a, ast.Constant)]
    return out


def _kwarg(d, key):
    if not isinstance(d, ast.Call):
        return None
    for kw in d.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _owner_only(fn):
    """A real gate on the INVOKER: interaction.user.id vs <x>.owner_id.

    Strict on purpose. A looser check flags /ban (compares the TARGET to the
    owner for hierarchy safety) and /wordle (compares a player to the game's
    owner) — neither restricts who may run the command.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        sides = [_dotted(node.left)] + [_dotted(c) for c in node.comparators]
        if (any(s.endswith(("interaction.user.id", "ctx.author.id")) for s in sides)
                and any(s.endswith("owner_id") for s in sides)):
            return True
    return False


def _class_group_name(cls):
    """Group name for `class X(app_commands.Group)` — declared as a class keyword,
    a class-level `name = "..."`, or `super().__init__(name="...")`."""
    for kw in cls.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    for node in cls.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Name) and t.id == "name"
                        and isinstance(node.value, ast.Constant)):
                    return node.value.value
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and _dotted(sub).endswith("__init__")):
                    got = _kwarg(sub, "name")
                    if got:
                        return got
    return cls.name.lower()


def _decorators(fn):
    """(command name or None, visibility perms, runtime perms, group var or None)"""
    name = gvar = None
    vis = run = None
    for d in fn.decorator_list:
        dn = _dotted(d)
        if dn.endswith("app_commands.command") or dn == "command":
            name = _kwarg(d, "name") or fn.name
        elif dn.endswith(".command"):
            gvar = dn.rsplit(".command", 1)[0].split(".")[-1]
            name = _kwarg(d, "name") or fn.name
        elif "default_permissions" in dn:
            vis = _perm_args(d) if isinstance(d, ast.Call) else []
        elif "has_permissions" in dn:
            run = _perm_args(d) if isinstance(d, ast.Call) else []
    return name, vis or [], run or [], gvar


def scan_cogs(cogdir):
    """{command path: {cog, visibility_permissions, runtime_permissions, owner}}

    Handles all three shapes this codebase uses:
      * bare `@app_commands.command` on a Cog method            -> /name
      * `grp = app_commands.Group(name="x")` + `@grp.command`   -> /x name
      * `class G(app_commands.Group)` with the name set in      -> /parent x name
        `super().__init__(name="x")`, attached by
        `grp.add_command(G())`
    """
    model = {}
    for fname in sorted(os.listdir(cogdir)):
        if not fname.endswith(".py"):
            continue
        mod = fname[:-3]
        try:
            tree = ast.parse(open(os.path.join(cogdir, fname), encoding="utf-8").read())
        except SyntaxError as e:
            print("  ! skipping %s: %s" % (fname, e), file=sys.stderr)
            continue

        # var -> group name, for @var.command(...)
        groups = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if _dotted(node.value).endswith("Group"):
                    gname = _kwarg(node.value, "name")
                    for t in node.targets:
                        if isinstance(t, ast.Name) and gname:
                            groups[t.id] = gname

        # Group subclasses, and where they get attached: parent.add_command(Cls())
        class_groups, attached_to = {}, {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                    _dotted(b).endswith("Group") for b in node.bases):
                class_groups[node.name] = _class_group_name(node)
            if isinstance(node, ast.Call) and _dotted(node).endswith("add_command"):
                parent = _dotted(node.func).rsplit(".add_command", 1)[0].split(".")[-1]
                for a in node.args:
                    cls = _dotted(a)
                    if cls:
                        attached_to[cls.split(".")[-1]] = parent

        def record(path, fn, vis, run):
            model[path.strip()] = {
                "cog": mod, "visibility_permissions": vis,
                "runtime_permissions": run, "server_owner_only": _owner_only(fn)}

        # commands inside a Group subclass carry that group's (possibly nested) path
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if cls.name not in class_groups:
                continue
            parent = attached_to.get(cls.name)
            prefix = groups.get(parent, "") if parent else ""
            full = ("%s %s" % (prefix, class_groups[cls.name])).strip()
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                name, vis, run, _ = _decorators(fn)
                if name:
                    record("%s %s" % (full, name), fn, vis, run)

        # everything else (skip the Group-subclass bodies already recorded above)
        group_class_fns = {id(fn) for cls in ast.walk(tree)
                           if isinstance(cls, ast.ClassDef) and cls.name in class_groups
                           for fn in cls.body
                           if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if id(fn) in group_class_fns:
                continue
            name, vis, run, gvar = _decorators(fn)
            if not name:
                continue
            if gvar is not None and gvar not in groups:
                continue                       # decorator on something we can't resolve
            prefix = groups[gvar] + " " if gvar in groups else ""
            record(prefix + name, fn, vis, run)
    return model


# ------------------------------------------------------------ flatten tree
def _params(options):
    out = []
    for o in options or []:
        if o["type"] in (1, 2):
            continue
        p = {"name": o["name"], "type": OPT_TYPE.get(o["type"], "type%d" % o["type"]),
             "required": bool(o.get("required")),
             "description": (o.get("description") or "").strip()}
        if o.get("choices"):
            p["choices"] = [c["name"] for c in o["choices"]]
        if o.get("channel_types"):
            p["channel_types"] = [CHANNEL_TYPES.get(t, str(t)) for t in o["channel_types"]]
        for api_k, out_k in (("min_value", "min"), ("max_value", "max"),
                             ("min_length", "min_length"), ("max_length", "max_length")):
            if o.get(api_k) is not None:
                p[out_k] = o[api_k]
        if o.get("autocomplete"):
            p["autocomplete"] = True
        out.append(p)
    return out


def _walk(cmd):
    opts = cmd.get("options") or []
    subs = [o for o in opts if o["type"] == 1]
    groups = [o for o in opts if o["type"] == 2]
    if not subs and not groups:
        return [{"path": cmd["name"], "description": (cmd.get("description") or "").strip(),
                 "params": _params(opts)}]
    leaves = [{"path": "%s %s" % (cmd["name"], s["name"]),
               "description": (s.get("description") or "").strip(),
               "params": _params(s.get("options"))} for s in subs]
    for g in groups:
        for s in [o for o in (g.get("options") or []) if o["type"] == 1]:
            leaves.append({"path": "%s %s %s" % (cmd["name"], g["name"], s["name"]),
                           "description": (s.get("description") or "").strip(),
                           "params": _params(s.get("options"))})
    return leaves


def decode_perms(raw):
    if raw in (None, "", "0"):
        return []
    try:
        bits = int(raw)
    except (TypeError, ValueError):
        return []
    return [n for b, n in PERM_BITS if bits & b]


def manifest(root):
    try:
        raw = json.load(open(os.path.join(root, "commands.json"), encoding="utf-8"))
    except Exception:
        return {}
    return {e["name"]: e for e in raw.get("commands", []) if isinstance(e, dict)}


# ---------------------------------------------------------------- markdown
def _label(p):
    return PERM_LABEL.get(p, p.replace("_", " ").title())


def _access(cmd, acc):
    a = acc.get(cmd["path"], {})
    if a.get("server_owner_only"):
        return "**Server owner only**"
    run = [_label(p) for p in a.get("runtime_permissions", [])]
    vis = [_label(p) for p in a.get("visibility_permissions", [])]
    if run:
        return "Requires **%s**" % " + ".join(run)
    if vis:
        return ("Requires **%s** — ⚠️ visibility gate only, overridable in "
                "Server Settings → Integrations" % " + ".join(vis))
    if cmd.get("permissions"):
        return "Requires **%s**" % " + ".join(cmd["permissions"])
    return "Everyone"


def _constraints(p):
    bits = []
    if p.get("choices"):
        bits.append("one of: " + ", ".join("`%s`" % c for c in p["choices"]))
    if p.get("channel_types"):
        bits.append("channel types: " + ", ".join(p["channel_types"]))
    if p.get("min") is not None or p.get("max") is not None:
        bits.append("range %s–%s" % (p.get("min", "?"), p.get("max", "?")))
    if p.get("min_length") is not None or p.get("max_length") is not None:
        bits.append("length %s–%s" % (p.get("min_length", 0), p.get("max_length", "?")))
    if p.get("autocomplete"):
        bits.append("autocomplete")
    return "; ".join(bits)


def render(bot_name, cmds, acc):
    out = io.StringIO()
    w = out.write
    by_cog = {}
    for c in cmds:
        by_cog.setdefault(c["cog"], []).append(c)

    w("# Command reference\n\n")
    w("Every slash command **%s** exposes. Generated from the *live registered "
      "command tree* — what Discord actually has synced — plus an AST pass over "
      "the cogs, so it cannot drift from the running bot.\n\n" % bot_name)
    w("- **%d commands** (%d top-level, the rest subcommands) across %d cogs\n"
      % (len(cmds), len({c["root"] for c in cmds}), len(by_cog)))
    w("- Regenerate: `python3 tools/gen_command_docs.py`\n\n")

    w("## How to read this\n\n")
    w("Signatures use the usual convention — `<required>`, `[optional]`.\n\n")
    w("**Access** says who may run a command:\n\n")
    w("| Tier | Meaning |\n|---|---|\n")
    w("| Everyone | No permission gate — any member. |\n")
    w("| Requires *permission* | Gated on a Discord permission **and enforced when "
      "the command runs**. Widening it in Server Settings → Integrations does not "
      "bypass the check. |\n")
    w("| Server owner only | Hard-gated to the guild owner. Cannot be delegated. |\n\n")
    w("**There are no bot-owner-only commands.** Nothing is reserved to the bot's "
      "developer — every gate is a permission your own server controls.\n\n")
    w("Anything acting in bulk requires Administrator, enforced at runtime, because "
      "`default_permissions` on its own is overridable in Integrations. A command "
      "carrying only that weaker gate is flagged inline with ⚠️.\n\n")

    w("## Index\n\n")
    placed = set()
    for title, cogs in SECTIONS:
        present = [c for c in cogs if c in by_cog]
        if not present:
            continue
        names = set()
        for cog in present:
            placed.add(cog)
            names |= {"`/%s`" % c["root"] for c in by_cog[cog]}
        w("**%s** — %s\n\n" % (title, ", ".join(sorted(names))))
    rest = sorted(set(by_cog) - placed)
    if rest:
        names = sorted({"`/%s`" % c["root"] for cog in rest for c in by_cog[cog]})
        w("**Other** — %s\n\n" % ", ".join(names))

    def render_cog(cog):
        w("### %s\n\n<sub>`cogs/%s.py`</sub>\n\n" % (FRIENDLY.get(cog, cog), cog))
        for c in sorted(by_cog[cog], key=lambda x: x["path"]):
            w("#### `/%s`\n\n" % c["path"])
            if c["description"]:
                w("%s\n\n" % c["description"])
            w("```\n%s\n```\n\n" % c["signature"])
            w("**Access:** %s" % _access(c, acc))
            if c.get("cooldown_seconds"):
                w(" &nbsp;·&nbsp; **Cooldown:** %ss" % c["cooldown_seconds"])
            if not c.get("dm_allowed", True):
                w(" &nbsp;·&nbsp; Server only")
            w("\n\n")
            if c["params"]:
                w("| Parameter | Type | Required | Description |\n|---|---|:--:|---|\n")
                for p in c["params"]:
                    d = (p["description"] or "").replace("|", "\\|").strip()
                    con = _constraints(p)
                    if con:
                        d = (d + " " if d else "") + "*(%s)*" % con
                    w("| `%s` | %s | %s | %s |\n" % (p["name"], p["type"],
                                                     "Yes" if p["required"] else "No",
                                                     d or "—"))
                w("\n")
            else:
                w("*No parameters.*\n\n")

    for title, cogs in SECTIONS:
        present = [c for c in cogs if c in by_cog]
        if present:
            w("---\n\n## %s\n\n" % title)
            for cog in present:
                render_cog(cog)
    if rest:
        w("---\n\n## Other\n\n")
        for cog in rest:
            render_cog(cog)
    return out.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-dir", default="/opt/peepos-reclaimer",
                    help="deployed bot dir (holds .env + cogs/)")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "commands.md"))
    ap.add_argument("--json", help="also write the intermediate data here")
    a = ap.parse_args()

    me, live = fetch_live(os.path.join(a.bot_dir, ".env"))
    acc = scan_cogs(os.path.join(a.bot_dir, "cogs"))
    man = manifest(a.bot_dir)

    cmds = []
    for cmd in live:
        if cmd.get("type", 1) != 1:
            continue                       # user/message context menus aren't slash
        meta = man.get(cmd["name"], {})
        perms = decode_perms(cmd.get("default_member_permissions"))
        for leaf in _walk(cmd):
            leaf["signature"] = " ".join(
                ["/" + leaf["path"]] +
                [("<%s>" if p["required"] else "[%s]") % p["name"] for p in leaf["params"]])
            leaf.update(root=cmd["name"], cog=acc.get(leaf["path"], {}).get("cog", "?"),
                        permissions=perms,
                        cooldown_seconds=meta.get("cooldown_seconds"),
                        dm_allowed=cmd.get("dm_permission", True))
            cmds.append(leaf)
    cmds.sort(key=lambda c: (c["cog"], c["path"]))

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    open(a.out, "w", encoding="utf-8").write(render(me.get("username"), cmds, acc))
    if a.json:
        json.dump({"bot": me.get("username"), "commands": cmds, "access": acc},
                  open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    unmapped = sorted({c["root"] for c in cmds if c["cog"] == "?"})
    weak = sorted(k for k, v in acc.items()
                  if v["visibility_permissions"] and not v["runtime_permissions"])
    print("wrote %s" % a.out)
    print("  %d commands / %d top-level / %d cogs"
          % (len(cmds), len({c["root"] for c in cmds}), len({c["cog"] for c in cmds})))
    if unmapped:
        print("  ! not mapped to a cog: %s" % ", ".join(unmapped))
    if weak:
        print("  ! visibility-gate only (overridable in Integrations): %s"
              % ", ".join(weak))


if __name__ == "__main__":
    main()
