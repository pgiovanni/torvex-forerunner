"""Cross-channel bursts — pure helpers (no discord import) behind the AutoMod
card's "Same message across channels" section (Paul, 2026-09-29: "a cross
channel burst rule is good").

One member posting the SAME thing in several channels inside a few seconds is
how every spam tool that has hit the home server behaves — the image blasts
(four pictures into eight channels in twenty seconds), the 9/13 advert menu
(same text, seven channels), the 8/31 invite selfbot. Each stayed under the
per-channel flood rule because no single channel saw more than two messages.
This rule counts CHANNELS, not messages.

"The same thing" is a signature of the message: its text with case and
spacing flattened, the sizes of its attachments, and its stickers. Nothing is
downloaded to build it. Short text-only and sticker-only messages are left out
— "lol" in three channels is a person, not a tool.

It is also the backstop for utils/scam_images.py: a scam picture that has been
redesigned gets past the fingerprint match, and is caught here on the third
channel instead. The fingerprints of what it caught are kept with the ledger
row so the operator can promote them to templates.

Everything here is deterministic and unit-tested; cogs/crosspost.py does the
Discord I/O.
"""
import hashlib
import json
import os
import re
import sqlite3
import time

_HITDB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "linkguard.db")

MODES = ("off", "delete", "timeout", "kick", "ban")
DEFAULT_MODE = "timeout"
DEFAULT_BURST = (3, 15)          # channels, seconds
DEFAULT_TIMEOUT_MIN = 1440
# Text-only messages shorter than this never count. Attachments, stickers and
# links always do.
MIN_TEXT = 24
MAX_WINDOW = 120

_WS = re.compile(r"\s+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)


# ---------------------------------------------------------------- signature
def normalize(text):
    return _WS.sub(" ", (text or "").strip().lower())


def signature(content, attachment_sizes=(), sticker_ids=()):
    """→ a short hex signature, or None when the message is too slight to
    count (no files, no link, and under MIN_TEXT characters). A sticker on its
    own is slight: the only sticker-only burst in the archive (8/21, replayed
    9/29) was a member, not a tool."""
    text = normalize(content)
    sizes = sorted(int(s) for s in (attachment_sizes or ()))
    stickers = sorted(str(s) for s in (sticker_ids or ()))
    if not sizes and len(text) < MIN_TEXT and not _URL.search(text):
        return None
    blob = json.dumps([text, sizes, stickers], separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ window
def prune(events, window, now):
    cutoff = now - window
    return [e for e in events if e["ts"] >= cutoff]


def burst(events, sig, channels_needed, window, now):
    """events = [{ts, channel_id, message_id, sig}] for ONE member. → the events
    that make up the burst (same signature, inside the window) when they span at
    least `channels_needed` different channels; else None."""
    cutoff = now - window
    same = [e for e in events if e["sig"] == sig and e["ts"] >= cutoff]
    if len({e["channel_id"] for e in same}) >= channels_needed:
        return same
    return None


# ------------------------------------------------------------------- config
def mode(cfg):
    """The AutoMod master switch off reads as "off"; an unknown value reads as
    the default rather than as something harsher."""
    if not bool((cfg or {}).get("automod_enabled", 1)):
        return "off"
    m = str((cfg or {}).get("automod_burst_mode") or DEFAULT_MODE).lower()
    return m if m in MODES else DEFAULT_MODE


def burst_window(cfg):
    """→ (channels, seconds). Two channels is cross-posting, not a burst, so the
    floor is 3 whatever the panel says."""
    pair = (cfg or {}).get("automod_burst") or DEFAULT_BURST
    try:
        channels, seconds = int(pair[0]), int(pair[1])
    except (TypeError, ValueError, IndexError):
        channels, seconds = DEFAULT_BURST
    return max(3, min(channels, 25)), max(3, min(seconds, MAX_WINDOW))


def timeout_minutes(cfg):
    try:
        n = int((cfg or {}).get("automod_burst_timeout_min") or DEFAULT_TIMEOUT_MIN)
    except (TypeError, ValueError):
        n = DEFAULT_TIMEOUT_MIN
    return max(1, min(n, 40320))


def staff_exempt(cfg):
    """On by default, unlike the scam-image match: this rule judges behaviour,
    and a moderator posting one notice in three channels is doing their job."""
    return bool((cfg or {}).get("automod_burst_exempt_staff", 1))


def exempt_channels(cfg):
    return {str(c) for c in ((cfg or {}).get("automod_burst_exempt_channels") or [])}


# ------------------------------------------------------------------- ledger
def _db(path=None):
    c = sqlite3.connect(path or _HITDB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db(path=None):
    with _db(path) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS burst_hits (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                   guild_id TEXT, user_id TEXT, username TEXT,
                   sig TEXT, channels TEXT, messages TEXT, span REAL,
                   content TEXT, attachments INTEGER, image_hashes TEXT,
                   mode TEXT, deleted INTEGER, action TEXT, failed TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bh_user ON burst_hits(guild_id, user_id, ts)")


def record_hit(row, path=None):
    with _db(path) as c:
        c.execute(
            "INSERT INTO burst_hits(ts,guild_id,user_id,username,sig,channels,messages,span,"
            "content,attachments,image_hashes,mode,deleted,action,failed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row.get("ts") or time.time(), str(row["guild_id"]), str(row["user_id"]),
             row.get("username"), row.get("sig"),
             json.dumps([str(c) for c in row.get("channels") or []]),
             json.dumps([str(m) for m in row.get("messages") or []]),
             float(row.get("span") or 0), (row.get("content") or "")[:500],
             int(row.get("attachments") or 0), json.dumps(row.get("image_hashes") or []),
             row.get("mode"), int(row.get("deleted") or 0), row.get("action"), row.get("failed")))


def count_hits(guild_id, user_id, path=None):
    try:
        with _db(path) as c:
            return c.execute("SELECT COUNT(*) FROM burst_hits WHERE guild_id=? AND user_id=?",
                             (str(guild_id), str(user_id))).fetchone()[0]
    except sqlite3.Error:
        return 0
