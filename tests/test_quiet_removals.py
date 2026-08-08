"""Quiet-removal marks — pure logic, no clock sleeping (time is injected).
Run:  /opt/peepos-reclaimer/venv/bin/python tests/test_quiet_removals.py
Exits non-zero on any failure.
"""
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from utils import quiet_removals as q  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


NOW = time.time()

# ---- unmarked uids are never silenced ----
q.clear()
check("unknown uid is loud", q.is_quiet(123) is False)

# ---- a mark silences, and does not consume ----
q.mark(123, ttl=60)
check("marked uid is quiet", q.is_quiet(123, now=NOW) is True)
check("second listener gets the same answer", q.is_quiet(123, now=NOW) is True)
check("third listener too", q.is_quiet(123, now=NOW) is True)
check("a different uid is unaffected", q.is_quiet(999, now=NOW) is False)

# ---- string/int uids are the same key (discord ids arrive both ways) ----
check("str uid matches int mark", q.is_quiet("123", now=NOW) is True)

# ---- expiry is read-time: silence ends on its own ----
check("still quiet just before expiry", q.is_quiet(123, now=NOW + 59) is True)
check("loud again after expiry", q.is_quiet(123, now=NOW + 61) is False)
check("lapsed mark is dropped on read", q.active(now=NOW + 61) == [])

# ---- ttl is clamped, never unbounded ----
q.clear()
exp = q.mark(7, ttl=99999)
check("ttl clamped to MAX_TTL", exp - time.time() <= q.MAX_TTL + 1)
q.mark(8, ttl=-5)
check("negative ttl is instantly lapsed", q.is_quiet(8) is False)

# ---- clear ----
q.clear()
q.mark(1, ttl=60); q.mark(2, ttl=60); q.mark(3, ttl=60)
check("clear(uid) drops one", q.clear(2) == 1 and q.is_quiet(1) and not q.is_quiet(2))
check("clear(uid) on a miss returns 0", q.clear(4242) == 0)
check("clear() drops the rest", q.clear() == 2 and q.active() == [])

# ---- active() reports what is silenced, soonest first ----
q.clear()
q.mark(10, ttl=90); q.mark(11, ttl=30)
order = [u for u, _ in q.active(now=time.time())]
check("active() sorted by time remaining", order == [11, 10])
check("active() reports both marks", len(q.active()) == 2)
q.clear()

print(f"\n{_total - len(_fails)}/{_total} passed")
sys.exit(1 if _fails else 0)
