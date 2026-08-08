"""Removals the bot carries out without saying anything.

A uid marked here leaves with NO channel announcement: no goodbye message, no
mod-log embed, no leave-triggered automation rule. What does not change is the
recording — `member_events` and `identity_events` still get their rows, and
Discord's own audit log is untouched. This suppresses NOISE, not evidence:
"did X leave or were they kicked?" has to stay answerable next month, and the
whole value of the ledger is that it never disagrees with Discord's.

Same shape as `antinuke_window`, for the same reasons:

  * A mark is TIME-BOXED. Silence is a thing you open for a moment, not a
    property a uid carries forever — a uid quietened in August must not
    swallow a real departure in November.
  * Expiry is READ-TIME, never a scheduled write. `is_quiet()` compares a
    timestamp, so every failure mode in this file ends the silence EARLY.
    Nothing has to run for a mark to lapse.
  * A restart clears every mark, which is also the safe direction.

`is_quiet()` deliberately does not consume the mark: four separate listeners
ask about the same removal and all four must get the same answer.
"""
import time

DEFAULT_TTL = 120.0   # comfortably outlives mod_log's ~4.5s audit-classify wait
MAX_TTL = 900.0

_marks = {}  # uid:int -> expires_at:float


def mark(uid, ttl=DEFAULT_TTL):
    """Silence the next removal of `uid`. Returns the expiry timestamp."""
    ttl = max(0.0, min(float(ttl), MAX_TTL))
    exp = time.time() + ttl
    _marks[int(uid)] = exp
    return exp


def is_quiet(uid, now=None):
    """True if this removal should be announced nowhere. Non-consuming."""
    now = time.time() if now is None else now
    exp = _marks.get(int(uid))
    if exp is None:
        return False
    if exp <= now:
        del _marks[int(uid)]     # lapsed marks clean themselves up on read
        return False
    return True


def clear(uid=None):
    """Drop one mark, or all of them. Returns how many were dropped."""
    if uid is None:
        n = len(_marks)
        _marks.clear()
        return n
    return 1 if _marks.pop(int(uid), None) is not None else 0


def active(now=None):
    """[(uid, seconds_left)] for every live mark, soonest to expire first —
    so an operator can see what is currently silenced and why nothing posted."""
    now = time.time() if now is None else now
    live = [(u, e - now) for u, e in _marks.items() if e > now]
    return sorted(live, key=lambda p: p[1])
