"""The faults have to be as trustworthy as the instrument measuring them.

An injection that silently does nothing produces a capture with no breaks in
it, which is indistinguishable from "the agent never broke its own prefix" —
the exact condition E4 exists to escape. So the load-bearing assertions here
are the negative ones: that a fault which fails to fire says so, that a typo
raises instead of falling back to a clean run, and that the baseline arm is
declared rather than merely unset.
"""
from __future__ import annotations

import json

import pytest

from agentcostlab import inject
from agentcostlab.codec import serialise
from agentcostlab.redact import POLICIES


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    inject.reset()
    monkeypatch.delenv("AGENTCOSTLAB_INJECT", raising=False)
    yield
    inject.reset()


def body(**over):
    base = {
        "model": "claude-opus-5",
        "system": "You are helpful.",
        "tools": [
            {
                "name": "read_file",
                "description": "Read a file.",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- arming ---
def test_nothing_is_armed_by_default():
    assert inject.active() is None
    assert inject.apply(body(), "L") is None


def test_a_typo_raises_instead_of_falling_back_to_a_clean_run(monkeypatch):
    """The failure that would be invisible in the data.

    A silent fallback to "no injection" yields a capture with zero breaks, and
    prediction 4.1 ("every fault produced at least one break") would be answered
    by an instrument defect wearing the costume of a result.
    """
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i9")
    with pytest.raises(inject.UnknownInjection):
        inject.active()


def test_the_baseline_arm_is_declared_not_merely_unset(monkeypatch):
    """i0 must annotate. 4.5 is only scoreable against a capture that says
    which arm it belongs to."""
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i0")
    before = json.loads(json.dumps(b := body()))
    note = inject.apply(b, "L")
    assert note is not None and note["id"] == "i0"
    assert note["applied"] is False
    assert b == before, "the baseline arm must not touch the body"


# ------------------------------------------------------- enumerated cover ---
def test_every_registered_fault_is_exercised_here():
    """Coverage derived from the registry, not from a checklist.

    A new fault added without a case below fails this test rather than shipping
    untested — the same shape as the provider/rate gate in test_gates.py.
    """
    covered = {"i0", "i1", "i2", "i3", "i4"}
    assert set(inject.REGISTRY) == covered, (
        "a fault was added or removed; add its case to this module and update "
        "the covered set in the same commit"
    )


@pytest.mark.parametrize("name", sorted(inject.REGISTRY))
def test_every_fault_annotates_the_record(monkeypatch, name):
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", name)
    note = inject.apply(body(), "L")
    assert note is not None
    assert note["id"] == name
    assert set(note) >= {"id", "applied", "detail", "turn"}


# ------------------------------------------------------------------- I1 ---
@pytest.mark.parametrize(
    "system",
    [None, "You are helpful.", [{"type": "text", "text": "You are helpful."}]],
    ids=["absent", "string", "blocks"],
)
def test_i1_changes_the_prefix_on_every_turn(monkeypatch, system):
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i1")
    a, b = body(system=system), body(system=system)
    inject.apply(a, "L")
    inject.apply(b, "L")
    assert serialise(a["system"]) != serialise(b["system"])


def test_i1_appends_a_block_rather_than_editing_block_zero(monkeypatch):
    """Editing an existing block would move whatever a cache_control marker
    covers, conflating "prefix changed" with "breakpoint moved" — two different
    findings that the attributor reports differently."""
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i1")
    original = {"type": "text", "text": "You are helpful.", "cache_control": {"type": "ephemeral"}}
    b = body(system=[original])
    inject.apply(b, "L")
    assert b["system"][0] == original
    assert len(b["system"]) == 2


# ------------------------------------------------------------------- I2 ---
def test_i2_is_byte_different_but_semantically_equal(monkeypatch):
    """The whole point: `dict ==` calls these equal, the cache does not.

    Retried because the shuffle can land on the original order — that
    intermittency is the real-world shape of the bug, and is why 4.6 reports a
    rate rather than a verdict.
    """
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i2")
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    base = body(tools=[{"name": "t", "description": "d", "input_schema": schema}])
    reference = serialise(base["tools"][0]["input_schema"])

    for _ in range(50):
        b = body(tools=[{"name": "t", "description": "d", "input_schema": dict(schema)}])
        inject.apply(b, "L")
        got = b["tools"][0]["input_schema"]
        assert got == schema, "reordering keys must not change the mapping"
        if serialise(got) != reference:
            return
    pytest.fail("50 shuffles never produced a different byte order")


def test_i2_reports_not_applied_when_there_is_nothing_to_shuffle(monkeypatch):
    """A schema too small to reorder is not a fired fault. Saying otherwise
    would credit a break to a turn that never diverged."""
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i2")
    b = body(tools=[{"name": "t", "input_schema": {"type": "object"}}])
    assert inject.apply(b, "L")["applied"] is False


# ---------------------------------------------------------------- I3 / I4 ---
def test_i3_fires_once_after_the_threshold_not_every_turn(monkeypatch):
    """A single break with a clean before/after is what separates "attributed
    the break" from "attributed a permanently broken prefix"."""
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i3")
    fired = [inject.apply(body(), "L")["applied"] for _ in range(inject.I3_AFTER_TURN + 4)]
    assert fired.count(True) == 1
    assert fired.index(True) == inject.I3_AFTER_TURN


def test_i4_does_not_fire_without_an_explicit_target(monkeypatch):
    """No default target: a wrong guess would bill a different model than
    intended and the capture would still look like a successful I4 run."""
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i4")
    monkeypatch.setattr(inject, "I4_TO_MODEL", "")
    fired = [inject.apply(body(), "L")["applied"] for _ in range(inject.I4_AFTER_TURN + 4)]
    assert not any(fired)


def test_i4_switches_once_and_stays_switched(monkeypatch):
    """The switch must persist, or the model snaps back and breaks the prefix a
    second time — two divergences from one intended fault, which 4.1 cannot
    score. This test caught exactly that: the first implementation rewrote only
    the transition turn.

    `applied` marks the transition alone, because that is the turn carrying the
    divergence; later turns are rewritten but both sides of their comparison
    already name the new model.
    """
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i4")
    monkeypatch.setattr(inject, "I4_TO_MODEL", "claude-haiku-4-5-20251001")
    seen = []
    for _ in range(inject.I4_AFTER_TURN + 4):
        b = body()
        note = inject.apply(b, "L")
        seen.append((note["applied"], b["model"]))

    assert [a for a, _ in seen].count(True) == 1
    assert seen[inject.I4_AFTER_TURN][0] is True
    after = [m for _, m in seen[inject.I4_AFTER_TURN:]]
    assert after == ["claude-haiku-4-5-20251001"] * len(after), (
        "the switch must persist; snapping back is a second break"
    )
    assert seen[0][1] == "claude-opus-5", "turns before the threshold are untouched"


# ---------------------------------------------------------------- lineage ---
def test_two_lineages_do_not_advance_each_others_schedule(monkeypatch):
    """Concurrent sessions share the process. A global counter would fire L2's
    fault on its first turn because L1 had already run past the threshold."""
    monkeypatch.setenv("AGENTCOSTLAB_INJECT", "i3")
    for _ in range(inject.I3_AFTER_TURN + 1):
        inject.apply(body(), "L1")
    assert inject.apply(body(), "L2")["applied"] is False


# ------------------------------------------------------------------- gate ---
def test_the_injection_field_is_classified_for_export():
    """The redaction gate fails closed on unclassified fields. A capture that
    lost its campaign label would read as organic traffic."""
    assert "injection" in POLICIES
