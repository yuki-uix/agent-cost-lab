"""E3's number is a dollar figure derived from a counterfactual, so both halves
need holding: the arithmetic, and the claim that the counterfactual is a model
rather than an observation.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "compaction_payback", ROOT / "scripts" / "compaction_payback.py")
e3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e3)


def _capture():
    path = ROOT / "data" / "raw" / "capture-03.jsonl"
    if not path.exists():
        pytest.skip(f"capture-03 not present at {path}; nothing to measure")
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_the_compaction_is_found_by_its_marker_not_by_threading():
    """The proxy cannot thread across a /compact — #42's accepted blind spot —
    so the two lineages have to be paired by the marker and by time."""
    found = e3.find_compaction(_capture())
    assert found, "the /compact in capture-03 was not found"
    pre, post = found
    assert len(pre) == 6 and len(post) == 46


def test_payback_lands_where_the_report_says():
    """18-19 turns, against the 2-4 predicted. If this moves, report/05 and
    predictions 3.1 are both stale."""
    from agentcostlab.pricing import load_rates

    pre, post = e3.find_compaction(_capture())
    rates = load_rates()
    turns = {e3.payback(pre, post, "anthropic/claude-opus-5", w, rates)[0]
             for w in (0, 571, 900)}
    assert turns <= {18, 19}, turns


def test_the_answer_does_not_hinge_on_the_one_free_parameter():
    """The counterfactual has exactly one thing that had to be chosen rather
    than measured — what an ordinary turn would have written on the compaction
    turn. Sweeping it across the observed range must not move the conclusion."""
    from agentcostlab.pricing import load_rates

    pre, post = e3.find_compaction(_capture())
    rates = load_rates()
    turns = [e3.payback(pre, post, "anthropic/claude-opus-5", w, rates)[0]
             for w in (0, 571, 900)]
    assert max(turns) - min(turns) <= 1, f"the sweep moves the answer: {turns}"


def test_the_saving_is_the_cut_priced_as_a_cache_read():
    """The shape of the result: compaction saves on the cheapest line item.
    Cached reads are 0.1x, so removing tokens buys back very little per turn,
    while the rewrite is billed at the 1h write tier."""
    from agentcostlab.pricing import load_rates

    pre, post = e3.find_compaction(_capture())
    rates = load_rates()
    _, _, one_off, per_turn = e3.payback(pre, post, "anthropic/claude-opus-5",
                                         571, rates)
    cut = e3._context(pre[-1]["usage"]) - e3._context(post[0]["usage"])
    assert per_turn == pytest.approx(cut * 0.50 / 1e6)
    assert one_off / per_turn > 10, "a long payback is the finding; this guards it"


def test_a_capture_with_no_compaction_says_so():
    rows = [json.loads(l) for l in (ROOT / "data" / "raw" / "capture.jsonl").read_text()
            .splitlines() if l.strip()] if (ROOT / "data" / "raw" / "capture.jsonl").exists() else None
    if rows is None:
        pytest.skip("capture-01 not present")
    assert e3.find_compaction(rows) is None


def test_the_one_off_and_the_crossing_come_from_the_same_arithmetic():
    """They were computed twice, by two constructions that differed in whether
    the counterfactual turn carried its input and output tokens. The gap was
    $0.00006 — immaterial, and exactly the shape of drift this repo keeps
    finding: one concept, two copies."""
    from agentcostlab.pricing import cost, load_rates
    from agentcostlab.providers import Usage, normalise

    pre, post = e3.find_compaction(_capture())
    rates = load_rates()
    _, _, one_off, _ = e3.payback(pre, post, "anthropic/claude-opus-5", 571, rates)

    usage = normalise("anthropic", post[0]["usage"])
    counterfactual = Usage(cache_read=e3._context(pre[-1]["usage"]),
                           input_uncached=usage.input_uncached,
                           output=usage.output, cache_write_1h=571)
    expected = (cost(usage, "anthropic/claude-opus-5", rates)
                - cost(counterfactual, "anthropic/claude-opus-5", rates))
    assert one_off == pytest.approx(expected, abs=1e-12)
