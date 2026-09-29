"""Scam images — pure helpers (no discord import) behind the AutoMod card's
"Scam images" section (Paul, 2026-09-29: "we need to have a mr beast spam
blocker. should be a one click set up on the dashboard").

The spam this was built against: hijacked member accounts post the same four
screenshots (a fake MrBeast profile announcing a crypto casino, the casino's
promo-code page, a "Withdrawal Success" pop-up, a phone showing the payout)
into every channel they can reach, voice-channel chats first and the honeypot
last. Each wave re-encodes and resizes the files and swaps the casino domain,
so the bytes never repeat — file hashes are useless. What the picture LOOKS
like does not change, and that is what is matched here:

  * dHash  — 9x8 grayscale, each bit = "this pixel is brighter than the next"
  * pHash  — 32x32 grayscale, DCT, the 8x8 low-frequency corner against its
             median

An image matches a template when BOTH hashes sit within `threshold` bits of
it. Measured 9/29 over 2,992 cached images: every one of the 263 spam copies
was within 8 bits of its template; the nearest unrelated image was 20 away.

Templates live in data/scam_image_hashes.json so a new wave is a data change.
Pillow is the only dependency. Everything here is deterministic and
unit-tested; cogs/scam_images.py does the Discord I/O.
"""
import io
import json
import math
import os
import sqlite3
import time

from PIL import Image

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "scam_image_hashes.json")
# Same file LinkGuard keeps its catch log in — one durable "what AutoMod caught"
# store rather than a new database per feature.
_HITDB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "linkguard.db")

MODES = ("off", "delete", "timeout", "kick", "ban")
DEFAULT_MODE = "timeout"
DEFAULT_TIMEOUT_MIN = 1440
DEFAULT_THRESHOLD = 10
# Never trust a threshold from the data file past this: at 16+ bits unrelated
# dark screenshots start to look alike and the feature would delete real posts.
MAX_THRESHOLD = 12
MAX_BYTES = 8 * 1024 * 1024
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

_SIZE, _KEEP = 32, 8
_COS = [[math.cos((2 * x + 1) * u * math.pi / (2 * _SIZE)) for x in range(_SIZE)]
        for u in range(_KEEP)]


# ------------------------------------------------------------------ hashing
def dhash(img):
    px = list(img.convert("L").resize((9, 8), Image.LANCZOS).getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


def phash(img):
    px = list(img.convert("L").resize((_SIZE, _SIZE), Image.LANCZOS).getdata())
    rows = [[sum(px[r * _SIZE + x] * _COS[u][x] for x in range(_SIZE)) for u in range(_KEEP)]
            for r in range(_SIZE)]
    flat = [sum(rows[y][u] * _COS[v][y] for y in range(_SIZE))
            for v in range(_KEEP) for u in range(_KEEP)]
    rest = sorted(flat[1:])          # the DC term is brightness, not shape
    med = rest[len(rest) // 2]
    bits = 0
    for f in flat:
        bits = (bits << 1) | (f > med)
    return bits


def hash_bytes(data):
    """(dhash, phash) for image bytes, or None when it isn't a readable image.
    Blocking — callers run it in a thread."""
    try:
        img = Image.open(io.BytesIO(data))
        # JPEG only: decode at reduced size. The hashes look at a 32x32 view, so
        # nothing is lost, and a 12-megapixel photo costs a fraction to open.
        img.draft("L", (64, 64))
        img.load()
        return dhash(img), phash(img)
    except Exception:
        return None


def hamming(a, b):
    return bin(a ^ b).count("1")


# ---------------------------------------------------------------- templates
def load_templates(path=_DATA):
    """→ (templates, threshold). templates = [{family, name, d, p}]. A missing
    or broken file yields no templates: the feature goes quiet, never wrong."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return [], DEFAULT_THRESHOLD
    out = []
    for fam in raw.get("families") or []:
        for t in fam.get("templates") or []:
            try:
                out.append({"family": str(fam.get("key") or "scam"),
                            "label": str(fam.get("label") or fam.get("key") or "Scam image"),
                            "name": str(t.get("name") or ""),
                            "d": int(str(t["dhash"]), 16), "p": int(str(t["phash"]), 16)})
            except (KeyError, TypeError, ValueError):
                continue
    try:
        thr = int(raw.get("threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        thr = DEFAULT_THRESHOLD
    return out, max(0, min(thr, MAX_THRESHOLD))


def match(hashes, templates, threshold=DEFAULT_THRESHOLD):
    """hashes = (dhash, phash) → the closest template within `threshold` on BOTH
    hashes, as {family, label, name, distance}; else None."""
    if not hashes:
        return None
    d, p = hashes
    best = None
    for t in templates:
        dist = max(hamming(d, t["d"]), hamming(p, t["p"]))
        if dist <= threshold and (best is None or dist < best["distance"]):
            best = {"family": t["family"], "label": t["label"], "name": t["name"],
                    "distance": dist}
    return best


def is_image(filename, content_type):
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return True
    return not ct and str(filename or "").lower().endswith(IMAGE_EXTS)


# ------------------------------------------------------------------- config
def mode(cfg):
    """What to do on a match. The AutoMod master switch off reads as "off"; an
    unknown value reads as the default rather than as something harsher."""
    if not bool((cfg or {}).get("automod_enabled", 1)):
        return "off"
    m = str((cfg or {}).get("automod_scamimg_mode") or DEFAULT_MODE).lower()
    return m if m in MODES else DEFAULT_MODE


def timeout_minutes(cfg):
    try:
        n = int((cfg or {}).get("automod_scamimg_timeout_min") or DEFAULT_TIMEOUT_MIN)
    except (TypeError, ValueError):
        n = DEFAULT_TIMEOUT_MIN
    return max(1, min(n, 40320))     # Discord's 28-day ceiling


def staff_exempt(cfg):
    """Off by default: the posters are hijacked accounts, and a hijacked mod
    posts the same four pictures."""
    return bool((cfg or {}).get("automod_scamimg_exempt_staff", 0))


# ------------------------------------------------------------------- ledger
def _db(path=None):
    c = sqlite3.connect(path or _HITDB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db(path=None):
    with _db(path) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS scam_image_hits (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                   guild_id TEXT, user_id TEXT, username TEXT,
                   channel_id TEXT, message_id TEXT,
                   family TEXT, template TEXT, distance INTEGER, images INTEGER,
                   mode TEXT, deleted INTEGER, action TEXT, failed TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sih_user "
                  "ON scam_image_hits(guild_id, user_id, ts)")


def record_hit(row, path=None):
    with _db(path) as c:
        c.execute(
            "INSERT INTO scam_image_hits(ts,guild_id,user_id,username,channel_id,message_id,"
            "family,template,distance,images,mode,deleted,action,failed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row.get("ts") or time.time(), str(row["guild_id"]), str(row["user_id"]),
             row.get("username"), str(row["channel_id"]), str(row["message_id"]),
             row.get("family"), row.get("template"), int(row.get("distance") or 0),
             int(row.get("images") or 0), row.get("mode"), int(bool(row.get("deleted"))),
             row.get("action"), row.get("failed")))


def count_hits(guild_id, user_id, path=None):
    try:
        with _db(path) as c:
            return c.execute("SELECT COUNT(*) FROM scam_image_hits WHERE guild_id=? AND user_id=?",
                             (str(guild_id), str(user_id))).fetchone()[0]
    except sqlite3.Error:
        return 0
