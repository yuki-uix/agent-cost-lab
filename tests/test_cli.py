"""The ``agentcostlab`` CLI: ``record`` reuses capture.sh, ``diagnose`` is money-first.

``diagnose_text`` is the testable heart; the command layer maps its refusals to a
non-zero exit code plus a next step. Tests feed synthetic captures through the
same entrance (``cost_by_cause``) the CLI uses — never re-implementing the money
arithmetic.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from agentcostlab import analysis, cli
from agentcostlab.analysis import cost_by_cause
from agentcostlab.pricing import Rate

SIMPLE = {
    "anthropic/claude-sonnet-5": Rate(input_uncached=1.0, cache_read=0.1,
                                       cache_write_5m=1.25, cache_write_1h=2.0,
                                       output=5.0, source="https://example.test",
                                       retrieved_at="2026-08-19", verified=True),
    "anthropic/claude-opus-5": Rate(input_uncached=3.0, cache_read=0.3,
                                     cache_write_5m=3.75, cache_write_1h=6.0,
                                     output=15.0, source="https://example.test",
                                     retrieved_at="2026-08-19", verified=True),
}


@pytest.fixture
def rates(monkeypatch):
    monkeypatch.setattr(analysis, "load_rates", lambda: SIMPLE)
    monkeypatch.setattr(cli, "load_rates", lambda: SIMPLE)


def body(system_text, model="claude-sonnet-5"):
    return {"model": model, "system": system_text,
            "messages": [{"role": "user", "content": "hi"}]}


def usage(cache_read=0, input_tokens=0, five=0, hour=0):
    """Anthropic-shaped usage with a complete TTL breakdown."""
    return {"input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": five + hour,
            "cache_creation": {"ephemeral_5m_input_tokens": five,
                               "ephemeral_1h_input_tokens": hour},
            "output_tokens": 0}


def prev_usage(cache_read, creation):
    return {"cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": creation}


def rec(resp, prev=None, *, model="claude-sonnet-5", system="s", body_=None, usage_=None):
    record = {"response_id": resp, "provider": "anthropic",
              "request_body": body_ if body_ is not None else body(system, model=model)}
    if prev is not None:
        record["injected_previous_message_id"] = prev
    if usage_ is not None:
        record["usage"] = usage_
    return record


# --- zero breaks still has money --------------------------------------------

def test_diagnose_zero_break_is_non_empty_and_has_money(rates):
    """A healthy capture with no miss still prints the money: read tokens, the
    saving, and a one-break estimate — never an empty table."""
    records = [rec("a", usage_=usage(cache_read=1000))]
    text = cli.diagnose_text(records)

    assert text.strip()
    assert "$" in text
    assert "0 breaks" in text


# --- percentages carry their count on the same line -------------------------

def test_every_percentage_line_carries_an_integer_count(rates):
    """Constraint 3: a bare ``100%`` must never print. Every ``%`` line has to
    carry an integer count; strip the percentage itself and a count must remain."""
    records = [
        rec("a", system="A", usage_=prev_usage(1000, 200)),
        rec("b", prev="a", system="B", usage_=usage(cache_read=800, input_tokens=400)),
    ]
    text = cli.diagnose_text(records)

    pct = re.compile(r"\d+%")
    digit = re.compile(r"\d+")
    assert "%" in text, "the gate must actually exercise a percentage line"
    for line in text.splitlines():
        if "%" in line:
            assert digit.search(pct.sub("", line)), f"bare percentage: {line!r}"


# --- the three tiers are visually distinct; ambiguous is a candidate list ----

def test_three_tiers_are_distinguishable_and_ambiguous_lists_candidates(rates):
    """Constraint 4: ``ambiguous`` names its candidates and calls them candidates,
    and those names never leak into the ``by_cause`` block."""
    records = [
        rec("a", body_={"model": "claude-sonnet-5", "system": "A",
                        "messages": [{"role": "user", "content": "hi"}]},
            usage_=prev_usage(1000, 200)),
        rec("b", prev="a",
            body_={"model": "claude-sonnet-5", "system": "B",
                   "messages": [{"role": "user", "content": "bye"}]},
            usage_=usage(cache_read=800, input_tokens=400)),
    ]
    text = cli.diagnose_text(records)

    lines = text.splitlines()
    by_cause_lines = [l for l in lines if l.startswith("  by_cause:")]
    ambiguous_lines = [l for l in lines if l.startswith("  ambiguous:")]

    assert by_cause_lines == ["  by_cause:  none"]
    assert "system" not in "\n".join(by_cause_lines)
    assert "messages" not in "\n".join(by_cause_lines)
    assert any("candidates, not causes: system, messages" in l for l in ambiguous_lines)


# --- per-model savings are a display of cost_by_cause's total ---------------

def test_per_model_savings_sum_to_cost_by_cause_total(rates):
    records = [
        rec("a", usage_=usage(cache_read=1000)),
        rec("b", model="claude-opus-5", usage_=usage(cache_read=2000)),
    ]
    breakdown = cost_by_cause(records)
    stats = cli._model_stats(records, SIMPLE)
    assert sum(s["saved"] for s in stats) == pytest.approx(breakdown.hit_usd_saved)


# --- exit codes: a capture that cannot be priced is a refusal, not an error --

def test_diagnose_refuses_unknown_model_with_next_step(rates, tmp_path):
    p = tmp_path / "cap.jsonl"
    p.write_text(json.dumps({
        "response_id": "a", "provider": "anthropic",
        "request_body": {"model": "claude-haiku-4-5"},
    }) + "\n")
    code, text = cli.run_diagnose(p)
    assert code != 0
    assert "claude-haiku-4-5" in text
    assert "Next step" in text


def test_diagnose_missing_capture_is_a_refusal_with_next_step(tmp_path):
    code, text = cli.run_diagnose(tmp_path / "nope.jsonl")
    assert code != 0
    assert "Next step" in text


# --- record forwards to capture.sh rather than re-implementing it -----------

def test_record_forwards_to_the_capture_script(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0))
    assert cli._cmd_record("status") == 0
    assert calls == [[str(cli.CAPTURE_SH), "status"]]


def test_record_capture_script_is_the_real_one():
    assert cli.CAPTURE_SH.name == "capture.sh"
    assert cli.CAPTURE_SH.exists()
