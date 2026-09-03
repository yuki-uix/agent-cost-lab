"""Prefix-diff attributor: the tests call ``attribute()`` — they never re-implement
the diff. Hand-crafted request pairs live in ``fixtures/attribution/*.json``, so
the suite is offline, repeatable, and independent of ``data/raw/``.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
from collections import Counter
from pathlib import Path

import pytest

from agentcostlab.ledger import broke_cache
from agentcostlab.attribute import (
    CACHE_LAYOUT,
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
    # Filtered on the ledger being readable, which is what this test needs.
    # `official_signal` used to serve as that filter and no longer exists: #35
    # split it into a ledger verdict and an official component, because folding
    # them together scored "the API cannot tell you" as "nothing changed".
    pairs = [(p, c) for p, c in cal.pair_by_previous(rows)
             if cal.ledger_broke(p, c) is not None]
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


# --- cache_control directives (#34) ------------------------------------------

CC_1H = {"cache_control": {"type": "ephemeral", "ttl": "1h"}}
CC_DEFAULT = {"cache_control": {"type": "ephemeral"}}


def _directive_body(*marks):
    """Three text blocks in one message; `marks` places a marker on some of them."""
    blocks = [{"type": "text", "text": t} for t in ("a", "b", "c")]
    for index, marker in marks:
        blocks[index] = {**blocks[index], **marker}
    return {"model": "m", "system": [{"type": "text", "text": "S"}], "tools": [],
            "messages": [{"role": "user", "content": blocks},
                         {"role": "user", "content": [{"type": "text", "text": "tail"}]}],
            "max_tokens": 10}


def test_a_ttl_change_inside_messages_is_a_break():
    """`_normalise_content` strips `cache_control` from text blocks, so this
    change survived normalisation invisibly. It was caught on real traffic only
    because Claude Code changed `system` in the same request, and nothing
    strips the marker there."""
    _assert_break_under_all_orders(
        _directive_body((0, CC_1H)), _directive_body((0, CC_DEFAULT)), "messages")


def test_moving_the_marker_is_still_not_a_break():
    """What #21 fixed. Claude Code walks the breakpoint down the conversation
    every turn; treating each move as a change produced 25 false reports."""
    for prev, curr in (
        (_directive_body((0, CC_1H)), _directive_body((1, CC_1H))),
        (_directive_body((0, CC_1H)), _directive_body((0, CC_1H), (1, CC_1H))),
    ):
        for order in ALL_ORDERS:
            assert attribute(prev, curr, order=order) is None, order


def test_any_directive_field_counts_not_just_ttl():
    """Compared structurally, so a directive Anthropic adds later is caught
    without another edit here."""
    _assert_break_under_all_orders(
        _directive_body((0, {"cache_control": {"type": "ephemeral", "beta_knob": 1}})),
        _directive_body((0, {"cache_control": {"type": "ephemeral", "beta_knob": 2}})),
        "messages")


def test_the_real_partial_break_is_attributed():
    """capture-02 record 61, 2026-08-19 — the project's first observed cache
    break. Claude Code dropped `ttl: "1h"` from every marker in one request;
    2,822 tokens were paid for twice and the official diagnostics could only
    answer `unavailable`.

    Read from the capture rather than copied into a fixture: a hand-copied pair
    is a fixture that merely resembles the event.
    """
    capture = Path(__file__).resolve().parents[1] / "data" / "raw" / "capture-02.jsonl"
    if not capture.exists():
        pytest.skip(f"capture-02 not present at {capture}; nothing to assert")
    rows = [json.loads(line) for line in capture.read_text().splitlines() if line.strip()]
    by_id = {r["response_id"]: r for r in rows if r.get("response_id")}

    broke = []
    for curr in rows:
        prev = by_id.get(curr.get("injected_previous_message_id"))
        usage, prev_usage = curr.get("usage"), (prev or {}).get("usage")
        if not usage or not prev_usage:
            continue
        if broke_cache(prev, curr):
            broke.append((prev, curr))

    assert len(broke) == 1, f"expected exactly the one known break, found {len(broke)}"
    prev, curr = broke[0]
    for order in ALL_ORDERS:
        d = attribute(prev["request_body"], curr["request_body"], order=order)
        assert d is not None and not d.suppressed, f"the one real break was missed under {order}"


def test_neither_capture_gains_a_false_break():
    """The rule must not buy the break it catches with reports the ledger
    contradicts. 111 pairs across both captures."""
    root = Path(__file__).resolve().parents[1] / "data" / "raw"
    checked = 0
    for name in ("capture.jsonl", "capture-02.jsonl"):
        path = root / name
        if not path.exists():
            continue
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        by_id = {r["response_id"]: r for r in rows if r.get("response_id")}
        for curr in rows:
            prev = by_id.get(curr.get("injected_previous_message_id"))
            usage, prev_usage = curr.get("usage"), (prev or {}).get("usage")
            if not usage or not prev_usage:
                continue
            checked += 1
            ledger_broke = broke_cache(prev, curr)
            d = attribute(prev["request_body"], curr["request_body"])
            assert (d is not None and not d.suppressed) == ledger_broke, \
                f"{name}: attributor and ledger disagree"
    if not checked:
        pytest.skip("no captures present")


def test_a_marker_vanishing_entirely_is_left_where_21_put_it():
    """Deliberately NOT a break, and not because it is known to be safe.

    Both sides must declare a directive before they are compared. A request
    that declares none is a different question — whether it still reads a
    previously written prefix — and neither capture contains a single instance:
    marker counts never change across 111 pairs. #21 decided this case on its
    own evidence, and #34 has none that would overturn it.
    """
    with_marker = _directive_body((0, CC_1H))
    without = _directive_body()
    for order in ALL_ORDERS:
        assert attribute(with_marker, without, order=order) is None, order
        assert attribute(without, with_marker, order=order) is None, order


# --- candidates and order stability (delegation 08) ---------------------------

def _pair_with_divergences(components):
    """A synthetic request pair where exactly the named components diverge."""
    base = {
        "model": "claude-sonnet-5",
        "system": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "read", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant",
             "content": [{"type": "text", "text": "done", "cache_control": {"type": "ephemeral"}}]},
        ],
        "tool_choice": "auto",
        "max_tokens": 100,
    }
    prev = json.loads(json.dumps(base))
    curr = json.loads(json.dumps(base))
    for component in components:
        if component == "model":
            curr["model"] = "claude-opus-5"
        elif component == "system":
            curr["system"][0]["text"] = "S2"
        elif component == "tools":
            curr["tools"].append({"name": "bash", "input_schema": {"type": "object"}})
        elif component == "messages":
            curr["messages"][0]["content"] = "bye"
        elif component == "params":
            curr["tool_choice"] = "any"
    return prev, curr


def test_order_stability_matches_full_enumeration_for_every_k():
    """AC1: the closed form ``order_stability = 1/len(candidates)`` is checked
    against a *real* 120-permutation enumeration, not copied into the assertion.
    For each k in 1..5 a synthetic pair with exactly k diverging components is
    swept; each of the k candidates must be elected exactly 1/k of the time."""
    for k in range(1, 6):
        components = set(COMPONENTS[:k])
        prev, curr = _pair_with_divergences(components)
        winners = Counter()
        for order in ALL_ORDERS:
            d = attribute(prev, curr, order=order)
            assert d is not None, (k, order)
            winners[d.component] += 1
        # every candidate is elected in exactly 1/k of the 120 orders
        for component in components:
            assert winners[component] == len(ALL_ORDERS) // k, (k, component, dict(winners))
        # nothing outside the candidate set is ever elected
        assert set(winners) == components

        base = attribute(prev, curr)
        assert len(base.candidates) == k
        assert base.order_stability == pytest.approx(1.0 / k)
        # the closed form equals the *measured* maximum elected proportion
        measured = max(winners.values()) / len(ALL_ORDERS)
        assert base.order_stability == pytest.approx(measured)


def test_candidates_and_order_stability_are_order_invariant():
    """AC3: ``candidates`` and ``order_stability`` are properties of the two
    bodies, not of the sweep order. Across all 120 orders they must be identical
    — only ``component`` may change, which is the instability being measured."""
    prev, curr = _pair_with_divergences({"system", "tools"})
    base = attribute(prev, curr)
    assert len(base.candidates) == 2
    # candidates are in CACHE_LAYOUT order, not in the swept order
    assert base.candidates == ("tools", "system")

    elected = set()
    for order in ALL_ORDERS:
        d = attribute(prev, curr, order=order)
        assert d is not None
        assert d.candidates == base.candidates
        assert d.order_stability == base.order_stability
        elected.add(d.component)

    assert elected == {"system", "tools"}
