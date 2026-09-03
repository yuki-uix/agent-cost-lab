"""The cost-by-cause bridge, tested through ``cost_by_cause()`` itself.

Every test calls the public function and checks its output against numbers
worked out by hand from the rate table — never re-implementing the bucket
assignment in the test. The one test that reads a real capture skips, naming
the file it needs, when ``data/raw/`` is absent.
"""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from agentcostlab import analysis
from agentcostlab.analysis import CauseBreakdown, CauseCost, cost_by_cause
from agentcostlab.attribute import Divergence
from agentcostlab.pricing import AmbiguousCacheWrite, Rate, UnverifiedRate

# A verified rate table with round numbers, so the hand arithmetic below is
# trivial to check: input 1.0, read 0.1, write 5m 1.25, write 1h 2.0.
SIMPLE = {
    "x/y": Rate(input_uncached=1.0, cache_read=0.1, cache_write_5m=1.25,
                cache_write_1h=2.0, output=5.0, source="https://example.test",
                retrieved_at="2026-08-19", verified=True),
}


@pytest.fixture
def simple_rates(monkeypatch):
    monkeypatch.setattr(analysis, "load_rates", lambda: SIMPLE)


def rec(resp, prev_msg=None, *, provider="anthropic", usage=None, body=None):
    record = {"response_id": resp, "provider": provider,
              "request_body": body if body is not None else {}}
    if prev_msg is not None:
        record["injected_previous_message_id"] = prev_msg
    if usage is not None:
        record["usage"] = usage
    return record


def body(system_text):
    return {"model": "claude-sonnet-5", "system": system_text,
            "messages": [{"role": "user", "content": "hi"}]}


def usage(cache_read=0, input_tokens=0, five=0, hour=0, output=0):
    """Anthropic-shaped usage with a complete TTL breakdown."""
    return {"input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": five + hour,
            "cache_creation": {"ephemeral_5m_input_tokens": five,
                               "ephemeral_1h_input_tokens": hour},
            "output_tokens": output}


def prev_usage(cache_read, creation):
    """The only two fields ``broke_cache`` reads off the previous turn."""
    return {"cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": creation}


def cause_for(breakdown, cause):
    return next(c for c in breakdown.by_cause if c.cause == cause)


# --- AC1 / AC2: the one real cache break ------------------------------------

@pytest.fixture
def capture_02_breakdown():
    capture = Path(__file__).resolve().parents[1] / "data" / "raw" / "capture-02.jsonl"
    if not capture.exists():
        pytest.skip(f"capture-02 not present at {capture}; AC1 cannot be checked")
    rows = [json.loads(line) for line in capture.read_text().splitlines() if line.strip()]
    return cost_by_cause(rows, "anthropic/claude-sonnet-5")


def test_capture_02_repaid_tokens_is_exactly_2822(capture_02_breakdown):
    """AC1 hard gate: the conservation identity must reproduce the reported 2,822
    tokens paid twice, exactly — not approximately, not from a tokenizer."""
    total = sum(c.repaid_tokens for c in capture_02_breakdown.by_cause) \
        + capture_02_breakdown.unattributed.repaid_tokens
    assert total == 2822

    # Exactly one turn broke across the whole capture.
    assert sum(c.turns for c in capture_02_breakdown.by_cause) \
        + capture_02_breakdown.unattributed.turns == 1


def test_capture_02_interval_matches_the_hand_calc(capture_02_breakdown):
    """AC1: usd_low / usd_high must match the arithmetic done from the rate table.

    Rate: anthropic/claude-sonnet-5 (retrieved 2026-08-19), input 2.00, cache_read
    0.20, cache_write_5m 2.50, cache_write_1h 4.00. Record 61's usage carries
    input_uncached 3,780 and cache_write_5m 1,038, no 1h write — so the two
    non-cache-read buckets present are input (2.00) and 5m write (2.50).

        usd_low  = 2822 * (2.00 - 0.20) / 1e6 = 2822 * 1.80 / 1e6 = 0.0050796
        usd_high = 2822 * (2.50 - 0.20) / 1e6 = 2822 * 2.30 / 1e6 = 0.0064906
    """
    if capture_02_breakdown.unattributed.turns:
        cause = capture_02_breakdown.unattributed
    else:
        (cause,) = [c for c in capture_02_breakdown.by_cause if c.turns]
    assert cause.repaid_tokens == 2822
    assert cause.usd_low == pytest.approx(0.0050796)
    assert cause.usd_high == pytest.approx(0.0064906)


def test_capture_02_break_is_attributed_to_system(capture_02_breakdown):
    """AC2, reported not forced: the TTL-switch break lands in ``system``, not
    ``unattributed``. The attributor names ``system`` / ``field removed: 'ttl'``,
    which is a *real* attribution, so ``unattributed`` must stay empty."""
    assert capture_02_breakdown.unattributed.turns == 0
    system = cause_for(capture_02_breakdown, "system")
    assert system.turns == 1
    assert system.repaid_tokens == 2822


# --- AC3: the interval must span two ends, not collapse to a midpoint --------

def test_interval_spans_when_two_buckets_are_present(simple_rates):
    """AC3: with both a 1h write and uncached input actually present that turn,
    the repaid tokens have a cheap end and an expensive end — report both."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        # cache_read 800 of an expected 1200 -> repaid 400; input_uncached and a
        # 1h write are both non-zero, so low=1.00 and high=2.00 are both in play.
        rec("b", prev_msg="a", body=body("B"),
            usage=usage(cache_read=800, input_tokens=100, hour=50)),
    ]
    b = cost_by_cause(records, "x/y")

    system = cause_for(b, "system")
    assert system.turns == 1
    assert system.repaid_tokens == 400
    assert system.usd_high > system.usd_low
    assert system.usd_low == pytest.approx(400 * (1.00 - 0.10) / 1e6)   # 0.00036
    assert system.usd_high == pytest.approx(400 * (2.00 - 0.10) / 1e6)  # 0.00076


def test_a_single_bucket_degrades_to_a_point(simple_rates):
    """The one case where a point estimate is allowed: only one non-cache-read
    bucket actually carried tokens, so low and high coincide."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        # Only a 5m write is present (no uncached input, no 1h write).
        rec("b", prev_msg="a", body=body("B"), usage=usage(cache_read=800, five=50)),
    ]
    b = cost_by_cause(records, "x/y")

    system = cause_for(b, "system")
    assert system.repaid_tokens == 400
    assert system.usd_low == system.usd_high == pytest.approx(400 * (1.25 - 0.10) / 1e6)


# --- AC4: bytes and tokens are two quantities --------------------------------

def test_bytes_and_tokens_never_share_a_numeric_column():
    """``Divergence`` reports JSON bytes; ``CauseCost`` reports billing tokens and
    dollars. The two quantities are uncalibrated in this repo, so no output type
    may carry them side by side. Enforced by the actual field lists, not a checklist."""
    byte_fields = {f.name for f in fields(Divergence) if f.name.startswith("bytes_")}
    assert byte_fields == {"bytes_before", "bytes_after"}
    for cls in (CauseCost, CauseBreakdown):
        shared = byte_fields & {f.name for f in fields(cls)}
        assert not shared, f"{cls.__name__} mixes byte counts with tokens/dollars: {shared}"


# --- AC5: unverified rates refuse, and an unpriceable write propagates -------

def test_unverified_rate_raises_instead_of_a_partial_result(monkeypatch):
    unverified = {"x/y": Rate(input_uncached=1.0, cache_read=0.1, cache_write_5m=1.25,
                              cache_write_1h=2.0, output=5.0, source="blog post",
                              retrieved_at="2026-08-17", verified=False)}
    monkeypatch.setattr(analysis, "load_rates", lambda: unverified)
    with pytest.raises(UnverifiedRate):
        cost_by_cause([], "x/y")


def test_an_unpriceable_cache_write_propagates(simple_rates):
    """A broken turn whose write arrived with no TTL must refuse, not guess a tier."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 100)),
        # No `cache_creation` breakdown -> the write has an unspecified TTL.
        rec("b", prev_msg="a", body=body("B"),
            usage={"input_tokens": 0, "cache_read_input_tokens": 900,
                   "cache_creation_input_tokens": 100, "output_tokens": 0}),
    ]
    with pytest.raises(AmbiguousCacheWrite):
        cost_by_cause(records, "x/y")


# --- the three "cannot tell" buckets stay separate ---------------------------

def test_a_break_the_attributor_cannot_explain_is_unattributed_and_priced(simple_rates):
    """Ledger says broke, bodies identical -> no cause. The money is still real:
    it goes to ``unattributed`` and is priced, not folded into a cause."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        rec("b", prev_msg="a", body=body("A"), usage=usage(cache_read=800, input_tokens=50)),
    ]
    b = cost_by_cause(records, "x/y")

    assert not b.by_cause
    assert b.unattributed.turns == 1
    assert b.unattributed.repaid_tokens == 400
    assert b.unattributed.usd_low == pytest.approx(400 * (1.00 - 0.10) / 1e6)


def test_a_divergence_the_ledger_does_not_see_is_disputed_not_priced(simple_rates):
    """Attributor reports a break, ledger says the cache survived. Counted only —
    assigning it a dollar figure would overstate the losses."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 100)),
        # cache_read equals the expected 1100 -> no break; bodies diverge anyway.
        rec("b", prev_msg="a", body=body("B"), usage=usage(cache_read=1100)),
    ]
    b = cost_by_cause(records, "x/y")

    assert b.disputed_turns == 1
    assert not b.by_cause
    assert b.unattributed.turns == 0
    assert b.unattributed.repaid_tokens == 0


def test_missing_usage_is_not_measured_not_a_break(simple_rates):
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 100)),
        rec("b", prev_msg="a", body=body("B")),  # no usage key at all
    ]
    b = cost_by_cause(records, "x/y")
    assert b.not_measured_turns == 1
    assert not b.by_cause
    assert b.disputed_turns == 0


def test_reading_more_than_expected_is_an_anomaly_not_a_negative_loss(simple_rates):
    """``repaid < 0`` means the instrument saw more than the ledger predicts. It
    is never clamped to zero and never reported as a negative loss."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 100)),
        rec("b", prev_msg="a", body=body("B"), usage=usage(cache_read=1200)),
    ]
    b = cost_by_cause(records, "x/y")
    assert b.not_measured_turns == 1
    assert not b.by_cause
    assert b.unattributed.repaid_tokens == 0


# --- hit_usd_saved is reported apart from losses -----------------------------

def test_hit_usd_saved_is_the_read_discount_not_netted_against_losses(simple_rates):
    """Every cache read is billed at 0.1x instead of 1x; the difference is the
    saving, and it is a separate field, never subtracted from the break losses."""
    records = [rec("a", usage=usage(cache_read=1000))]
    b = cost_by_cause(records, "x/y")
    assert b.hit_usd_saved == pytest.approx(1000 * (1.0 - 0.1) / 1e6)
    assert not b.by_cause
    assert b.unattributed.usd_low == 0.0
