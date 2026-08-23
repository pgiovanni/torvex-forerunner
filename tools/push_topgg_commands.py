#!/usr/bin/env python3
"""Push the LIVE Discord command tree to the bot's Top.gg listing.

Top.gg v1 exposes `PUT /api/v1/projects/@me/commands`, which takes a JSON array
of Discord application-command objects and renders them as a searchable
"Commands" tab on the bot page. Docs: https://docs.top.gg/api/v1/projects.md

Same sourcing principle as tools/gen_command_docs.py: read the registered tree
from Discord rather than a hand-kept list, so the public page can't drift from
what members actually see.

    python3 tools/push_topgg_commands.py --dry-run --out /tmp/topgg.json
    python3 tools/push_topgg_commands.py                  # really push
    python3 tools/push_topgg_commands.py --public-only    # hide staff-gated cmds
    python3 tools/push_topgg_commands.py --clear          # empty the tab

Tokens: DISCORD_TOKEN from .env (as everywhere else) and TOPGG_TOKEN from the
environment or .env. The Top.gg token is a **v1** token — Integrations & API on
the project page. A legacy v0 token (used without the `Bearer` prefix) is
rejected by v1 endpoints; create a new one if you only have the old kind.
"""
import argparse, json, os, sys, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DISCORD_API = "https://discord.com/api/v10"
TOPGG_API = "https://top.gg/api/v1"

# Per-registration bookkeeping Discord returns and Top.gg has no use for.
# `default_permission` is the deprecated boolean superseded by
# default_member_permissions.
DROP_KEYS = {"id", "application_id", "version", "guild_id", "default_permission"}


def env(key, path):
    if not os.path.exists(path):
        return None
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
        req = urllib.request.Request(DISCORD_API + path, headers={
            "Authorization": "Bot " + token,
            "User-Agent": "DiscordBot (peeposreclaimer, 1.0)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    me = get("/users/@me")
    return me, get("/applications/%s/commands" % me["id"])


def clean(cmd):
    """Strip registration bookkeeping, keep everything Top.gg renders."""
    return {k: v for k, v in cmd.items() if k not in DROP_KEYS}


def is_staff_gated(cmd):
    """True when Discord hides the command from ordinary members by default.

    `default_member_permissions` is a permission bitfield string; "0" means
    nobody-but-admins. Absent/None means everyone sees it. This is VISIBILITY
    only (a server admin can override it in Integrations) — but it is the right
    signal for "would a browsing user ever run this", which is what the Top.gg
    tab is for.
    """
    return cmd.get("default_member_permissions") not in (None, "")


def push(payload, token):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        TOPGG_API + "/projects/@me/commands", data=body, method="PUT",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 "User-Agent": "peeposreclaimer command sync"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit("top.gg rejected the push: HTTP %s %s\n%s"
                 % (e.code, e.reason, detail.strip() or "(no body)"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.path.join(ROOT, ".env"))
    ap.add_argument("--out", help="write the payload here (always, push or not)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and show the payload, send nothing")
    ap.add_argument("--public-only", action="store_true",
                    help="omit commands Discord hides from ordinary members")
    ap.add_argument("--exclude", default="",
                    help="comma-separated top-level names to omit")
    ap.add_argument("--clear", action="store_true",
                    help="push an empty array, removing the Commands tab content")
    ap.add_argument("--token", help="Top.gg v1 token (else TOPGG_TOKEN env/.env)")
    args = ap.parse_args()

    topgg_token = (args.token or os.environ.get("TOPGG_TOKEN")
                   or env("TOPGG_TOKEN", args.env))

    if args.clear:
        payload, me = [], None
    else:
        me, live = fetch_live(args.env)
        excluded = {n.strip() for n in args.exclude.split(",") if n.strip()}
        payload, skipped = [], {"context menu": [], "staff-gated": [], "excluded": []}
        for cmd in sorted(live, key=lambda c: c["name"]):
            # type 2/3 are user/message context menus: no description, not what
            # the Commands tab documents.
            if cmd.get("type", 1) != 1:
                skipped["context menu"].append(cmd["name"])
                continue
            if cmd["name"] in excluded:
                skipped["excluded"].append(cmd["name"])
                continue
            if args.public_only and is_staff_gated(cmd):
                skipped["staff-gated"].append(cmd["name"])
                continue
            payload.append(clean(cmd))

        print("%s: %d of %d top-level commands"
              % (me["username"], len(payload), len(live)))
        for reason, names in skipped.items():
            if names:
                print("  skipped (%s): %s" % (reason, ", ".join(names)))
        print("  pushing: " + ", ".join("/" + c["name"] for c in payload))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print("wrote " + args.out)

    if args.dry_run:
        print("dry run — nothing sent")
        return

    if not topgg_token:
        sys.exit("no TOPGG_TOKEN (env, .env, or --token). Get a v1 token from "
                 "the project's Integrations & API settings.")

    status, body = push(payload, topgg_token)
    print("top.gg responded HTTP %s%s" % (status, (" " + body.strip()) if body.strip() else ""))
    if status == 204:
        print("done — the Commands tab now mirrors the live tree")


if __name__ == "__main__":
    main()
