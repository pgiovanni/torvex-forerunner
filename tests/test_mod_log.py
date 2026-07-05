"""Mod-log logic harness — exercises the pure helpers of cogs/mod_log.py
against synthetic audit entries / archive rows, no live Discord.

The attribution matcher is the correctness-critical piece: Discord AGGREGATES
message_delete audit entries (same mod + same author + same channel within a
few minutes = ONE entry whose `count` bumps), so "a new entry appeared" is not
the only evidence of a mod delete — "a known entry's count grew" must also
attribute, and a stale never-seen entry must NOT.

Run on any box with discord.py importable:
    /opt/peepos-reclaimer/venv/bin/python tests/test_mod_log.py
Exits non-zero on any failure.
"""
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.mod_log as ml  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


NOW = time.time()
CH, AUTHOR, MOD = 111, 222, 333


def entry(eid, count=1, age=5.0, target=AUTHOR, channel=CH, user=MOD):
    return {"id": eid, "user_id": user, "user_name": f"mod{user}",
            "target_id": target, "channel_id": channel,
            "count": count, "created_ts": NOW - age}


# 1) fresh new entry matching channel+author → attributed
cache = {}
hit = ml.match_delete_entry([entry(1)], cache, CH, AUTHOR, NOW)
check("fresh new entry attributes", hit and hit["user_id"] == MOD)
check("cache learned the entry", cache.get(1) == 1)

# 2) same entry again, count unchanged → NOT attributed (that delete is old news)
hit = ml.match_delete_entry([entry(1)], cache, CH, AUTHOR, NOW)
check("unchanged known entry does not attribute", hit is None)

# 3) the aggregation trick: known entry's count grew → attributed even though old
hit = ml.match_delete_entry([entry(1, count=2, age=290)], cache, CH, AUTHOR, NOW)
check("count bump on aggregated entry attributes", hit and hit["user_id"] == MOD)
check("cache tracked the bumped count", cache.get(1) == 2)

# 4) stale entry never seen before (bot just restarted, unprimed) → NOT evidence
hit = ml.match_delete_entry([entry(9, age=600)], {}, CH, AUTHOR, NOW)
check("stale unseen entry does not attribute", hit is None)

# 5) wrong channel / wrong author → no match, but cache still learns it
cache = {}
hit = ml.match_delete_entry([entry(2, channel=999)], cache, CH, AUTHOR, NOW)
check("wrong channel does not attribute", hit is None)
check("non-matching entry still cached", cache.get(2) == 1)
hit = ml.match_delete_entry([entry(3, target=777)], cache, CH, AUTHOR, NOW)
check("wrong author does not attribute", hit is None)

# 6) bulk mode: author_id=None skips the target check
hit = ml.match_delete_entry([entry(4, target=None)], {}, CH, None, NOW)
check("bulk match ignores target", hit is not None)

# 7) newest matching entry wins when several qualify
hits2 = ml.match_delete_entry([entry(20, age=1), entry(10, age=2)], {}, CH, AUTHOR, NOW)
check("newest entry preferred", hits2 and hits2["id"] == 20)

# 8) self-delete: empty audit page → None
check("no entries = self delete", ml.match_delete_entry([], {}, CH, AUTHOR, NOW) is None)

# ---- transcript ----
rows = [
    {"message_id": "200", "author_id": "2", "author_name": "beta",
     "created_ts": NOW, "content": "second", "attachments": None},
    {"message_id": "100", "author_id": "1", "author_name": "alpha",
     "created_ts": NOW - 60, "content": "first",
     "attachments": '[{"filename": "cat.png"}]'},
]
txt = ml.build_transcript(rows, "TestGuild")
check("transcript chronological (snowflake order)", txt.index("first") < txt.index("second"))
check("transcript names authors", "alpha (1)" in txt)
check("transcript lists attachments", "cat.png" in txt)

# ---- filename sanitizer ----
check("path separators neutralized", "/" not in ml.safe_filename("../../etc/passwd")
      and "\\" not in ml.safe_filename("..\\..\\x.png"))
check("normal name preserved", ml.safe_filename("IMG_1234.png") == "IMG_1234.png")
check("long name capped", len(ml.safe_filename("a" * 300 + ".png")) <= 80)
check("empty name safe", ml.safe_filename("") == "file")

# ---- truncation ----
check("short content untouched", ml._trunc("hi") == "hi")
check("long content capped at limit", len(ml._trunc("x" * 5000)) == 1024)
check("none content ok", ml._trunc(None) == "")

# ---- media prune selection ----
old, new = ("m1", NOW - 40 * 86400), ("m2", NOW - 1 * 86400)
sel = ml.files_to_prune([old, new], NOW - 30 * 86400)
check("prunes only files past retention", sel == ["m1"])

print(f"\n{_total - len(_fails)}/{_total} passed")
sys.exit(1 if _fails else 0)
