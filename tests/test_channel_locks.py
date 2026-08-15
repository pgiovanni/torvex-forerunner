"""Tests for utils/channel_locks.py — the /lock // /unlock state store.

Pure sqlite/json, no discord import: runs in the local venv AND on the VPS.
Standalone-script style like the other older test files (run it directly).
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import channel_locks as cl  # noqa: E402

failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def main():
    db = os.path.join(tempfile.mkdtemp(), "locks_test.db")

    # tri-state round trip — the null (=inherit) surviving is the whole point
    prev = cl.pack_prev(
        {"everyone": {"send_messages": None, "create_public_threads": False},
         "role:123": {"send_messages": True},
         "member:456": {"send_messages": False}},
        {"send_messages": None})
    out = cl.unpack_prev(prev)
    check("None survives json round trip", out["targets"]["everyone"]["send_messages"] is None)
    check("False survives", out["targets"]["everyone"]["create_public_threads"] is False)
    check("role target round trips", out["targets"]["role:123"]["send_messages"] is True)
    check("member target round trips", out["targets"]["member:456"]["send_messages"] is False)
    check("me scope round trips", out["me"]["send_messages"] is None)

    # v1 rows (@everyone-only locks from 2026-08-14) lift into the targets shape
    v1 = '{"everyone": {"send_messages": null, "connect": false}, "me": {"send_messages": true}}'
    out = cl.unpack_prev(v1)
    check("v1 lifts to targets", out["targets"]["everyone"]["connect"] is False)
    check("v1 keeps tri-state", out["targets"]["everyone"]["send_messages"] is None)
    check("v1 me survives", out["me"]["send_messages"] is True)

    # save / get / clear
    check("no lock yet", cl.get_lock(1, 10, db=db) is None)
    cl.save_lock(1, 10, 999, "mod#1", "raid", prev, db=db)
    row = cl.get_lock(1, 10, db=db)
    check("lock stored", row is not None)
    check("who", row["locked_by"] == 999 and row["locked_by_name"] == "mod#1")
    check("reason", row["reason"] == "raid")
    check("prev restored through storage", row["prev"]["targets"]["everyone"]["send_messages"] is None)
    check("ts present", isinstance(row["locked_ts"], int) and row["locked_ts"] > 0)

    # guild scoping — same channel id in another guild is NOT the same lock
    check("guild-scoped get", cl.get_lock(2, 10, db=db) is None)
    cl.save_lock(2, 10, 111, "other#2", None, cl.pack_prev({}, {}), db=db)
    check("two guilds, two rows", len(cl.list_locks(1, db=db)) == 1 and len(cl.list_locks(2, db=db)) == 1)
    cl.clear_lock(2, 10, db=db)
    check("clear is guild-scoped", cl.get_lock(1, 10, db=db) is not None and cl.get_lock(2, 10, db=db) is None)

    # re-lock overwrites rather than erroring (INSERT OR REPLACE)
    cl.save_lock(1, 10, 888, "mod#3", "again", prev, db=db)
    check("replace on re-save", cl.get_lock(1, 10, db=db)["locked_by"] == 888)

    # None reason round trips
    cl.save_lock(1, 11, 999, "mod#1", None, prev, db=db)
    check("None reason ok", cl.get_lock(1, 11, db=db)["reason"] is None)

    # list ordering + shape
    locks = cl.list_locks(1, db=db)
    check("list shape", {l["channel_id"] for l in locks} == {10, 11})

    cl.clear_lock(1, 10, db=db)
    cl.clear_lock(1, 11, db=db)
    check("all cleared", cl.list_locks(1, db=db) == [])

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("all channel_locks checks passed")


if __name__ == "__main__":
    main()
