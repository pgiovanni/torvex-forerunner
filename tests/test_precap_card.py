"""Precapture card rendering — capture-time fallback + refresh bookkeeping.

The 2026-08-09 BapVMH alert printed a bare IPv6 for its Connection because the
card is posted within a second of the link-open while the gate's intel drain
scores the row on a timer — measured 109 seconds on that row, by which point
the gate knew `AS7552 Viettel Group, residential`. Two fixes are pinned here:
the card now falls back to capture-time facts, and the posted message is
tracked so it can be corrected in place once scoring lands.

No discord import in the rendering half — `_precap_conn` is a module-level pure
function, so it is imported directly out of the cog source. The bookkeeping half
uses quarantine_store against a temp DB.
"""
import os
import sys
import tempfile
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)


def _load_precap_conn():
    """Pull _precap_conn + _CONN_CLASS out of cogs/altguard.py without importing
    discord: exec only the module-level prelude up to the first class."""
    src = open(os.path.join(ROOT, "cogs", "altguard.py"), encoding="utf-8").read()
    cut = src.index("\nclass ")
    prelude = src[:cut]
    # strip the imports the prelude does not need for these two symbols
    lines = [l for l in prelude.split("\n")
             if not l.startswith(("import discord", "from discord", "import aiohttp",
                                  "import quarantine_store"))]
    mod = types.ModuleType("altguard_prelude")
    mod.__dict__["__file__"] = "altguard.py"
    exec(compile("\n".join(lines), "altguard_prelude", "exec"), mod.__dict__)
    return mod


M = _load_precap_conn()

BAPVMH = {
    "ip": "2402:800:61b3:cb58:a7:1131:49d3:de08",
    "cap_country": "VN", "cap_asn": 7552, "cap_org": "Viettel Group",
    "cap_conn_class": None,
}
SCORED = dict(BAPVMH, scored_country="VN", scored_city="Van Dinh",
              scored_region="Hanoi", scored_isp="Viettel Group",
              scored_asn=7552, scored_org="Viettel Group",
              scored_conn_class="residential", scored_geo_trust=1,
              scored_fraud=0, scored_host="")


def test_unscored_card_names_the_network_instead_of_only_an_ip():
    out = M._precap_conn(BAPVMH)
    assert "VN" in out and "Viettel Group" in out and "AS7552" in out


def test_the_raw_ip_is_always_kept():
    """Explicit product decision — the address stays on the card either way."""
    assert BAPVMH["ip"] in M._precap_conn(BAPVMH)
    assert BAPVMH["ip"] in M._precap_conn(SCORED)


def test_scored_values_win_over_capture_time_ones():
    out = M._precap_conn(SCORED)
    assert "Van Dinh" in out and "residential" in out.lower()


def test_capture_time_class_is_omitted_when_unknown():
    """cap_conn_class is NULL unless a curated list identified it — the card
    must not print 'none', which reads like a classification."""
    out = M._precap_conn(BAPVMH)
    assert "no rDNS" not in out and "unclassified" not in out


def test_new_classes_have_readable_labels():
    for cls in ("business", "satellite", "hosting", "residential", "mobile"):
        assert cls in M._CONN_CLASS, f"{cls} would render as a raw string"


# --- refresh bookkeeping ----------------------------------------------------

def _cleanup(path):
    # sqlite3 connections in quarantine_store are commit-scoped, not closed, so
    # Windows still holds the handle when the test ends. The temp file is the
    # OS's problem at that point; failing teardown must not fail the test.
    try:
        os.unlink(path)
    except OSError:
        pass


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["ALTGUARD_QUARANTINE_DB"] = path
    for m in [m for m in list(sys.modules) if m == "quarantine_store"]:
        del sys.modules[m]
    import quarantine_store as q
    q._PATH = path
    q.init()
    return q, path


def test_card_is_tracked_then_marked_refreshed():
    q, path = _store()
    try:
        q.remember_precap_card(41, 111, 222)
        pending = q.precap_cards_to_refresh()
        assert [c["precap_id"] for c in pending] == [41]
        assert pending[0]["message_id"] == "222"
        q.mark_precap_refreshed(41)
        assert q.precap_cards_to_refresh() == []
    finally:
        _cleanup(path)


def test_reposting_the_same_row_tracks_the_newest_card():
    q, path = _store()
    try:
        q.remember_precap_card(7, 111, 222)
        q.remember_precap_card(7, 111, 333)      # must not raise
        pending = q.precap_cards_to_refresh()
        assert len(pending) == 1 and pending[0]["message_id"] == "333"
    finally:
        _cleanup(path)


def test_stale_cards_are_not_retried_forever():
    """A row the drain declines to score must age out of the retry set."""
    q, path = _store()
    try:
        q.remember_precap_card(9, 111, 222)
        with q._conn() as c:
            c.execute("UPDATE precap_cards SET ts=? WHERE precap_id=9", (1.0,))
        assert q.precap_cards_to_refresh() == []
    finally:
        _cleanup(path)


# --- Private Relay is named, not reported as a gap --------------------------
# It egresses via Fastly/Cloudflare and publishes no PTR, so it lands on
# conn_class 'none' and used to render as "no rDNS published" — which reads
# like missing data when it is a known, deliberately handled case. Both of the
# only two unclassifiable precapture rows in the corpus are Private Relay.

RELAY = {
    "ip": "146.75.128.212", "scored_asn": 54113,
    "scored_org": "iCloud Private Relay", "scored_isp": "iCloud Private Relay",
    "scored_conn_class": "none", "scored_relay": 1, "scored_host": "146.75.128.212",
}


def test_private_relay_is_labelled_not_called_missing_rdns():
    out = M._precap_conn(RELAY)
    assert "iCloud Private Relay" in out
    assert "no rDNS published" not in out


def test_a_real_unclassified_row_still_says_so():
    """The label must not swallow a genuine gap — only relay rows get renamed."""
    out = M._precap_conn(dict(RELAY, scored_relay=0, scored_org="Some ISP",
                              scored_isp="Some ISP"))
    assert "no rDNS published" in out
    assert "iCloud Private Relay" not in out
