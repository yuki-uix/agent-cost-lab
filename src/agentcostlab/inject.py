"""Deliberate cache-prefix faults, for generating misses with a known cause.

E1 is blocked on data, not analysis: across four captures the main one carries
**zero** ``cache_miss_reason`` verdicts, because a healthy agent simply does not
break its own prefix. ``calibrate_attributor.py`` says so in its own docstring —
every comparable turn in that capture is "no divergence". So the attributor has
only ever been checked against negatives.

Natural capture cannot fix that. The faults have to be introduced on purpose.
This module is where they live. See ``docs/e4-injection-campaign.md``.

**This module deliberately rewrites the subject under measurement**, which is
exactly what ``codec.serialise`` exists to prevent everywhere else. Three rules
keep that from becoming a silent instrument defect:

1. **Off unless asked.** :func:`active` returns ``None`` unless
   ``AGENTCOSTLAB_INJECT`` names a registered fault. There is no default-on path
   and no config file that could carry one in from a previous run.
2. **Every affected record says so.** The proxy writes what :func:`describe`
   returns into the record. A capture taken under injection can never be mistaken
   for an organic one, including by a reader who was not there.
3. **Faults mutate the body the proxy is about to serialise**, so the recorded
   ``request_body`` is the body that was actually sent. The attributor diffs
   recorded bodies; if injection happened after recording, it would diff a stream
   that was never transmitted — the failure ``codec`` was written to rule out.

Not every fault in the campaign belongs here. I5 (history compaction) is driven
by the client (``/compact``), not by the wire, and simulating it by truncating
messages in flight would leave the client's next turn referring to context the
upstream no longer has. That one is *observed*, not injected: run it in the
client, the proxy records it like any other turn.
"""
from __future__ import annotations

import os
import random
from collections.abc import Callable
from datetime import datetime, timezone

# Turn counters, keyed by lineage. Faults that fire "after turn N" need to know
# how deep the conversation is; the proxy already computes a lineage key, so the
# count is kept per lineage rather than globally — two concurrent sessions must
# not advance each other's schedule.
_TURNS: dict[str, int] = {}

# Injections that fire once, after a threshold, need to know whether they have
# already fired for this lineage. Firing every turn afterwards would turn a
# single-break fault into a permanent one and destroy the distinction 4.1 tests.
_FIRED: set[tuple[str, str]] = set()


def _bump(lineage: str) -> int:
    _TURNS[lineage] = _TURNS.get(lineage, 0) + 1
    return _TURNS[lineage]


# --------------------------------------------------------------------------
# I1 — system prompt carries a value that changes every request
# --------------------------------------------------------------------------
def i1_system_timestamp(body: dict, lineage: str) -> str | None:
    """Append an ISO timestamp to the system prompt. Breaks every turn.

    The canonical own-goal: a timestamp early in the prompt reprices everything
    after it, on every single request, with no error anywhere. Fixture:
    ``fixtures/attribution/system_timestamp.json``.
    """
    _bump(lineage)
    stamp = datetime.now(timezone.utc).isoformat()
    system = body.get("system")
    if system is None:
        body["system"] = f"Current time: {stamp}"
    elif isinstance(system, str):
        body["system"] = f"{system}\nCurrent time: {stamp}"
    elif isinstance(system, list):
        # Anthropic block form. Append a block rather than editing an existing
        # one: editing block 0 would also move any cache_control marker's
        # content, conflating "prefix changed" with "breakpoint moved".
        body["system"] = [*system, {"type": "text", "text": f"Current time: {stamp}"}]
    else:
        return None
    return f"appended timestamp {stamp}"


# --------------------------------------------------------------------------
# I2 — tool schema key order is unstable
# --------------------------------------------------------------------------
def i2_tool_schema_key_order(body: dict, lineage: str) -> str | None:
    """Shuffle the key order inside each tool's ``input_schema``.

    Semantically identical, byte-different. The attributor's own rule is that
    *within a node the cache is still byte-sensitive*, so this must be reported
    as a divergence even though ``dict ==`` would call the two equal. Fixture:
    ``fixtures/attribution/tools_schema_key_order.json``.

    Intermittent by construction — a shuffle sometimes lands on the original
    order, which is the real-world shape of this bug (``Object.keys`` ordering
    varying between processes). It is the only fault here carrying randomness,
    so prediction 4.6 requires n>=5 and reports a rate, not a verdict.
    """
    _bump(lineage)
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    touched = 0
    for tool in tools:
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or len(schema) < 2:
            continue
        items = list(schema.items())
        random.shuffle(items)
        tool["input_schema"] = dict(items)
        touched += 1
    return f"reordered input_schema keys on {touched} tool(s)" if touched else None


# --------------------------------------------------------------------------
# I3 — a tool description changes mid-session
# --------------------------------------------------------------------------
I3_AFTER_TURN = 4


def i3_tool_description_edit(body: dict, lineage: str) -> str | None:
    """Edit one tool's ``description``, once, after turn :data:`I3_AFTER_TURN`.

    A single break with a clean before/after, which is what separates
    "attributed the break" from "attributed a permanently broken prefix".
    """
    turn = _bump(lineage)
    if turn <= I3_AFTER_TURN or ("i3", lineage) in _FIRED:
        return None
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    target = tools[0]
    if "description" not in target:
        return None
    target["description"] = f"{target['description']} (edited by E4 injection)"
    _FIRED.add(("i3", lineage))
    return f"edited description of tool {target.get('name')!r} at turn {turn}"


# --------------------------------------------------------------------------
# I4 — the model changes mid-session
# --------------------------------------------------------------------------
I4_AFTER_TURN = 4
I4_TO_MODEL = os.environ.get("AGENTCOSTLAB_INJECT_I4_MODEL", "")


def i4_model_switch(body: dict, lineage: str) -> str | None:
    """Switch ``model`` after turn :data:`I4_AFTER_TURN`, and *stay* switched.

    Requires ``AGENTCOSTLAB_INJECT_I4_MODEL``; there is no default target,
    because a wrong guess would silently bill a different model than intended
    and the capture would look like a successful I4 run.

    The rewrite persists for the rest of the lineage, because a real mid-session
    model switch does. An earlier version rewrote only the transition turn and
    let the model snap back on the next one — which produces **two** breaks
    (over and back), not the single clean transition prediction 4.1 is written
    against. Caught by ``test_i4_switches_once_when_given_a_target``.

    ``applied`` is therefore reported on the transition turn alone: that is the
    turn carrying the divergence. Later turns are rewritten but are not faults —
    both sides of their prefix comparison already name the new model.

    This one changes what the client gets back: a different model answers from
    the switch onward. Acceptable here (the subject is cache behaviour, not
    output quality) but it is why I4 must never run against work whose output
    matters.
    """
    turn = _bump(lineage)
    if turn <= I4_AFTER_TURN or not I4_TO_MODEL:
        return None
    before = body.get("model")
    if before == I4_TO_MODEL:
        return None
    body["model"] = I4_TO_MODEL
    if ("i4", lineage) in _FIRED:
        return None
    _FIRED.add(("i4", lineage))
    return f"switched model {before!r} -> {I4_TO_MODEL!r} at turn {turn}"


# --------------------------------------------------------------------------
# I0 — baseline
# --------------------------------------------------------------------------
def i0_none(body: dict, lineage: str) -> str | None:
    """Touch nothing. Exists so a baseline run is *declared*, not merely unset.

    Prediction 4.5 (no false positives) is only meaningful against a capture
    that says which arm it belongs to. An unlabelled capture cannot distinguish
    "baseline" from "someone forgot to set the variable".
    """
    _bump(lineage)
    return None


Injection = Callable[[dict, str], "str | None"]

REGISTRY: dict[str, Injection] = {
    "i0": i0_none,
    "i1": i1_system_timestamp,
    "i2": i2_tool_schema_key_order,
    "i3": i3_tool_description_edit,
    "i4": i4_model_switch,
}


class UnknownInjection(ValueError):
    """Raised when the env var names a fault that does not exist.

    Loud on purpose. A typo that fell back to "no injection" would produce a
    capture that looks like a clean baseline and silently answer 4.1 with "no
    break was ever produced" — an instrument failure wearing the costume of a
    result.
    """


def active() -> str | None:
    """Which fault is armed, from ``AGENTCOSTLAB_INJECT``. ``None`` when unset."""
    name = os.environ.get("AGENTCOSTLAB_INJECT", "").strip().lower()
    if not name:
        return None
    if name not in REGISTRY:
        raise UnknownInjection(
            f"AGENTCOSTLAB_INJECT={name!r} is not a registered fault. "
            f"Known: {', '.join(sorted(REGISTRY))}"
        )
    return name


def apply(body: dict, lineage: str) -> dict | None:
    """Apply the armed fault to ``body`` in place.

    Returns the record annotation, or ``None`` when nothing is armed. The
    annotation is written into every affected record by the proxy so a capture
    can never be read as organic.
    """
    name = active()
    if name is None:
        return None
    detail = REGISTRY[name](body, lineage)
    return {
        "id": name,
        "applied": detail is not None,
        "detail": detail,
        "turn": _TURNS.get(lineage, 0),
    }


def describe() -> dict | None:
    """Campaign metadata for the capture header. ``None`` when nothing is armed."""
    name = active()
    if name is None:
        return None
    return {
        "id": name,
        "doc": REGISTRY[name].__doc__.strip().splitlines()[0] if REGISTRY[name].__doc__ else "",
        "i3_after_turn": I3_AFTER_TURN,
        "i4_after_turn": I4_AFTER_TURN,
        "i4_to_model": I4_TO_MODEL or None,
    }


def reset() -> None:
    """Clear per-lineage state. For tests; a live proxy has one campaign per run."""
    _TURNS.clear()
    _FIRED.clear()
