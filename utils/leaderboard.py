"""Pure helpers for the /chat-levels rankings card (no discord import).

Ranking rule (Paul, 9/10): rank by chat LEVEL, then XP — and show the XP, so
three Lv.13 members don't look arbitrarily ordered.

Currency rule: Peepo Bucks 💰 are earned only in the home community, so the
card only shows them there. Everywhere else the money column is the server's
own Regular/Server Bucks 💵. The "bucks" sort follows the same rule.
"""

MEDALS = ["🥇", "🥈", "🥉"]


def order_clause(by_bucks: bool, is_home: bool, local: bool) -> str:
    """SQL ORDER BY body for the leaderboard query."""
    if by_bucks:
        if local:
            return "peepo_bucks DESC, g.xp DESC" if is_home else "regular_bucks DESC, g.xp DESC"
        return "peepo_bucks DESC, xp DESC" if is_home else "regular_bucks DESC, xp DESC"
    if local:
        return "g.level DESC, g.xp DESC"
    return "level DESC, xp DESC"


def bucks_label(is_home: bool) -> str:
    return "Peepo Bucks 💰" if is_home else "Server Bucks 💵"


def format_line(i: int, row, is_home: bool) -> str:
    """One leaderboard row. `row` is any mapping with discord_id, level, xp,
    peepo_bucks, regular_bucks."""
    place = MEDALS[i] if i < 3 else f"{i + 1}."
    money = (f"{row['peepo_bucks']:,} 💰 | " if is_home else "") + f"{row['regular_bucks']:,} 💵"
    return f"{place} <@{row['discord_id']}> — Lv.{row['level']} · {row['xp']:,} XP | {money}"
