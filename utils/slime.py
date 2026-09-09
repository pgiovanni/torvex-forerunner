"""`!slime` — pick a SFW slime GIF and build the message. Pure, no discord import.

Config lives in data/slime_gifs.json (GIF list + who to ping) so the list can
grow without a code deploy. Never repeats the previous GIF in the same channel.
"""
import json
import os
import random
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data", "slime_gifs.json")


@dataclass
class SlimeConfig:
    ping_user_id: int | None = None
    gifs: list[str] = field(default_factory=list)


def load_config(path: str = DATA_PATH) -> SlimeConfig:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    gifs: list[str] = []
    for g in raw.get("gifs", []):
        g = str(g).strip()
        if g.startswith("https://") and g not in gifs:
            gifs.append(g)
    uid = raw.get("ping_user_id")
    try:
        uid = int(uid) if uid else None
    except (TypeError, ValueError):
        uid = None
    return SlimeConfig(ping_user_id=uid, gifs=gifs)


def pick(gifs: list[str], last: str | None = None, rng=random) -> str:
    """Random GIF, avoiding `last` when there is any alternative."""
    if not gifs:
        raise ValueError("no slime gifs configured")
    pool = [g for g in gifs if g != last] or list(gifs)
    return rng.choice(pool)


def build_content(gif: str, mention: str | None) -> str:
    """One line of text (with the single ping, if any) then the GIF link on
    its own line so Discord unfurls it."""
    head = f"{mention} 🟢 slime!" if mention else "🟢 slime!"
    return f"{head}\n{gif}"
