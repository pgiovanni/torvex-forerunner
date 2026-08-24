"""How commands are grouped for humans — shared by /help, the docs generator
and (by copy) the dashboard's reference page.

One list, imported everywhere, so a cog added to one surface can't be missing
from another. Order is deliberate: security is why most servers add the bot.
"""

SECTIONS = [
    ("Security",          ["altguard", "antinuke", "security", "quarantine_lock",
                           "link_guard", "recon_watch", "verify_prune",
                           "simpleverify", "honeypot", "server_backup",
                           "security_ai"]),
    ("Moderation",        ["moderation", "mod_log", "conduct"]),
    ("Server setup",      ["setup", "automation", "role_menu", "emojis",
                           "pi_count", "suggestions", "tickets", "server_info"]),
    ("Members & invites", ["invites", "level_roles", "economy", "activity", "stats"]),
    ("Games & fun",       ["fun", "games", "rpg", "pvp", "wordle", "chess_cog",
                           "trading", "gear", "gifts", "peepo"]),
    ("AI",                ["ai"]),
    ("Help",              ["help"]),
]

FRIENDLY = {
    "activity": "Activity graphs", "ai": "AI", "altguard": "AltGuard (alt detection)",
    "antinuke": "Anti-nuke", "automation": "Automation", "chess_cog": "Chess",
    "conduct": "Conduct record", "economy": "Economy & levels", "emojis": "Emojis",
    "fun": "Fun", "games": "Games vs the bot", "gear": "Gear", "gifts": "Gifts",
    "help": "Help", "honeypot": "Honeypot", "invites": "Invites",
    "level_roles": "Level roles",
    "link_guard": "LinkGuard", "mod_log": "Mod log & message archive",
    "moderation": "Moderation", "peepo": "Peepo", "pi_count": "Pi counting",
    "pvp": "PvP", "quarantine_lock": "Quarantine lock", "recon_watch": "Recon watch",
    "role_menu": "Reaction roles", "rpg": "RPG", "security": "Security config",
    "security_ai": "Security AI (reviewed verdicts)",
    "server_backup": "Server backup", "server_info": "Server info", "setup": "Setup",
    "simpleverify": "Simple verify",
    "stats": "Stats", "suggestions": "Suggestions", "tickets": "Tickets",
    "trading": "Trading", "verify_prune": "Verify prune", "wordle": "Wordle",
}

SECTION_ICONS = {
    "Security": "🛡️", "Moderation": "🔨", "Server setup": "⚙️",
    "Members & invites": "👥", "Games & fun": "🎮", "AI": "🤖", "Help": "❓",
    "Other": "📦",
}


def section_of(cog: str) -> str:
    for title, cogs in SECTIONS:
        if cog in cogs:
            return title
    return "Other"
