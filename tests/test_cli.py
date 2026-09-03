"""The ``agentcostlab`` CLI: ``record`` reuses capture.sh, ``diagnose`` is money-first.

``diagnose_text`` is the testable heart; the command layer maps its refusals to a
non-zero exit code plus a next step. Tests feed synthetic captures through the
same entrance (``cost_by_cause``) the CLI uses — never re-implementing the money
arithmetic.
"""
from __future__ import annotations

import json
import re
import sys
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

    # The rule is that each percentage carries *its own* denominator, not that
    # the line contains a digit somewhere. An earlier version asserted the
    # latter: stripping "100%" from `ambiguous: 1 turn (100%), 2,822 tokens,
    # $0.005080` still left plenty of digits, so deleting the count passed.
    pct = re.compile(r"(\d+)%(?P<tail>.{0,40})")
    assert "%" in text, "the gate must actually exercise a percentage line"
    found = 0
    for line in text.splitlines():
        for m in pct.finditer(line):
            found += 1
            assert re.search(r"of\s+\d+", m.group("tail")), (
                f"percentage without its denominator: {line!r}")
    assert found, "no percentage was matched, so nothing was checked"


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


def test_one_break_is_a_rate_interval_not_a_point_estimate():
    """P2-b: a hypothetical break has no real usage bucket, so it is a pure rate
    interval — cheapest bucket (uncached) to most expensive (1h write) — never a
    single point. Hand-computed against the real fixture rates for opus."""
    from agentcostlab.pricing import load_rates as load_real_rates

    records = [rec("a", model="claude-opus-5", usage_=usage(cache_read=1000))]
    stats = cli._model_stats(records, load_real_rates())
    opus = next(s for s in stats if s["model"] == "claude-opus-5")

    typical = opus["typical_prefix"]
    # fixtures/pricing.json opus: input_uncached 5.0, cache_read 0.50,
    # cache_write_1h 10.0 per MTok.
    assert opus["one_break_low"] == pytest.approx(typical * (5.0 - 0.50) / 1_000_000)
    assert opus["one_break_high"] == pytest.approx(typical * (10.0 - 0.50) / 1_000_000)
    assert opus["one_break_high"] > opus["one_break_low"]


# --- exit codes: a capture that cannot be priced is a refusal, not an error --

def test_diagnose_refuses_unknown_model_with_next_step(rates, tmp_path):
    p = tmp_path / "cap.jsonl"
    # usage present so the record is measurable and actually reaches rate
    # resolution — the point of this test is the unknown-model refusal, not the
    # no-usage guard.
    p.write_text(json.dumps({
        "response_id": "a", "provider": "anthropic",
        "request_body": {"model": "claude-haiku-4-5"},
        "usage": {"cache_read_input_tokens": 100},
    }) + "\n")
    code, text = cli.run_diagnose(p)
    assert code != 0
    assert "claude-haiku-4-5" in text
    assert "Next step" in text


def test_diagnose_missing_capture_is_a_refusal_with_next_step(tmp_path):
    code, text = cli.run_diagnose(tmp_path / "nope.jsonl")
    assert code != 0
    assert "Next step" in text


def test_diagnose_refuses_repayment_exceeding_capacity(rates, tmp_path):
    """P2-a: a turn that re-bills more tokens than its non-cache-read buckets
    carried breaks the conservation identity; ``run_diagnose`` must turn that
    into exit 2 + a next step, not a traceback."""
    records = [
        rec("a", usage_=prev_usage(1000, 200)),
        rec("b", prev="a", usage_=usage(cache_read=0, input_tokens=100)),
    ]
    p = tmp_path / "cap.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    code, text = cli.run_diagnose(p)
    assert code == 2
    assert "refusing to diagnose" in text
    assert "Next step" in text


def test_diagnose_refuses_ambiguous_cache_write(rates, tmp_path):
    """P2-a: a write with no TTL whose two tiers cost differently cannot be
    priced; ``run_diagnose`` must return exit 2 + a next step, not raise."""
    records = [
        rec("a", usage_=prev_usage(1000, 0)),
        rec("b", prev="a", usage_={"input_tokens": 0, "cache_read_input_tokens": 0,
                                    "cache_creation_input_tokens": 500}),
    ]
    p = tmp_path / "cap.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    code, text = cli.run_diagnose(p)
    assert code == 2
    assert "refusing to diagnose" in text
    assert "Next step" in text


# --- the default capture is the highest-numbered one, never an attempt --------

def test_default_capture_prefers_highest_numbered_and_ignores_attempts(tmp_path, monkeypatch):
    """P1-a: only ``capture-NN.jsonl`` participates, compared numerically — so
    ``capture-10`` beats ``capture-2``, and ``capture-attempt*`` never wins."""
    monkeypatch.setattr(cli, "RAW_DIR", tmp_path)
    (tmp_path / "capture-02.jsonl").write_text("")
    (tmp_path / "capture-10.jsonl").write_text("")
    (tmp_path / "capture-attempt3-prelineagefix.jsonl").write_text("")
    assert cli._default_capture() == tmp_path / "capture-10.jsonl"


def test_default_capture_falls_back_when_no_numbered_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "RAW_DIR", tmp_path)
    assert cli._default_capture() == tmp_path / "capture.jsonl"


# --- record forwards to capture.sh rather than re-implementing it -----------

def test_record_forwards_to_the_capture_script(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0))
    assert cli._cmd_record("status") == 0
    assert calls == [[str(cli.CAPTURE_SH), "status"]]


def test_record_points_capture_sh_at_the_running_interpreter(monkeypatch):
    """P1-b: capture.sh defaults its python to the repo's ``.venv``, but a
    pip-installed CLI must hand it the interpreter it is actually running under."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw["env"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._cmd_record("status") == 0
    assert seen["cmd"] == [str(cli.CAPTURE_SH), "status"]
    assert seen["env"]["ACL_PYTHON"] == sys.executable


def test_record_capture_script_is_the_real_one():
    assert cli.CAPTURE_SH.name == "capture.sh"
    assert cli.CAPTURE_SH.exists()


def test_one_break_dollars_reproduce_from_the_displayed_prefix(rates):
    """The printed 'median prefix N tokens' must price out to the printed dollar
    figure. A median over an even set is a .5 value; if the money is computed from
    the float while the display is floored, the reader multiplies N by the rate and
    cannot rederive the number on screen. Two reads, 100 and 101 -> median 100.5."""
    stats = cli._model_stats(
        [rec("a", usage_=usage(cache_read=100)),
         rec("b", usage_=usage(cache_read=101))],
        SIMPLE,
    )
    (s,) = stats
    n = s["typical_prefix"]
    rate = SIMPLE["anthropic/claude-sonnet-5"]
    # priced from the very integer that will be displayed, not from 100.5
    assert s["one_break_low"] == n * (rate.input_uncached - rate.cache_read) / 1_000_000
    assert s["one_break_high"] == n * (rate.cache_write_1h - rate.cache_read) / 1_000_000


def test_no_measurable_usage_is_a_refusal_not_a_zero_cost_success(rates, tmp_path):
    """P1: a capture with no usable usage measured nothing. Reporting it as a clean
    $0.00 run is a zero wearing a success badge — it must refuse with a next step.
    Covers empty file, missing-usage records, and empty-usage ({}) records."""
    for name, rows in [
        ("empty", []),
        ("missing", [rec("a")]),                          # no usage key
        ("empty_usage", [rec("a", usage_={})]),           # usage present but empty
    ]:
        p = tmp_path / f"{name}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        code, text = cli.run_diagnose(p)
        assert code != 0, f"{name}: measured nothing but returned success"
        assert "Next step" in text, f"{name}: refusal without a next step"


def test_a_read_failure_still_gives_a_next_step(tmp_path):
    """P3: run_diagnose promises 'never just an error'. A corrupt/truncated capture
    must carry a concrete next step like every other refusal."""
    p = tmp_path / "corrupt.jsonl"
    p.write_text("not json at all\n")
    code, text = cli.run_diagnose(p)
    assert code != 0
    assert "Next step" in text


def test_a_healthy_zero_break_capture_is_still_a_success(rates, tmp_path):
    """The guard must not swallow a legitimate zero-cost run: a session that read
    from cache and simply never broke stays exit 0 with its savings."""
    rows = [
        rec("a", usage_=usage(cache_read=0, input_tokens=1000)),
        rec("b", prev="a", usage_=usage(cache_read=900)),   # a real cache read, no break
    ]
    p = tmp_path / "healthy.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    code, text = cli.run_diagnose(p)
    assert code == 0
    assert "$" in text
