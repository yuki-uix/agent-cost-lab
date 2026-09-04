"""Lineage identity must ignore cache breakpoints, not conversation content.

``proxy._lineage_key`` identifies a conversation by its ``messages[0]``. Claude
Code moves the ``cache_control`` breakpoint down the conversation every turn, so
hashing ``messages[0]`` verbatim splits one conversation into several the moment
the marker walks to a different block. ``codec.strip_cache_control`` drops the
marker everywhere — every depth, every block type — so identity hashes the
conversation, not the write-point. These tests pin that behaviour, and pin the
one place it must *not* apply: ``attribute._normalise_content`` stays text-only.
"""
from __future__ import annotations

import json
from pathlib import Path

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "lineage"
    / "cache_control_moved.json"
)


# --- AC1: real-traffic fixture collapses [0] and [1], keeps [2] ---------------

def test_fixture_collapses_identical_conversation_after_marker_moves():
    """Records [0] and [1] are one conversation; ``messages[0]`` differs only by
    a ``cache_control`` marker Claude Code moved. [2] is a genuinely different
    conversation. Stripping must collapse 3 lineages to 2."""
    from agentcostlab.proxy import _lineage_key

    records = json.loads(FIXTURE.read_text())["records"]
    assert len(records) == 3

    keys = [_lineage_key(r["request_body"]) for r in records]
    assert keys[0] == keys[1], "the moved marker must not split one conversation"
    assert keys[0] != keys[2], "a different first message must stay a different lineage"
    assert len(set(keys)) == 2, f"expected 3 records to collapse to 2 lineages, got {keys}"


# --- AC2: the #14 defence — distinct conversations stay distinct --------------

def test_distinct_first_messages_still_distinct_after_stripping():
    """Stripping must not erase real differences: two different first messages,
    both carrying markers, must still hash to different lineages."""
    from agentcostlab.proxy import _lineage_key

    a = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "task A", "cache_control": {"type": "ephemeral"}}]}]}
    b = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "task B", "cache_control": {"type": "ephemeral"}}]}]}
    assert _lineage_key(a) != _lineage_key(b)


# --- AC3: stripping is recursive and block-type agnostic ----------------------

def test_strip_cache_control_reaches_every_block_type_and_depth():
    """One pass strips the marker at top level, inside a ``content`` array, on
    ``tool_use`` / ``tool_result`` blocks, and in arbitrary nesting — and leaves
    the input untouched (it runs on the request path, before the body is reused)."""
    from agentcostlab.codec import strip_cache_control

    marked = {
        "cache_control": {"type": "ephemeral"},  # top level
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "a",
                 "cache_control": {"type": "ephemeral"}},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {},
                 "cache_control": {"type": "ephemeral"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "r",
                 "cache_control": {"type": "ephemeral"}},
            ]},
        ],
        "nested": {"deep": [{"keep": "v", "cache_control": {"type": "ephemeral"}}]},
    }
    expected = {
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "a"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "r"},
            ]},
        ],
        "nested": {"deep": [{"keep": "v"}]},
    }

    assert strip_cache_control(marked) == expected
    # The input must not be mutated: _lineage_key runs before the body is
    # forwarded and later mutated by the injection steps.
    assert "cache_control" in marked
    assert marked["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


# --- AC4: attribute._normalise_content stays text-only, on purpose -------------

def test_normalise_content_still_only_strips_text_blocks():
    """The asymmetry with ``strip_cache_control`` is deliberate, not a bug to
    unify. For divergence attribution the marker's *position* is evidence — a
    marker moving from a text block to a ``tool_use`` block is a real
    ``cache_control`` change, compared separately by ``_cache_directives``. For
    lineage identity the position is irrelevant. This pins the difference so a
    future cleanup cannot merge the two and move record 61's k=2 conclusion.
    """
    from agentcostlab.attribute import _normalise_content

    content = [
        {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
        {"type": "tool_use", "id": "t", "name": "Read", "input": {},
         "cache_control": {"type": "ephemeral"}},
    ]
    out = _normalise_content(content)
    assert out[0] == {"type": "text", "text": "a"}          # text block stripped
    assert out[1]["cache_control"] == {"type": "ephemeral"}  # tool_use block kept
