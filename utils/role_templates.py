"""Ready-made self-assign role sets.

The original `/rolemenu bootstrap` could only rebuild ONE server's panels: it
was a hardcoded list of this guild's role IDs, so it was a migration tool, not a
feature. These templates are guild-agnostic — they describe roles by NAME,
emoji and colour, so any server can stand up a normal set in one command and the
bot creates whatever doesn't exist yet.

Nothing here grants a permission. Every role is created with none, which is what
makes one-click creation safe to hand to any admin.

`exclusive` means a member may hold only one option from that panel (an age band,
one DM preference); the panel enforces it when the button is clicked.
"""

# name, emoji, colour (0 = Discord default/no colour)
TEMPLATES = {
    "pronouns": {
        "title": "🏷️ Pronouns",
        "blurb": "Let people know how to refer to you. Pick as many as fit.",
        "exclusive": False,
        "roles": [
            ("She/Her", "🌸", 0xE91E63),
            ("He/Him", "🌊", 0x3498DB),
            ("They/Them", "🌿", 0x2ECC71),
            ("Any pronouns", "✨", 0x9B59B6),
            ("Ask my pronouns", "❓", 0x95A5A6),
        ],
    },
    "age": {
        "title": "🎂 Age",
        "blurb": ("Pick your age range — you can only hold one, and choosing a new one "
                  "replaces the old.\n\nThis helps staff keep the server safe for everyone."),
        "exclusive": True,
        "roles": [
            ("13-15", "🐣", 0x7FB069),
            ("16-17", "🌱", 0x4E9F3D),
            ("18-21", "✨", 0x5B8CFF),
            ("22-27", "🍷", 0x9B59B6),
            ("28+", "🧭", 0xD4AF37),
        ],
    },
    "regions": {
        "title": "🌍 Region",
        "blurb": "Where in the world are you? Handy for event timing.",
        "exclusive": False,
        "roles": [
            ("North America", "🌎", 0x3498DB),
            ("South America", "🌎", 0x1ABC9C),
            ("Europe", "🌍", 0x9B59B6),
            ("Africa", "🌍", 0xE67E22),
            ("Asia", "🌏", 0xE91E63),
            ("Oceania", "🌏", 0x2ECC71),
        ],
    },
    "notifications": {
        "title": "🔔 Notifications",
        "blurb": "Opt into the pings you actually want. No role, no ping.",
        "exclusive": False,
        "roles": [
            ("Announcements", "📢", 0x5B8CFF),
            ("Giveaways", "🎉", 0xF1C40F),
            ("Events", "🎮", 0x2ECC71),
            ("Polls", "📊", 0x9B59B6),
            ("Bump Squad", "👋", 0xE67E22),
            ("Revive", "⚡", 0x95A5A6),
        ],
    },
    "colours": {
        "title": "🎨 Name colour",
        "blurb": "Pick a colour for your name. One at a time.",
        "exclusive": True,
        "roles": [
            ("Red", "❤️", 0xE74C3C),
            ("Orange", "🧡", 0xE67E22),
            ("Yellow", "💛", 0xF1C40F),
            ("Green", "💚", 0x2ECC71),
            ("Blue", "💙", 0x3498DB),
            ("Purple", "💜", 0x9B59B6),
            ("Pink", "🩷", 0xE91E63),
        ],
    },
    # The full palette a colour-focused server expects (three tiers of every
    # hue plus neutrals) — 25 options is exactly Discord's one-message button
    # cap, so this is as big as a single pick-one panel can ever be.
    "colours_full": {
        "title": "🎨 Name colour — full palette",
        "blurb": "Pick a colour for your name. One at a time — choosing a new one replaces the old.",
        "exclusive": True,
        "roles": [
            ("Dark red", "❤️", 0x4B0000),
            ("Dark orange", "🧡", 0x883904),
            ("Dark yellow", "💛", 0x89590C),
            ("Dark green", "💚", 0x1A5F09),
            ("Dark blue", "💙", 0x123456),
            ("Dark purple", "💜", 0x3B0B5A),
            ("Dark pink", "🩷", 0xA60059),
            ("Red", "❤️", 0xFF0004),
            ("Orange", "🧡", 0xFF7100),
            ("Yellow", "💛", 0xFFEE00),
            ("Green", "💚", 0x24FF00),
            ("Blue", "💙", 0x00C6FF),
            ("Purple", "💜", 0xB900FF),
            ("Pink", "🩷", 0xFF007C),
            ("Pastel red", "❤️", 0xFF7575),
            ("Pastel orange", "🧡", 0xFFBE5B),
            ("Pastel yellow", "💛", 0xFBFF85),
            ("Pastel green", "💚", 0x98FF8B),
            ("Pastel blue", "💙", 0xAAC5FF),
            ("Pastel purple", "💜", 0xF0A8FF),
            ("Pastel pink", "🩷", 0xFFABCD),
            ("Black", "🖤", 0x0D0D0D),
            ("White", "🤍", 0xFFFFFF),
            ("Light gray", "🩶", 0x818181),
            ("Beige", "🤎", 0xCCB08C),
        ],
    },
    "sexuality": {
        "title": "🏳️‍🌈 Sexuality",
        "blurb": "Pick what fits you. Optional, and you can pick more than one.",
        "exclusive": False,
        "roles": [
            ("Straight", "🔷", 0x607D8B),
            ("Gay", "🌈", 0x11806A),
            ("Lesbian", "🧡", 0xE67E22),
            ("Bisexual", "💜", 0x71368A),
            ("Pansexual", "💛", 0xF1C40F),
        ],
    },
    "platforms": {
        "title": "🎮 Platform",
        "blurb": "What do you play on? Pick all that apply.",
        "exclusive": False,
        "roles": [
            ("PC", "🖥️", 0x5B8CFF),
            ("PlayStation", "🎮", 0x003791),
            ("Xbox", "🎮", 0x107C10),
            ("Nintendo", "🎮", 0xE60012),
            ("Mobile", "📱", 0x95A5A6),
        ],
    },
    "dms": {
        "title": "💬 DM preference",
        "blurb": "Tell people how you'd rather be contacted. One choice.",
        "exclusive": True,
        "roles": [
            ("DMs open", "🟢", 0x2ECC71),
            ("Ask before DMing", "🟡", 0xF1C40F),
            ("DMs closed", "🔴", 0xE74C3C),
        ],
    },
}

# Discord allows at most 5 rows x 5 buttons on a message; every template above
# stays well inside that, but a template added later must not silently truncate.
MAX_BUTTONS = 25


def template_choices():
    """(key, title) pairs for building the slash-command choice list."""
    return [(k, t["title"]) for k, t in TEMPLATES.items()]


def as_json():
    """The templates as plain data, for the web dashboard.

    The bot writes this to a shared file on start so the dashboard's "start from
    a template" picker can never drift from what `/rolemenu template` builds —
    there is exactly one definition, here.
    """
    return {
        key: {
            "title": t["title"],
            "blurb": t["blurb"],
            "exclusive": bool(t["exclusive"]),
            "roles": [{"name": n, "emoji": e, "colour": c} for n, e, c in t["roles"]],
        }
        for key, t in TEMPLATES.items()
    }


def validate():
    """Fail loudly at import time rather than posting a broken panel."""
    for key, t in TEMPLATES.items():
        assert t["roles"], f"{key}: no roles"
        assert len(t["roles"]) <= MAX_BUTTONS, f"{key}: too many buttons for one message"
        names = [r[0].lower() for r in t["roles"]]
        assert len(names) == len(set(names)), f"{key}: duplicate role names"
        for name, emoji, colour in t["roles"]:
            assert name and emoji, f"{key}: {name!r} missing name or emoji"
            assert 0 <= colour <= 0xFFFFFF, f"{key}: {name} colour out of range"


validate()
