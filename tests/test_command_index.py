"""Tests for the AI's command index (utils/command_index.py).

Pure — no discord import — so these run in the local venv as well as on the
VPS. Two things are being pinned: that the block handed to the model says what
the commands actually are, and that the citation checker catches an invented
command without tripping over ordinary prose, URLs or dates.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from utils.command_index import (  # noqa: E402
    CommandIndex, build_block, known, load, unknown_citations,
)

SAMPLE = {
    "bot": "Peepo's Reclaimer",
    "commands": [
        {"path": "ask", "signature": "/ask <question> [quick] [character]",
         "description": "Ask the AI — it knows the server 🤖",
         "root": "ask", "permissions": []},
        {"path": "msglog history", "signature": "/msglog history <user_id>",
         "description": "Everything recorded about one account.",
         "root": "msglog", "permissions": ["Administrator"]},
        {"path": "activity user", "signature": "/activity user <user> [days]",
         "description": "A member's messages per day + top channels.",
         "root": "activity", "permissions": []},
    ],
}


def _write(data):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


# ── the block the model sees ─────────────────────────────────────────────────

def test_block_is_one_line_per_command():
    block = build_block(SAMPLE)
    body = block.split("\n")[1:]
    assert len(body) == 3


def test_block_quotes_signatures_verbatim():
    """The model must be able to copy the argument shape straight out."""
    block = build_block(SAMPLE)
    assert "/ask <question> [quick] [character]" in block
    assert "/activity user <user> [days]" in block


def test_block_states_completeness_and_count():
    block = build_block(SAMPLE)
    assert "complete list" in block.lower()
    assert "3 commands" in block


def test_block_marks_permission_gated_commands_only():
    """Everyone-commands carry no tag — that's most of them, so the tag has to
    cost nothing in the common case."""
    lines = build_block(SAMPLE).split("\n")
    admin = [l for l in lines if "msglog history" in l][0]
    plain = [l for l in lines if "/ask" in l][0]
    assert "[Administrator]" in admin
    assert "[" not in plain.split("—")[1]


def test_block_is_far_smaller_than_the_raw_json():
    raw = len(json.dumps(SAMPLE))
    assert len(build_block(SAMPLE)) < raw


# ── the citation check ───────────────────────────────────────────────────────

PATHS, ROOTS = known(SAMPLE)


def test_real_commands_are_not_flagged():
    text = "Run /ask to talk to me, or /activity user @someone for their stats."
    assert unknown_citations(text, PATHS, ROOTS) == set()


def test_invented_command_is_caught():
    assert unknown_citations("Try /levelcheck for that.", PATHS, ROOTS) == {"levelcheck"}


def test_real_group_with_invented_subcommand_is_caught():
    """The likeliest hallucination and the one a member would actually try."""
    assert unknown_citations("Use /msglog purge to clear it.", PATHS, ROOTS) == \
        {"msglog purge"}


def test_trailing_prose_is_not_read_as_a_subcommand():
    """'/ask about the weather' cites /ask — it is not a three-word command."""
    assert unknown_citations("You can /ask about the weather", PATHS, ROOTS) == set()


def test_urls_dates_and_prose_are_not_commands():
    text = ("See dashboard.torvex.app/docs/commands, it changed on 8/9, "
            "and/or check the pins.")
    assert unknown_citations(text, PATHS, ROOTS) == set()


def test_markdown_wrapping_does_not_hide_a_citation():
    assert unknown_citations("Try `/faketool` for that.", PATHS, ROOTS) == {"faketool"}


# ── loading + mtime reload ───────────────────────────────────────────────────

def test_missing_file_reports_unavailable_and_never_guesses():
    """No artifact must mean 'I don't have the list', not improvisation."""
    idx = CommandIndex(os.path.join(tempfile.gettempdir(), "definitely-not-here.json"))
    assert idx.available is False
    assert idx.block() == ""
    assert idx.unknown("go run /whatever") == set()


def test_malformed_file_is_treated_as_missing():
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    try:
        assert load(path) is None
        assert CommandIndex(path).available is False
    finally:
        os.unlink(path)


def test_index_reloads_when_the_file_changes():
    """A regenerated docs JSON must take effect without a bot restart.

    Deliberately rewritten IMMEDIATELY, with no mtime nudge: a regeneration can
    land in the same clock tick as the previous read, and an mtime-in-seconds
    cache key silently skips the reload when it does. This failed on Linux and
    passed on Windows until the key became (mtime_ns, size)."""
    path = _write(SAMPLE)
    try:
        idx = CommandIndex(path)
        assert idx.count == 3
        assert idx.unknown("/newthing") == {"newthing"}

        grown = json.loads(json.dumps(SAMPLE))
        grown["commands"].append(
            {"path": "newthing", "signature": "/newthing", "description": "New.",
             "root": "newthing", "permissions": []})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(grown, fh)

        assert idx.count == 4
        assert idx.unknown("/newthing") == set()
    finally:
        os.unlink(path)


def test_reload_is_detected_even_when_mtime_is_unchanged():
    """The exact regression: identical timestamp, different content."""
    path = _write(SAMPLE)
    try:
        idx = CommandIndex(path)
        assert idx.count == 3
        st = os.stat(path)

        grown = json.loads(json.dumps(SAMPLE))
        grown["commands"].append(
            {"path": "newthing", "signature": "/newthing", "description": "New.",
             "root": "newthing", "permissions": []})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(grown, fh)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))   # pin mtime back

        assert idx.count == 4
    finally:
        os.unlink(path)


def test_real_generated_artifact_if_present():
    """When docs/commands.json is checked in, it must actually parse and be
    the shape the cog expects — this is what catches a generator change."""
    repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    path = os.path.join(repo, "docs", "commands.json")
    if not os.path.exists(path):
        return
    data = load(path)
    assert data is not None, "docs/commands.json exists but does not parse"
    paths, roots = known(data)
    assert len(paths) > 100, f"only {len(paths)} commands parsed"
    block = build_block(data)
    assert block.count("\n") == len(data["commands"])
    assert unknown_citations("/help", paths, roots) == set()


# ── mentioned_in: the pre-gate for on-demand index attachment ────────────────

def test_mentioned_in_real_command_citation():
    idx = CommandIndex(_write(SAMPLE))
    assert idx.mentioned_in("how do I use /ask here")
    assert idx.mentioned_in("run /activity user @me")


def test_mentioned_in_ignores_prose_urls_and_unknown_commands():
    idx = CommandIndex(_write(SAMPLE))
    assert not idx.mentioned_in("what's the weather like")
    assert not idx.mentioned_in("this and/or that, see 8/9 notes")
    assert not idx.mentioned_in("dashboard.torvex.app/docs/commands")
    assert not idx.mentioned_in("try /notarealcommand maybe")


def test_mentioned_in_missing_file_is_false():
    idx = CommandIndex("/nonexistent/commands.json")
    assert not idx.mentioned_in("how do I use /ask here")
