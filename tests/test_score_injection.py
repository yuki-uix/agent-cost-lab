"""The scoring script's contract, tested through its own entry points.

``score_injection.py`` is E4's only way to turn an injected capture into a
three-way verdict, and it had never run on real data — its first run crashed on
``attribute`` returning a single ``Divergence`` where the script assumed a
collection. These tests pin the fixed contract (single divergence, suppression,
rejection gate, printed disagreements) without touching ``src/agentcostlab/``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "score_injection", SCRIPTS / "score_injection.py")
si = importlib.util.module_from_spec(_spec)
# The script's `@dataclass` resolves its (string) annotations through
# `sys.modules[cls.__module__]`, so the module must be registered before it runs.
sys.modules[_spec.name] = si
_spec.loader.exec_module(si)


_CTRL = {"type": "ephemeral"}


# --- body pairs whose attributor answer is pinned by test_attribute.py --------

def _break_pair():
    """A real, unsuppressed break: the only message's text changed, no marker."""
    prev = {"model": "m", "messages": [{"role": "user", "content": "a"}]}
    curr = {"model": "m", "messages": [{"role": "user", "content": "b"}]}
    return prev, curr


def _append_pair():
    """Normal per-turn growth: no divergence at all."""
    prev = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    curr = {"model": "m", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "x"},
    ]}
    return prev, curr


def _suppressed_pair():
    """A text change after the last cache_control block: cache intact."""
    prev = {"model": "m", "max_tokens": 1, "messages": [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
        {"role": "user", "content": "old question"},
    ]}
    curr = {"model": "m", "max_tokens": 1, "messages": [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": _CTRL}]},
        {"role": "user", "content": "new question"},
    ]}
    return prev, curr


# --- records -----------------------------------------------------------------

def _usage(cache_read=0, miss=10):
    """DeepSeek-shaped usage, so the ledger can measure (not merely guess)."""
    return {"prompt_cache_hit_tokens": cache_read,
            "prompt_cache_miss_tokens": miss}


def _rec(rid, *, body, usage=None, injection, prev=None,
         diagnostics_present=False, diagnostics=None):
    record = {"response_id": rid, "provider": "deepseek",
              "request_body": body, "injection": injection}
    if usage is not None:
        record["usage"] = usage
    if prev is not None:
        record["injected_previous_message_id"] = prev
    if diagnostics_present:
        record["diagnostics_present"] = True
        record["diagnostics"] = diagnostics
    return record


def _write(tmp_path, name, records):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


def _run(path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["score_injection.py", str(path)])
    code = si.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- AC3: a single Divergence is blamed or not, never iterated ----------------

def test_attributed_component_names_a_real_break():
    prev, curr = _break_pair()
    assert si.attributed_component(prev, curr) == "messages"


def test_attributed_component_is_none_when_nothing_diverged():
    prev, curr = _append_pair()
    assert si.attributed_component(prev, curr) is None


def test_attributed_component_excludes_a_suppressed_divergence():
    """A divergence after the cache_control marker is visible but not a break,
    so it must not be blamed."""
    prev, curr = _suppressed_pair()
    assert si.attributed_component(prev, curr) is None


def test_score_attributes_a_single_divergence_without_throwing():
    """The original crash: a single ``Divergence`` was treated as a collection.
    A full record pair now attributes cleanly instead of raising."""
    pbody, cbody = _break_pair()
    prev = _rec("r0", body=pbody, usage=_usage(0),
                injection={"id": "i1", "applied": False, "detail": None, "turn": 1})
    curr = _rec("r1", body=cbody, usage=_usage(0),
                injection={"id": "i1", "applied": False, "detail": None, "turn": 2},
                prev="r0")
    result = si.score([prev, curr])
    assert result.fault == "i1"
    assert result.tally["A: not fired, blamed messages"] == 1


# --- AC2: the rejection gate stays shut ---------------------------------------

def test_a_capture_without_injection_is_rejected_not_skipped(tmp_path, monkeypatch, capsys):
    """Mutation guard: weakening this gate to a silent skip must fail here.
    The message is asserted verbatim so it cannot drift either."""
    p = _write(tmp_path, "legacy.jsonl", [
        {"response_id": "a", "provider": "deepseek",
         "request_body": {"model": "m", "messages": []},
         "usage": _usage(0)},
    ])
    code, out, err = _run(p, monkeypatch, capsys)
    assert code == 2
    assert err == (f"{p}: no `injection` field on any record. This capture does not "
                   "declare a campaign arm and cannot be scored as one. Re-capture with "
                   "AGENTCOSTLAB_INJECT set (use i0 for the baseline).\n")
    assert out == ""


# --- AC4: three stats separate, disagreement printed not merged ---------------

def test_a_pairwise_disagreement_is_printed_not_reconciled(tmp_path, monkeypatch, capsys):
    """C says ``system`` while the attributor blames ``messages``; the three
    ground truths stay separate and the disagreement is printed, not merged."""
    pbody, cbody = _break_pair()
    prev = _rec("r0", body=pbody, usage=_usage(0),
                injection={"id": "i3", "applied": False, "detail": None, "turn": 1})
    curr = _rec("r1", body=cbody, usage=_usage(0),
                injection={"id": "i3", "applied": False, "detail": None, "turn": 2},
                prev="r0",
                diagnostics_present=True,
                diagnostics={"cache_miss_reason": {"type": "system_changed"}})
    p = _write(tmp_path, "i3.jsonl", [prev, curr])
    code, out, err = _run(p, monkeypatch, capsys)
    assert code == 0
    assert "A: not fired, blamed messages" in out
    assert "B: intact" in out
    assert "C: disagrees" in out
    assert "disagreements (1)" in out
    assert "official system, attributed messages, ledger broke=False" in out
    assert "no pairwise disagreement" not in out


# --- AC1: "never fired" is a state, not an error ------------------------------

def test_an_armed_fault_that_never_fired_is_reported(tmp_path, monkeypatch, capsys):
    pbody, cbody = _append_pair()
    prev = _rec("r0", body=pbody, usage=_usage(0),
                injection={"id": "i3", "applied": False, "detail": None, "turn": 1})
    curr = _rec("r1", body=cbody, usage=_usage(0),
                injection={"id": "i3", "applied": False, "detail": None, "turn": 2},
                prev="r0")
    p = _write(tmp_path, "i3.jsonl", [prev, curr])
    code, out, err = _run(p, monkeypatch, capsys)
    assert code == 0
    assert "fault never fired" in out
    assert "A: fired" not in out


def test_baseline_does_not_report_never_fired(tmp_path, monkeypatch, capsys):
    """i0 has no fault to fire; it reports its comparison, not the armed-but-
    silent notice that only non-baseline arms get."""
    pbody, cbody = _append_pair()
    prev = _rec("r0", body=pbody, usage=_usage(0),
                injection={"id": "i0", "applied": False, "detail": None, "turn": 1})
    curr = _rec("r1", body=cbody, usage=_usage(0),
                injection={"id": "i0", "applied": False, "detail": None, "turn": 2},
                prev="r0")
    p = _write(tmp_path, "i0.jsonl", [prev, curr])
    code, out, err = _run(p, monkeypatch, capsys)
    assert code == 0
    assert "fault never fired" not in out
