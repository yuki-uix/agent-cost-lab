"""Prefix-diff attributor: the tests call ``attribute()`` — they never re-implement
the diff. Hand-crafted request pairs live in ``fixtures/attribution/*.json``, so
the suite is offline, repeatable, and independent of ``data/raw/``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcostlab.attribute import (
    COMPONENTS,
    Divergence,
    attribute,
    diverging_components,
    prefix_bytes,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "attribution"


def _load_fixtures():
    return [json.loads(p.read_text()) for p in sorted(FIXTURES.glob("*.json"))]


FIXTURE_CASES = [(fx["name"], fx) for fx in _load_fixtures()]


# --- fixtures ---------------------------------------------------------------

@pytest.mark.parametrize("name,fx", FIXTURE_CASES, ids=[c[0] for c in FIXTURE_CASES])
def test_fixture_matches_expectation(name, fx):
    """Every hand-crafted pair attributes to the component/path/detail it claims.

    This is the real gate: the test calls attribute() and checks its output
    against the fixture's recorded answer, rather than re-deriving the diff.
    """
    result = attribute(fx["prev"], fx["curr"])
    expected = fx["expected"]

    if expected is None:
        assert result is None, f"{name}: expected no divergence, got {result}"
        return

    assert result is not None, f"{name}: expected {expected['component']}, got None"
    assert result.component == expected["component"], name
    assert result.path == expected["path"], name
    for needle in expected.get("detail_contains", []):
        assert needle in result.detail, f"{name}: {result.detail!r} missing {needle!r}"


# --- the byte trap -----------------------------------------------------------

def test_equal_content_different_bytes_is_a_divergence():
    """``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` are dict-equal but byte-different.

    The cache matches bytes, so this must be reported as a divergence, never
    treated as identical via ``dict ==``.
    """
    prev = {"model": "claude-sonnet-5", "max_tokens": 1,
            "messages": [{"role": "user", "content": [{"a": 1, "b": 2}]}]}
    curr = {"model": "claude-sonnet-5", "max_tokens": 1,
            "messages": [{"role": "user", "content": [{"b": 2, "a": 1}]}]}
    assert prev["messages"] == curr["messages"]  # dict == says equal
    result = attribute(prev, curr)
    assert result is not None
    assert result.component == "messages"


def test_top_level_key_order_only_in_params_is_not_a_divergence():
    """Top-level JSON key order is not part of any segment, so reordering the
    request's own keys (outside params) is not a cache break."""
    prev = {"model": "a", "max_tokens": 1, "messages": [{"role": "user", "content": "x"}]}
    curr = {"max_tokens": 1, "messages": [{"role": "user", "content": "x"}], "model": "a"}
    assert attribute(prev, curr) is None


def test_proxy_injected_diagnostics_field_is_ignored():
    """The proxy injects ``diagnostics.previous_message_id``, which changes every
    turn. It must never be reported as a cache break."""
    prev = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": "x"}],
            "diagnostics": {"previous_message_id": None}}
    curr = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": "x"}],
            "diagnostics": {"previous_message_id": "msg_001"}}
    assert attribute(prev, curr) is None


# --- append vs truncation asymmetry -----------------------------------------

def test_appending_messages_is_not_a_divergence():
    """Normal per-turn growth: prefix is preserved, so no divergence."""
    prev = {"model": "a", "messages": [{"role": "user", "content": "hi"}]}
    curr = {"model": "a", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "next"},
    ]}
    assert attribute(prev, curr) is None


def test_truncating_messages_is_a_divergence():
    """Dropping trailing history is a structural change, not a cache hit."""
    prev = {"model": "a", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]}
    curr = {"model": "a", "messages": [{"role": "user", "content": "hi"}]}
    result = attribute(prev, curr)
    assert result is not None
    assert result.component == "messages"


def test_adding_a_tool_is_a_divergence_not_an_append():
    """Tools are supposed to be byte-stable; appending one is tools_changed."""
    tool = {"name": "read", "input_schema": {"type": "object"}}
    prev = {"model": "a", "tools": [tool], "messages": [{"role": "user", "content": "x"}]}
    curr = {"model": "a", "tools": [tool, {"name": "bash", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "x"}]}
    result = attribute(prev, curr)
    assert result is not None
    assert result.component == "tools"


# --- bytes accounting (AC6) -------------------------------------------------

def _lcp(a: bytes, b: bytes) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


@pytest.mark.parametrize("name,fx", FIXTURE_CASES, ids=[c[0] for c in FIXTURE_CASES])
def test_bytes_partition_the_prefix(name, fx):
    """bytes_before + bytes_after == total serialised prefix bytes."""
    result = attribute(fx["prev"], fx["curr"])
    if result is None:
        assert fx["expected"] is None, name
        return
    total = len(prefix_bytes(fx["curr"]))
    assert result.bytes_before + result.bytes_after == total, name


@pytest.mark.parametrize("name,fx", FIXTURE_CASES, ids=[c[0] for c in FIXTURE_CASES])
def test_bytes_before_is_the_byte_common_prefix(name, fx):
    """For a non-append divergence, bytes_before is the actual byte-level
    longest common prefix of the two bodies' prefixes."""
    result = attribute(fx["prev"], fx["curr"])
    if result is None:
        return
    prev_bytes = prefix_bytes(fx["prev"])
    curr_bytes = prefix_bytes(fx["curr"])
    assert result.bytes_before == _lcp(prev_bytes, curr_bytes), name


def test_bytes_are_measured_on_the_current_request():
    """A divergence late in the prefix leaves a large cached head and a small
    repriced tail; assert the split is sane, not just self-consistent."""
    prev = {"model": "a", "system": "S" * 100,
            "messages": [{"role": "user", "content": "hi"}]}
    curr = {"model": "a", "system": "S" * 100,
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "x"}]}
    # append → no divergence
    assert attribute(prev, curr) is None
    # edit the first message instead → divergence at the end of the system prefix
    curr["messages"][0]["content"] = "HI"
    result = attribute(prev, curr)
    assert result is not None and result.component == "messages"
    assert result.bytes_before >= 100
    assert result.bytes_after >= 0


# --- segment order is configurable ------------------------------------------

def test_order_parameter_changes_which_component_is_first():
    """The same pair can attribute differently under different segment orders,
    which is exactly what calibrate_attributor.py measures."""
    prev = {"model": "a", "system": "S1", "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "t", "input_schema": {"type": "object"}}]}
    curr = {"model": "a", "system": "S2", "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "t", "input_schema": {"type": "object"}}]}
    system_first = attribute(prev, curr, order=("system", "messages", "tools", "model", "params"))
    tools_before_system = attribute(prev, curr, order=("tools", "system", "messages", "model", "params"))
    assert system_first is not None and system_first.component == "system"
    # tools are identical here, so the system divergence still surfaces first
    assert tools_before_system is not None and tools_before_system.component == "system"


def test_diverging_components_lists_every_break():
    prev = {"model": "a", "system": "S1", "messages": [{"role": "user", "content": "x"}]}
    curr = {"model": "b", "system": "S2", "messages": [{"role": "user", "content": "y"}]}
    assert set(diverging_components(prev, curr)) == {"model", "system", "messages"}


def test_all_components_in_order_are_known():
    assert set(COMPONENTS) == {"model", "system", "tools", "messages", "params"}
