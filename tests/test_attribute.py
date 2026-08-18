"""Prefix-diff attributor: the tests call ``attribute()`` — they never re-implement
the diff. Hand-crafted request pairs live in ``fixtures/attribution/*.json``, so
the suite is offline, repeatable, and independent of ``data/raw/``.
"""
from __future__ import annotations

import importlib.util
import itertools
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

# Every permutation of COMPONENTS: the segment order is a swept hypothesis, and
# the suppression decision must be invariant across all of them (AC1/AC3/AC4).
ALL_ORDERS = list(itertools.permutations(COMPONENTS))


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
    if "suppressed" in expected:
        assert result.suppressed is expected["suppressed"], name
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


# --- normalisation: equivalent transport encodings ---------------------------

def test_content_string_equals_single_text_block():
    """AC1: ``"content": "x"`` and ``"content": [{"type":"text","text":"x"}]``
    tokenise identically, so the cache does not break — not a divergence."""
    prev = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    curr = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}
    assert attribute(prev, curr) is None
    assert attribute(curr, prev) is None  # symmetric


def test_content_string_equals_single_text_block_with_cache_control():
    """The block form may also carry a cache_control breakpoint marker; both
    sides collapse to the same token-equivalent form."""
    prev = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": "hi",
                                       "cache_control": {"type": "ephemeral"}}]}]}
    curr = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}
    assert attribute(prev, curr) is None


def test_cache_control_marker_alone_is_not_a_divergence():
    """A moving ``cache_control`` breakpoint does not change the token stream,
    so its presence/absence alone is not a cache break."""
    prev = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant",
                          "content": [{"type": "text", "text": "done",
                                       "cache_control": {"type": "ephemeral"}}]}]}
    curr = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant",
                          "content": [{"type": "text", "text": "done"}]},
                         {"role": "user", "content": "next"}]}
    assert attribute(prev, curr) is None


def test_normalisation_never_merges_different_text():
    """Text content that actually differs must still be a divergence even when
    one side is a string and the other a block."""
    prev = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "old"}]}]}
    curr = {"model": "a", "max_tokens": 1,
            "messages": [{"role": "user", "content": "new"}]}
    result = attribute(prev, curr)
    assert result is not None
    assert result.component == "messages"


def test_tool_schema_key_order_is_still_a_divergence():
    """AC2: normalisation must not leak into ``tools`` — a key-order change in a
    tool schema is byte-different and still a real cache break."""
    prev = {"model": "a", "max_tokens": 1,
            "tools": [{"name": "read", "input_schema": {"type": "object",
                                                        "properties": {"a": {}, "b": {}}}}],
            "messages": [{"role": "user", "content": "x"}]}
    curr = {"model": "a", "max_tokens": 1,
            "tools": [{"name": "read", "input_schema": {"type": "object",
                                                        "properties": {"b": {}, "a": {}}}}],
            "messages": [{"role": "user", "content": "x"}]}
    result = attribute(prev, curr)
    assert result is not None
    assert result.component == "tools"


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


def test_attributor_and_proxy_share_one_encoder():
    """Not 'they currently agree' — the same object, so they cannot diverge."""
    from agentcostlab import attribute, codec, proxy

    assert proxy.serialise is codec.serialise
    assert attribute.serialise is codec.serialise
    # and the sentinel path stays local to the attributor
    assert attribute._dump(attribute._MISSING) == b""


# --- cache-aware suppression: the six AC2 counterexamples ---------------------
#
# The attributor now reports "did the cache actually break", not "did the text
# change". A divergence after the request's last cache_control block is in the
# non-cached tail and must be suppressed (returned with suppressed=True, not as
# a break) — but *only* that case. These six pin the boundary so the rule cannot
# be widened into silence. The first five must still report; the last must not.
# The suppression decision is structural in CACHE_LAYOUT, so every one of these
# holds under *all* 120 segment orders, not just the default.

_CTRL = {"type": "ephemeral"}


def _assert_break_under_all_orders(prev, curr, component):
    """The one diverging component reports as a cache *break* under every order."""
    for order in ALL_ORDERS:
        result = attribute(prev, curr, order=order)
        assert result is not None, f"{component}: no divergence under {order}"
        assert result.component == component, f"{component}: got {result.component} under {order}"
        assert not result.suppressed, f"{component}: suppressed under {order}"
    return attribute(prev, curr)


def _assert_suppressed_under_all_orders(prev, curr, component):
    """The one diverging component reports *suppressed* under every order."""
    for order in ALL_ORDERS:
        result = attribute(prev, curr, order=order)
        assert result is not None, f"{component}: no divergence under {order}"
        assert result.component == component, f"{component}: got {result.component} under {order}"
        assert result.suppressed, f"{component}: not suppressed under {order}"
    return attribute(prev, curr)


def test_change_before_breakpoint_is_a_break():
    """Editing a message that sits before the last cache_control block is still a miss."""
    prev = {"model": "a", "max_tokens": 1, "messages": [
        {"role": "user", "content": "original question"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer", "cache_control": _CTRL}]},
        {"role": "user", "content": "next"},
    ]}
    curr = {"model": "a", "max_tokens": 1, "messages": [
        {"role": "user", "content": "CHANGED question"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer", "cache_control": _CTRL}]},
        {"role": "user", "content": "next"},
    ]}
    result = _assert_break_under_all_orders(prev, curr, "messages")
    assert result.path[:2] == ["messages", 0]


def test_change_in_breakpoint_block_is_a_break():
    """Editing the very block that carries cache_control is still a miss."""
    prev = {"model": "a", "max_tokens": 1, "messages": [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer A", "cache_control": _CTRL}]},
        {"role": "user", "content": "next"},
    ]}
    curr = {"model": "a", "max_tokens": 1, "messages": [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer B", "cache_control": _CTRL}]},
        {"role": "user", "content": "next"},
    ]}
    result = _assert_break_under_all_orders(prev, curr, "messages")
    assert result.path[:4] == ["messages", 1, "content", 0]


def test_change_with_no_breakpoint_is_a_break():
    """A request with no cache_control marker has no breakpoint to hide behind."""
    prev = {"model": "a", "max_tokens": 1, "messages": [{"role": "user", "content": "a"}]}
    curr = {"model": "a", "max_tokens": 1, "messages": [{"role": "user", "content": "b"}]}
    _assert_break_under_all_orders(prev, curr, "messages")


def test_system_change_is_a_break():
    """A system change is before the messages breakpoint, so it is still a miss."""
    prev = {"model": "a", "max_tokens": 1, "system": "You are version A.", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
    ]}
    curr = {"model": "a", "max_tokens": 1, "system": "You are version B.", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
    ]}
    _assert_break_under_all_orders(prev, curr, "system")


def test_tools_change_is_a_break():
    """A tools change is before the messages breakpoint, so it is still a miss."""
    read = {"name": "read", "input_schema": {"type": "object"}}
    bash = {"name": "bash", "input_schema": {"type": "object"}}
    prev = {"model": "a", "max_tokens": 1, "tools": [read], "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
    ]}
    curr = {"model": "a", "max_tokens": 1, "tools": [read, bash], "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
    ]}
    _assert_break_under_all_orders(prev, curr, "tools")


def test_model_change_is_a_break():
    """A model change invalidates the whole prefix — never suppressed, any order."""
    prev = {"model": "claude-sonnet-5", "max_tokens": 1, "system": "S", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
    ]}
    curr = {"model": "claude-opus-5", "max_tokens": 1, "system": "S", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
    ]}
    _assert_break_under_all_orders(prev, curr, "model")


def test_change_after_breakpoint_is_suppressed():
    """Editing a block after the last cache_control block does not break the
    cache — it is suppressed, but still visible (AC4: the trace survives)."""
    prev = {"model": "a", "max_tokens": 1, "messages": [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
        {"role": "user", "content": "old question"},
    ]}
    curr = {"model": "a", "max_tokens": 1, "messages": [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
        {"role": "user", "content": "new question"},
    ]}
    result = _assert_suppressed_under_all_orders(prev, curr, "messages")
    assert result.path[:3] == ["messages", 2, "content"]
    assert "message 2" in result.detail


# --- order invariance on real data (AC1) -------------------------------------

def _load_calibrate():
    spec = importlib.util.spec_from_file_location(
        "calibrate_attributor",
        Path(__file__).resolve().parents[1] / "scripts" / "calibrate_attributor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_suppression_is_order_invariant_on_real_capture():
    """AC1: for every comparable pair in the real capture, ``suppressed`` is
    identical across all 120 segment orders. Skips (rather than passes) when the
    capture file is absent — the property lives in real data, not a synthetic one."""
    capture = Path(__file__).resolve().parents[1] / "data" / "raw" / "capture.jsonl"
    if not capture.exists():
        pytest.skip(f"real capture not present at {capture}; nothing to assert")
    cal = _load_calibrate()
    rows = cal.load_records(capture)
    pairs = [(p, c) for p, c in cal.pair_by_previous(rows) if cal.official_signal(c) is not None]
    assert pairs, "capture present but holds no comparable pairs"

    for prev, curr in pairs:
        base = None
        for order in ALL_ORDERS:
            result = attribute(prev["request_body"], curr["request_body"], order=order)
            suppressed = result.suppressed if result is not None else False
            if base is None:
                base = suppressed
            else:
                assert suppressed == base, (
                    f"suppression depends on segment order: {order} gave {suppressed}, expected {base}"
                )


# --- multi-component breakpoints (AC4) ---------------------------------------

def test_multi_component_breakpoints_under_every_order():
    """system (2 cache_control blocks) + messages (1 block): the 46-record shape.

    A system change before the system marker is before the last breakpoint
    (messages) and must break the cache; a messages change after the last
    messages marker is in the write tail and must be suppressed — under every
    segment order."""
    fx = json.loads((FIXTURES / "multi_component_breakpoints.json").read_text())
    prev, curr = fx["prev"], fx["curr"]

    # messages change after the last messages marker -> suppressed
    _assert_suppressed_under_all_orders(prev, curr, "messages")

    # system change before the system marker -> break
    sys_curr = json.loads(json.dumps(prev))
    sys_curr["system"][0]["text"] = "You are Claude Code (edited)."
    _assert_break_under_all_orders(prev, sys_curr, "system")
