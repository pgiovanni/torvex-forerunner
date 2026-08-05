"""Canonical outward-facing URLs.

One definition each, because these get pasted into embeds all over the bot and a
stale invite or a wrong dashboard host is the kind of thing nobody notices until
a server owner hits a dead link and gives up on the bot entirely.
"""

DASHBOARD_URL = "https://dashboard.torvex.app"
SUPPORT_INVITE = "https://discord.gg/scpwTFGVkz"
SITE_URL = "https://torvex.app"

# 13 permissions, deliberately NO Administrator — see cogs/help.py history.
INVITE_URL = (
    "https://discord.com/api/oauth2/authorize"
    "?client_id=1372003518667558952&permissions=310580210768"
    "&scope=bot%20applications.commands"
)


def invite_url(guild_id=None):
    """Bot invite, optionally deep-linked to a specific server so the owner
    doesn't have to pick it out of a dropdown."""
    return INVITE_URL + (f"&guild_id={guild_id}" if guild_id else "")


def dashboard_url(guild_id=None):
    """Dashboard, deep-linked to one server's config page when we know it.
    Route is /g/<gid> — verified against the dashboard app, not guessed."""
    return DASHBOARD_URL + (f"/g/{guild_id}" if guild_id else "")


def config_view(guild_id=None, *, invite=False):
    """Link buttons for any embed that answers "how do I configure this?".

    URL buttons are the only link Discord renders as a real button, and they
    carry no state — so these survive restarts with no view registration.
    Imported here rather than from a cog so any cog can use it without
    depending on another cog being loaded.
    """
    import discord  # local: keeps this module importable by non-bot tooling

    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="Open the Dashboard", emoji="⚙️", url=dashboard_url(guild_id)))
    if invite:
        view.add_item(discord.ui.Button(
            label="Add to your server", emoji="➕", url=invite_url(guild_id)))
    view.add_item(discord.ui.Button(
        label="Support server", emoji="💬", url=SUPPORT_INVITE))
    return view
