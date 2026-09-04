"""The cost-by-cause bridge, tested through ``cost_by_cause()`` itself.

Every test calls the public function and checks its output against numbers
worked out by hand from the rate table — never re-implementing the bucket
assignment in the test. The one test that reads a real capture skips, naming
the file it needs, when ``data/raw/`` is absent.
"""
from __future__ import annotations

import json
import random
from dataclasses import fields
from pathlib import Path

import pytest

from agentcostlab import analysis
from agentcostlab.analysis import (
    CauseBreakdown,
    CauseCost,
    MissingModel,
    NonAnthropicProvider,
    RecordRateMismatch,
    RepaidExceedsCapacity,
    cost_by_cause,
)
from agentcostlab.attribute import Divergence
from agentcostlab.pricing import AmbiguousCacheWrite, Rate, UnverifiedRate

# A verified rate table with round numbers, so the hand arithmetic below is
# trivial to check: input 1.0, read 0.1, write 5m 1.25, write 1h 2.0. The
# provider/model of the key match the record/body helpers further down, so the
# per-record rate-key validation (P2) holds.
SIMPLE = {
    "anthropic/claude-sonnet-5": Rate(input_uncached=1.0, cache_read=0.1,
                                       cache_write_5m=1.25, cache_write_1h=2.0,
                                       output=5.0, source="https://example.test",
                                       retrieved_at="2026-08-19", verified=True),
    # A second Anthropic model at a clean 3x the sonnet rate, so a mixed-model
    # capture's per-record pricing is trivially hand-checkable.
    "anthropic/claude-opus-5": Rate(input_uncached=3.0, cache_read=0.3,
                                     cache_write_5m=3.75, cache_write_1h=6.0,
                                     output=15.0, source="https://example.test",
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


def body(system_text, model="claude-sonnet-5"):
    return {"model": model, "system": system_text,
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
    return cost_by_cause(rows)


def test_capture_02_repaid_tokens_is_exactly_2822(capture_02_breakdown):
    """AC1 hard gate: the conservation identity must reproduce the reported 2,822
    tokens paid twice, exactly — not approximately, not from a tokenizer."""
    total = sum(c.repaid_tokens for c in capture_02_breakdown.by_cause) \
        + capture_02_breakdown.ambiguous.repaid_tokens \
        + capture_02_breakdown.unattributed.repaid_tokens
    assert total == 2822

    # Exactly one turn broke across the whole capture, split across the three
    # mutually exclusive buckets.
    assert sum(c.turns for c in capture_02_breakdown.by_cause) \
        + capture_02_breakdown.ambiguous.turns \
        + capture_02_breakdown.unattributed.turns == 1


def test_capture_02_interval_matches_the_hand_calc(capture_02_breakdown):
    """AC1: usd_low / usd_high must match the arithmetic done from the rate table.

    Rate: anthropic/claude-sonnet-5 (retrieved 2026-08-19), input 2.00, cache_read
    0.20, cache_write_5m 2.50, cache_write_1h 4.00. Record 61's usage carries
    input_uncached 3,780 and cache_write_5m 1,038, no 1h write. Greedy fill by
    unit price, each bucket capped at its own capacity:

        usd_low — cheapest first: input (2.00) has capacity 3,780 >= 2,822, so
            all 2,822 fit there:
            2822 * (2.00 - 0.20) / 1e6 = 2822 * 1.80 / 1e6 = 0.0050796

        usd_high — most expensive first: the 5m write (2.50) fills its whole
            1,038 first, and the remaining 2,822 - 1,038 = 1,784 spill into
            input (2.00):
            1038 * (2.50 - 0.20) = 1038 * 2.30 = 2387.4
            1784 * (2.00 - 0.20) = 1784 * 1.80 = 3211.2
            2387.4 + 3211.2 = 5598.6 / 1e6 = 0.0055986
    """
    # Exactly one of the three buckets holds the single break turn; pick it.
    buckets = [c for c in [*capture_02_breakdown.by_cause,
                           capture_02_breakdown.ambiguous,
                           capture_02_breakdown.unattributed] if c.turns]
    (cause,) = buckets
    assert cause.repaid_tokens == 2822
    assert cause.usd_low == pytest.approx(0.0050796)
    assert cause.usd_high == pytest.approx(0.0055986)


def test_capture_02_break_is_ambiguous_not_system(capture_02_breakdown):
    """The one real break has *two* diverging components, not one.

    ``attribute()`` names ``system`` (``field removed: 'ttl'``) only because
    ``system`` precedes ``messages`` in ``SEGMENT_ORDER``. The same turn also adds
    a ``cache_control`` marker to a ``tool_use`` block in ``messages``, so under
    a ``messages``-first order the attributor names ``messages`` instead. That is
    exactly the unreliability this task exists to flag: the turn must land in
    ``ambiguous`` with both candidates listed, never in ``by_cause[system]``."""
    assert capture_02_breakdown.unattributed.turns == 0
    assert capture_02_breakdown.by_cause == []
    assert capture_02_breakdown.ambiguous.turns == 1
    assert capture_02_breakdown.ambiguous.repaid_tokens == 2822
    assert set(capture_02_breakdown.ambiguous.candidates) == {"system", "messages"}


# --- AC3: the interval must span two ends, not collapse to a midpoint --------

def test_interval_spans_when_two_buckets_are_present(simple_rates):
    """AC3: with both a 1h write and uncached input actually present that turn,
    the repaid tokens have a cheap end and an expensive end — report both."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        # cache_read 800 of an expected 1200 -> repaid 400. Both buckets carry at
        # least 400 tokens, so each can hold the whole repaid amount: low packs
        # them all into input (1.00), high packs them all into the 1h write (2.00).
        rec("b", prev_msg="a", body=body("B"),
            usage=usage(cache_read=800, input_tokens=400, hour=400)),
    ]
    b = cost_by_cause(records)

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
        # Only a 5m write is present (no uncached input, no 1h write), carrying
        # at least the full 400 repaid tokens so the single bucket holds them all.
        rec("b", prev_msg="a", body=body("B"), usage=usage(cache_read=800, five=400)),
    ]
    b = cost_by_cause(records)

    system = cause_for(b, "system")
    assert system.repaid_tokens == 400
    assert system.usd_low == system.usd_high == pytest.approx(400 * (1.25 - 0.10) / 1e6)


# --- P1: each bucket is capped at its own capacity ---------------------------

def test_repaid_exceeding_bucket_capacity_refuses(simple_rates):
    """Repaid tokens cannot exceed what the turn's non-cache-read buckets carried;
    that would break the conservation premise, so it refuses rather than scale."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        # repaid 400, but input (100) + 5m write (50) carried only 150 tokens.
        rec("b", prev_msg="a", body=body("B"),
            usage=usage(cache_read=800, input_tokens=100, five=50)),
    ]
    with pytest.raises(RepaidExceedsCapacity) as exc_info:
        cost_by_cause(records)
    msg = str(exc_info.value)
    assert "400" in msg and "150" in msg


def test_repaid_equal_to_capacity_fills_every_bucket_to_the_brim(simple_rates):
    """Repaid == total capacity is the one no-spill case: every bucket fills to its
    own token count, so the upper bound is each bucket's full-capacity subtotal."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(800, 200)),
        # expected 1000, read 850 -> repaid 150; input (100) + 5m write (50) = 150.
        rec("b", prev_msg="a", body=body("B"),
            usage=usage(cache_read=850, input_tokens=100, five=50)),
    ]
    b = cost_by_cause(records)

    system = cause_for(b, "system")
    assert system.repaid_tokens == 150
    # input 100 @ (1.00-0.10) = 90 ; 5m 50 @ (1.25-0.10) = 57.5 ; total 147.5.
    assert system.usd_low == pytest.approx((100 * 0.90 + 50 * 1.15) / 1e6)
    assert system.usd_high == pytest.approx((100 * 0.90 + 50 * 1.15) / 1e6)


def test_a_zero_capacity_bucket_never_sets_the_bound(simple_rates):
    """A bucket that carried 0 tokens cannot set the upper bound even when its rate
    is the most expensive one present (the 1h write at 2.00 here)."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        # input carries the full 400; the 1h write is present at rate 2.00 but 0
        # tokens, so it must not set usd_high.
        rec("b", prev_msg="a", body=body("B"),
            usage=usage(cache_read=800, input_tokens=400, hour=0)),
    ]
    b = cost_by_cause(records)

    system = cause_for(b, "system")
    assert system.repaid_tokens == 400
    assert system.usd_high == pytest.approx(400 * (1.00 - 0.10) / 1e6)  # not 2.00


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
    """A record whose rate resolves but is ``verified: false`` must refuse, not
    publish a number from a blog-post rate."""
    unverified = {"anthropic/claude-sonnet-5": Rate(input_uncached=1.0, cache_read=0.1, cache_write_5m=1.25,
                              cache_write_1h=2.0, output=5.0, source="blog post",
                              retrieved_at="2026-08-17", verified=False)}
    monkeypatch.setattr(analysis, "load_rates", lambda: unverified)
    records = [rec("a", body=body("A"), usage=prev_usage(1000, 200))]
    with pytest.raises(UnverifiedRate):
        cost_by_cause(records)


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
        cost_by_cause(records)


# --- the three "cannot tell" buckets stay separate ---------------------------

def test_a_break_the_attributor_cannot_explain_is_unattributed_and_priced(simple_rates):
    """Ledger says broke, bodies identical -> no cause. The money is still real:
    it goes to ``unattributed`` and is priced, not folded into a cause."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        # input carries the full 400 repaid tokens, so it is the only bucket in play.
        rec("b", prev_msg="a", body=body("A"), usage=usage(cache_read=800, input_tokens=400)),
    ]
    b = cost_by_cause(records)

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
    b = cost_by_cause(records)

    assert b.disputed_turns == 1
    assert not b.by_cause
    assert b.unattributed.turns == 0
    assert b.unattributed.repaid_tokens == 0


def test_missing_usage_is_not_measured_not_a_break(simple_rates):
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 100)),
        rec("b", prev_msg="a", body=body("B")),  # no usage key at all
    ]
    b = cost_by_cause(records)
    assert b.not_measured_turns == 1
    assert not b.by_cause
    assert b.disputed_turns == 0


def test_reading_more_than_expected_is_not_a_break_nor_an_anomaly(simple_rates):
    """``curr.cache_read > prev.cache_read + prev.cache_write`` is an over-read,
    not a break: the shortfall criterion (AC4) sees no loss. It is never dumped
    in ``not_measured`` nor reported as a negative loss — the attributor's
    divergence is recorded as a dispute, counted only."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 100)),
        rec("b", prev_msg="a", body=body("B"), usage=usage(cache_read=1200)),
    ]
    b = cost_by_cause(records)
    assert b.not_measured_turns == 0
    assert not b.by_cause
    assert b.unattributed.repaid_tokens == 0
    assert b.disputed_turns == 1


# --- hit_usd_saved is reported apart from losses -----------------------------

def test_hit_usd_saved_is_the_read_discount_not_netted_against_losses(simple_rates):
    """Every cache read is billed at 0.1x instead of 1x; the difference is the
    saving, and it is a separate field, never subtracted from the break losses."""
    records = [rec("a", body=body("A"), usage=usage(cache_read=1000))]
    b = cost_by_cause(records)
    assert b.hit_usd_saved == pytest.approx(1000 * (1.0 - 0.1) / 1e6)
    assert not b.by_cause
    assert b.unattributed.usd_low == 0.0


# --- P2: each record prices itself, or refuses naming itself -----------------

def test_a_mixed_model_capture_prices_each_record_at_its_own_rate(simple_rates):
    """#59: a capture mixing two models is priced per-record — each break at its
    own model's rate, summed — never by merging tokens and averaging rates."""
    records = [
        # sonnet lineage: expected 1200, read 800 -> repaid 400 @ sonnet
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        rec("b", prev_msg="a", body=body("B"),
            usage=usage(cache_read=800, input_tokens=400)),
        # opus lineage: expected 2500, read 2300 -> repaid 200 @ opus
        rec("c", body=body("C", model="claude-opus-5"), usage=prev_usage(2000, 500)),
        rec("d", prev_msg="c", body=body("D", model="claude-opus-5"),
            usage=usage(cache_read=2300, input_tokens=200)),
    ]
    b = cost_by_cause(records)

    system = cause_for(b, "system")
    assert system.turns == 2
    assert system.repaid_tokens == 600
    # sonnet: 400 @ (1.00 - 0.10); opus: 200 @ (3.00 - 0.30); one bucket each.
    assert system.usd_low == pytest.approx((400 * 0.90 + 200 * 2.70) / 1e6)
    assert system.usd_high == pytest.approx((400 * 0.90 + 200 * 2.70) / 1e6)


def test_an_unknown_model_refuses_and_names_the_record(simple_rates):
    """A model with no rate entry must refuse, naming the record index and the
    provider/model that could not be resolved — not bill it at some other rate."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        rec("b", prev_msg="a", body={"model": "claude-haiku-4-5", "system": "B"},
            usage=usage(cache_read=800, input_tokens=400)),
    ]
    with pytest.raises(RecordRateMismatch) as exc_info:
        cost_by_cause(records)
    msg = str(exc_info.value)
    assert "record 1" in msg
    assert "claude-haiku-4-5" in msg
    assert "anthropic/claude-haiku-4-5" in msg


def test_a_record_without_a_model_field_refuses(simple_rates):
    """No ``model`` field -> refuse rather than guess: a body that omits the model
    cannot be priced, and guessing the most common model would bill a possibly
    different model at the wrong rate."""
    records = [
        rec("a", body={"system": "A"}, usage=prev_usage(1000, 200)),
        rec("b", prev_msg="a", body={"system": "B"},
            usage=usage(cache_read=800, input_tokens=400)),
    ]
    with pytest.raises(MissingModel) as exc_info:
        cost_by_cause(records)
    msg = str(exc_info.value)
    assert "record 0" in msg
    assert "model" in msg


# --- P3: non-Anthropic providers are refused outright ------------------------

def test_a_deepseek_record_refuses_naming_the_conservation_limit(simple_rates):
    """The conservation identity reads Anthropic usage keys, so a DeepSeek record
    must refuse with an explanation, not read 0 on both sides and pass a real
    break off as "not broken". Usage is DeepSeek-shaped (hit/miss), which is the
    shape that would have silently read as 0 under the Anthropic keys."""
    records = [
        rec("a", provider="deepseek", body={"model": "deepseek-chat", "system": "A"},
            usage={"prompt_cache_hit_tokens": 1000, "prompt_cache_miss_tokens": 0,
                   "completion_tokens": 0}),
        rec("b", prev_msg="a", provider="deepseek",
            body={"model": "deepseek-chat", "system": "B"},
            usage={"prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 400,
                   "completion_tokens": 0}),
    ]
    with pytest.raises(NonAnthropicProvider) as exc_info:
        cost_by_cause(records)
    msg = str(exc_info.value)
    assert "deepseek" in msg
    assert "conservation" in msg


def test_a_deepseek_record_among_anthropic_records_names_its_index(simple_rates):
    """The non-Anthropic refusal names which record offends — a single DeepSeek
    record slipped into an otherwise-Anthropic capture must point at that record,
    not at the capture as a whole."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        rec("b", prev_msg="a", provider="deepseek", body=body("B"),
            usage=usage(cache_read=800, input_tokens=400)),
    ]
    with pytest.raises(NonAnthropicProvider) as exc_info:
        cost_by_cause(records)
    msg = str(exc_info.value)
    assert "record 1" in msg
    assert "deepseek" in msg


def test_anthropic_records_are_unaffected_by_the_provider_gate(simple_rates):
    """The non-Anthropic gate must not touch an Anthropic capture."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        rec("b", prev_msg="a", body=body("B"), usage=usage(cache_read=800, input_tokens=400)),
    ]
    b = cost_by_cause(records)
    assert cause_for(b, "system").repaid_tokens == 400


# --- AC4: a multi-candidate break is never charged to a single cause ---------

def test_a_two_component_break_is_ambiguous_not_charged_to_a_cause(simple_rates):
    """AC4: when two components diverge, ``order_stability`` is 1/2 and the turn
    must not be charged to either name — it lands in ``ambiguous`` with both
    candidates, priced but never attributed to ``system`` or ``messages`` alone.

    This guards the routing in ``cost_by_cause`` against a future mutation that
    would let ``d.order_stability < 1.0`` slip into ``by_cause``: the moment a
    multi-candidate turn is folded into a single cause, this test goes red."""
    records = [
        rec("a", body={"model": "claude-sonnet-5", "system": "A",
                       "messages": [{"role": "user", "content": "hi"}]},
            usage=prev_usage(1000, 200)),
        rec("b", prev_msg="a",
            body={"model": "claude-sonnet-5", "system": "B",
                  "messages": [{"role": "user", "content": "bye"}]},
            usage=usage(cache_read=800, input_tokens=400)),
    ]
    b = cost_by_cause(records)

    assert b.by_cause == []
    assert b.unattributed.turns == 0
    assert b.ambiguous.turns == 1
    assert b.ambiguous.repaid_tokens == 400
    assert set(b.ambiguous.candidates) == {"system", "messages"}
    assert b.ambiguous.usd_low == pytest.approx(400 * (1.00 - 0.10) / 1e6)


# --- AC5: the three buckets are mutually exclusive and exhaustive -------------

def test_the_three_buckets_are_exhaustive_and_disjoint(simple_rates):
    """AC5: every broke turn lands in exactly one of ``by_cause`` / ``ambiguous``
    / ``unattributed`` — never two, never zero. The turn counts sum to the number
    of broke turns, so no repaid token is counted twice or dropped."""
    records = [
        # (1) one diverging component -> by_cause["system"]
        rec("a1", body={"model": "claude-sonnet-5", "system": "A",
                        "messages": [{"role": "user", "content": "hi"}]},
            usage=prev_usage(1000, 200)),
        rec("b1", prev_msg="a1",
            body={"model": "claude-sonnet-5", "system": "B",
                  "messages": [{"role": "user", "content": "hi"}]},
            usage=usage(cache_read=800, input_tokens=400)),
        # (2) two diverging components -> ambiguous
        rec("a2", body={"model": "claude-sonnet-5", "system": "A",
                        "messages": [{"role": "user", "content": "hi"}]},
            usage=prev_usage(1000, 200)),
        rec("b2", prev_msg="a2",
            body={"model": "claude-sonnet-5", "system": "B",
                  "messages": [{"role": "user", "content": "bye"}]},
            usage=usage(cache_read=800, input_tokens=400)),
        # (3) no divergence -> unattributed
        rec("a3", body={"model": "claude-sonnet-5", "system": "A",
                        "messages": [{"role": "user", "content": "hi"}]},
            usage=prev_usage(1000, 200)),
        rec("b3", prev_msg="a3",
            body={"model": "claude-sonnet-5", "system": "A",
                  "messages": [{"role": "user", "content": "hi"}]},
            usage=usage(cache_read=800, input_tokens=400)),
    ]
    b = cost_by_cause(records)

    assert len(b.by_cause) == 1
    assert cause_for(b, "system").turns == 1
    assert cause_for(b, "system").repaid_tokens == 400
    assert b.ambiguous.turns == 1
    assert b.ambiguous.repaid_tokens == 400
    assert set(b.ambiguous.candidates) == {"system", "messages"}
    assert b.unattributed.turns == 1
    assert b.unattributed.repaid_tokens == 400

    # exhaustive and disjoint: 3 broke turns -> 3 bucket turns, 1200 repaid tokens
    assert sum(c.turns for c in b.by_cause) + b.ambiguous.turns + b.unattributed.turns == 3
    assert (sum(c.repaid_tokens for c in b.by_cause)
            + b.ambiguous.repaid_tokens + b.unattributed.repaid_tokens) == 1200
    assert b.disputed_turns == 0
    assert b.not_measured_turns == 0


# --- AC7: the three #50 leftovers --------------------------------------------

def test_three_bucket_overflow_refuses(simple_rates):
    """#50 leftover: when all three non-cache-read buckets carry tokens but their
    total capacity still falls short of ``repaid``, refuse rather than scale."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        # expected 1200, read 200 -> repaid 1000; input (100) + 5m (50) + 1h (50)
        # carry only 200 tokens.
        rec("b", prev_msg="a", body=body("B"),
            usage=usage(cache_read=200, input_tokens=100, five=50, hour=50)),
    ]
    with pytest.raises(RepaidExceedsCapacity) as exc_info:
        cost_by_cause(records)
    msg = str(exc_info.value)
    assert "1000" in msg and "200" in msg


def test_no_non_cache_read_bucket_refuses(simple_rates):
    """#50 leftover: repaid > 0 but no non-cache-read bucket carried tokens means
    the repaid tokens had nowhere to land, so the premise fails and it refuses."""
    records = [
        rec("a", body=body("A"), usage=prev_usage(1000, 200)),
        # repaid 400, but input / 5m / 1h all carry 0 tokens.
        rec("b", prev_msg="a", body=body("B"), usage=usage(cache_read=800)),
    ]
    with pytest.raises(RepaidExceedsCapacity) as exc_info:
        cost_by_cause(records)
    msg = str(exc_info.value)
    assert "400" in msg and "0" in msg


def test_every_priced_bucket_satisfies_zero_le_low_le_high(simple_rates):
    """#50 leftover: over random usage, 0 <= usd_low <= usd_high always. The
    cache-read rate is the cheapest of the five, so re-billing can only cost
    more; the high end packs the most expensive bucket first by construction.
    A fixed seed keeps the 200-turn sweep reproducible."""
    rng = random.Random(20260903)
    for i in range(200):
        capacity_input = rng.randint(1, 500)
        capacity_five = rng.randint(1, 500)
        capacity_hour = rng.randint(1, 500)
        capacity = capacity_input + capacity_five + capacity_hour
        repaid = rng.randint(1, capacity)  # guaranteed to fit -> no refusal
        # prev expected = cache_read + creation = (repaid + 400); curr reads 400,
        # so the conservation identity yields exactly `repaid`.
        records = [
            rec(f"a{i}", body=body("A"), usage=prev_usage(repaid + 200, 200)),
            rec(f"b{i}", prev_msg=f"a{i}", body=body("B"),
                usage=usage(cache_read=400, input_tokens=capacity_input,
                            five=capacity_five, hour=capacity_hour)),
        ]
        b = cost_by_cause(records)
        system = cause_for(b, "system")
        assert system.turns == 1, i
        assert system.repaid_tokens == repaid, i
        assert 0.0 <= system.usd_low <= system.usd_high, (
            i, capacity_input, capacity_five, capacity_hour, repaid)
