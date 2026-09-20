"""AutoMod — pure helpers (no discord import) for the two features configured
on the dashboard's Moderation card (Paul, 2026-09-10: "moderation is the same
as auto mod, just build it in there"):

  * LINK POLICY — what happens when an ordinary member posts a link at all
    (as opposed to LinkGuard's malicious-domain hitlist). Modes: off /
    delete / timeout. Staff, exempt roles/channels and members holding a
    temporary LINK PASS (`/hitlist pass`) are let through. Discord's own
    domains and the GIF pickers are always allowed — a policy that ate every
    Tenor GIF would be switched off within the hour.

  * RAID DETECTION — N human joins inside W seconds trips once per cooldown;
    the response (alert / pause invites / quarantine / kick) is the admin's
    choice on the panel.

Everything here is deterministic and unit-tested; the cogs do the Discord I/O.
"""
import re
import time
from urllib.parse import urlparse

# Never policed: Discord itself + the GIF pickers every client embeds. A GIF is
# not the link problem anyone turns this feature on for, and deleting one reads
# to the member as the bot breaking — klipy.com was eating real posts at home
# the night the policy went to `delete` (2026-09-19).
ALWAYS_ALLOWED = (
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.new",
    "discord.media", "tenor.com", "tenor.co", "giphy.com", "gfycat.com",
    "klipy.com", "klipy.app",
)

LINK_MODES = ("off", "delete", "timeout")
RAID_ACTIONS = ("alert", "invites_off", "quarantine", "kick")

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]{}\"'`|\\]+", re.I)
_MASKED_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?\s*\)")


def extract_urls(content):
    """Every URL-ish token in a message body, masked-link targets included."""
    if not content:
        return []
    found = _URL_RE.findall(content)
    found += _MASKED_RE.findall(content)
    out, seen = [], set()
    for u in found:
        u = u.rstrip(".,;:!?")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def host_of(url):
    u = url if re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) else "http://" + url
    try:
        return (urlparse(u).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def host_allowed(host, allow):
    """Suffix match on domain labels: 'cdn.discordapp.com' is allowed by
    'discordapp.com'; 'notdiscord.com' is not."""
    if not host:
        return True  # nothing resolvable = nothing to police
    for rule in list(ALWAYS_ALLOWED) + [str(a).strip().lower().lstrip("*.") for a in (allow or []) if a]:
        if host == rule or host.endswith("." + rule):
            return True
    return False


def blocked_urls(content, allow):
    """URLs in `content` that the link policy would act on."""
    return [u for u in extract_urls(content) if not host_allowed(host_of(u), allow)]


def link_mode(cfg):
    m = str(cfg.get("automod_links_mode") or "off").lower()
    return m if m in LINK_MODES else "off"


def pass_active(row, now=None):
    """row = {expires_ts} or None → is the member's link pass still good?"""
    if not row:
        return False
    try:
        return float(row["expires_ts"]) > (now if now is not None else time.time())
    except (KeyError, TypeError, ValueError):
        return False


def raid_window(cfg):
    pair = cfg.get("automod_raid") or [10, 30]
    try:
        count, window = int(pair[0]), int(pair[1])
    except (TypeError, ValueError, IndexError):
        count, window = 10, 30
    return max(2, count), max(5, window)


def raid_action(cfg):
    a = str(cfg.get("automod_raid_action") or "alert").lower()
    return a if a in RAID_ACTIONS else "alert"


def log_channel_id(cfg):
    """Where AutoMod posts: the Moderation card's log channel, then the usual
    fallbacks. Returns an int id or None."""
    for key in ("mod_log_channel_id", "msglog_channel_id", "modlog_channel_id"):
        cid = cfg.get(key)
        if cid:
            try:
                return int(cid)
            except (TypeError, ValueError):
                continue
    return None


def raid_tripped(join_ts, count, window, now):
    """join_ts = iterable of join timestamps (any order). True when at least
    `count` of them fall inside the last `window` seconds."""
    cutoff = now - window
    return sum(1 for t in join_ts if t >= cutoff) >= count


def prune_joins(join_ts, window, now):
    cutoff = now - window
    return [t for t in join_ts if t >= cutoff]
