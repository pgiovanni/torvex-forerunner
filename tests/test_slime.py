"""utils/slime.py — the !slime picker. Pure, runs anywhere:
    py tests/test_slime.py   (exits non-zero on failure)
"""
import json
import os
import random
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from utils import slime  # noqa: E402

failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


# Shipped config parses and has the four launch GIFs + the ping target.
cfg = slime.load_config()
check("ping target set", cfg.ping_user_id == 1140944880638640161)
check("four launch gifs", len(cfg.gifs) == 4)
check("all https klipy links", all(g.startswith("https://klipy.com/gifs/") for g in cfg.gifs))

# Junk entries are dropped, duplicates collapsed, bad ping id -> None.
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
    json.dump({"ping_user_id": "nope", "gifs": ["https://a/1", "http://insecure", "", "https://a/1", 42]}, fh)
    tmp = fh.name
try:
    c2 = slime.load_config(tmp)
    check("junk dropped, dedup", c2.gifs == ["https://a/1"])
    check("bad ping id -> None", c2.ping_user_id is None)
finally:
    os.unlink(tmp)

# pick(): never the same GIF twice in a row when there is a choice ...
rng = random.Random(1)
last = None
repeats = 0
for _ in range(500):
    g = slime.pick(cfg.gifs, last=last, rng=rng)
    repeats += g == last
    last = g
check("no back-to-back repeats", repeats == 0)
# ... but a single-GIF list still works, and an empty one raises.
check("single gif ok", slime.pick(["https://x"], last="https://x") == "https://x")
try:
    slime.pick([])
    check("empty raises", False)
except ValueError:
    check("empty raises", True)

# build_content: exactly one mention, GIF on its own line so Discord unfurls it.
out = slime.build_content("https://g", "<@123>")
check("one mention", out.count("<@123>") == 1)
check("gif on own line", out.endswith("\nhttps://g"))
check("no mention when None", slime.build_content("https://g", None) == "🟢 slime!\nhttps://g")

if failures:
    print(f"\n{len(failures)} failing: {failures}")
    sys.exit(1)
print("\nall slime tests passed")
