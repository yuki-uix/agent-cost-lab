"""The two gates every number and every exported record must pass through.

Both are written as coverage tests rather than checklists: the provider list and
the field-policy map are the source of truth, so adding one without the other
fails here instead of shipping.
"""
from __future__ import annotations

import pytest

from agentcostlab.pricing import (AmbiguousCacheWrite, Rate, UnverifiedRate,
                                  cost, load_rates)
from agentcostlab.providers import EXTRACTORS, UnknownProvider, Usage, normalise
from agentcostlab.redact import POLICIES, Policy, UnclassifiedField, redact


# --- normalisation gate -----------------------------------------------------

def test_every_registered_provider_has_rates():
    """New provider without a rate entry → fail here, not in a published number."""
    rates = load_rates()
    priced = {key.split("/", 1)[0] for key in rates if not key.startswith("_")}
    assert set(EXTRACTORS) <= priced, f"unpriced providers: {set(EXTRACTORS) - priced}"


def test_unknown_provider_raises_rather_than_guessing():
    with pytest.raises(UnknownProvider):
        normalise("some-new-vendor", {"input_tokens": 10})


@pytest.mark.parametrize(
    "provider,payload,expected",
    [
        ("anthropic",
         {"input_tokens": 100, "cache_read_input_tokens": 900,
          "cache_creation_input_tokens": 50, "output_tokens": 20,
          "cache_creation": {"ephemeral_5m_input_tokens": 20,
                             "ephemeral_1h_input_tokens": 30}},
         Usage(cache_read=900, input_uncached=100, output=20,
               cache_write_5m=20, cache_write_1h=30)),
        ("deepseek",
         {"prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
          "prompt_tokens": 1000, "completion_tokens": 20},
         Usage(cache_read=900, input_uncached=100, output=20)),
        ("openai",
         {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 900},
          "completion_tokens": 20},
         Usage(cache_read=900, input_uncached=100, output=20)),
    ],
)
def test_usage_normalises_to_the_same_shape(provider, payload, expected):
    assert normalise(provider, payload) == expected


def test_hit_rate_uses_the_split_not_the_total():
    u = normalise("deepseek", {"prompt_cache_hit_tokens": 750,
                               "prompt_cache_miss_tokens": 250,
                               "completion_tokens": 0})
    assert u.hit_rate == 0.75
    assert u.input_total == 1000


# --- pricing gate -----------------------------------------------------------

def test_unverified_rates_refuse_to_produce_a_number():
    """The whole repo exists because second-hand numbers get published as fact."""
    unverified = {"x/y": Rate(1, 0.1, 1.25, 2.0, 5, "blog post", "2026-08-17", False)}
    with pytest.raises(UnverifiedRate):
        cost(Usage(cache_read=0, input_uncached=1000, output=0), "x/y", unverified)


def test_cost_splits_cache_read_from_list_price():
    verified = {"x/y": Rate(input_uncached=5.0, cache_read=0.5, cache_write_5m=6.25,
                            cache_write_1h=10.0, output=25.0, source="official",
                            retrieved_at="2026-08-17", verified=True)}
    # 1M cached reads must not be billed as 1M list-price input.
    cached = cost(Usage(cache_read=1_000_000, input_uncached=0, output=0),
                  "x/y", verified)
    listed = cost(Usage(cache_read=0, input_uncached=1_000_000, output=0),
                  "x/y", verified)
    assert cached == pytest.approx(0.5)
    assert listed == pytest.approx(5.0)
    assert listed / cached == pytest.approx(10.0)


OPUS5 = Rate(input_uncached=5.0, cache_read=0.5, cache_write_5m=6.25,
             cache_write_1h=10.0, output=25.0, source="official",
             retrieved_at="2026-08-19", verified=True)


def test_the_two_cache_write_tiers_are_priced_apart():
    """One `cache_write` field could not express Anthropic's price list: 1.25x
    base input for the 5-minute TTL against 2x for the 1-hour one. 99.5% of the
    tokens written across this repo's captures went to the 1h bucket, so pricing
    them at the 5m rate understated the write component by 37.5%."""
    table = {"x/y": OPUS5}
    five = cost(Usage(cache_read=0, input_uncached=0, output=0,
                      cache_write_5m=1_000_000), "x/y", table)
    hour = cost(Usage(cache_read=0, input_uncached=0, output=0,
                      cache_write_1h=1_000_000), "x/y", table)
    assert five == pytest.approx(6.25)
    assert hour == pytest.approx(10.0)
    assert hour / five == pytest.approx(1.6)


def test_a_write_with_no_ttl_is_refused_not_guessed():
    """Silently choosing the cheaper tier would understate by 37.5%. Present on
    0 of 252 observed records — which is exactly why it must not pass quietly
    the first time it appears."""
    with pytest.raises(AmbiguousCacheWrite):
        cost(Usage(cache_read=0, input_uncached=0, output=0,
                   cache_write_unspecified=1_000), "x/y", {"x/y": OPUS5})


def test_a_provider_without_ttl_tiers_prices_an_unspecified_write():
    """Nothing to be ambiguous about when both tiers cost the same."""
    flat = {"x/y": Rate(input_uncached=1.0, cache_read=0.1, cache_write_5m=1.0,
                        cache_write_1h=1.0, output=2.0, source="official",
                        retrieved_at="2026-08-19", verified=True)}
    assert cost(Usage(cache_read=0, input_uncached=0, output=0,
                      cache_write_unspecified=1_000_000), "x/y", flat) == pytest.approx(1.0)


def test_a_partial_ttl_breakdown_is_not_treated_as_complete():
    """The aggregate is authoritative for the total; a breakdown that does not
    add up leaves the remainder unpriceable rather than lost."""
    u = normalise("anthropic", {"cache_creation_input_tokens": 100,
                                "cache_creation": {"ephemeral_1h_input_tokens": 60}})
    assert u.cache_write_1h == 60
    assert u.cache_write_unspecified == 40
    assert u.cache_write == 100


def test_verified_entries_carry_a_real_source_and_real_numbers():
    """Replaces the tripwire that asserted the table was still empty. A verified
    entry with a TODO source, or with zeros, is a placeholder wearing a badge."""
    for key, rate in load_rates().items():
        if rate.verified:
            assert rate.source.startswith("http"), f"{key}: source is not a URL"
            assert "TODO" not in rate.source, f"{key}: verified against a TODO"
            assert rate.input_uncached > 0 and rate.output > 0, f"{key}: zeroed"
            assert rate.cache_write_1h >= rate.cache_write_5m, f"{key}: tiers inverted"
        else:
            assert rate.input_uncached == 0, f"{key}: unverified but carries numbers"


# A local judgement, not a documented limit: prices change rarely but they do
# change — Claude Sonnet 5's input price moved from $3 to $2 between this
# skill's cached table and the live page, and a `verified: true` stamped before
# that would still read as current today. 180 days is set where a stale rate
# starts being more likely than not to have drifted.
RATE_STALE_AFTER_DAYS = 180


def test_a_verified_rate_goes_stale_instead_of_staying_true_forever():
    """`Rate.age_days` existed and nothing called it, so a rate verified once
    was verified permanently. A gate that quietly stops working with time is
    not a gate."""
    stale = [f"{key} ({rate.age_days()}d, {rate.retrieved_at})"
             for key, rate in load_rates().items()
             if rate.verified and rate.age_days() > RATE_STALE_AFTER_DAYS]
    assert not stale, (
        f"verified rates older than {RATE_STALE_AFTER_DAYS} days: {stale}. "
        "Re-read the official pricing page and update retrieved_at, or set "
        "verified back to false."
    )


def test_the_staleness_check_can_actually_fail():
    """Asserting on the shipped table alone would pass forever while the table
    is fresh, including if age_days were wired up wrong."""
    old = Rate(input_uncached=5.0, cache_read=0.5, cache_write_5m=6.25,
               cache_write_1h=10.0, output=25.0, source="https://example.test",
               retrieved_at="2020-01-01", verified=True)
    assert old.age_days() > RATE_STALE_AFTER_DAYS


# --- export gate ------------------------------------------------------------

def test_unclassified_field_fails_closed():
    with pytest.raises(UnclassifiedField):
        redact({"t_start": 1.0, "surprise_new_field": "secret"})


def test_drop_and_hash_policies_are_enforced():
    out = redact({"t_start": 1.0, "headers": {"authorization": "Bearer sk-live"},
                  "request_body": {"system": "proprietary"}})
    assert "headers" not in out
    assert out["request_body"].startswith("sha256:")
    assert "proprietary" not in str(out)


def test_no_policy_is_left_unset():
    assert all(isinstance(p, Policy) for p in POLICIES.values())


def test_a_real_captured_record_prices_end_to_end():
    """Units are easy to get right in isolation; wiring is where cost numbers go
    wrong. This is the compaction turn E3 is about — capture-03, the first
    request of the post-compact conversation — priced through the real path and
    checked against the arithmetic done by hand.
    """
    import json
    from pathlib import Path

    capture = Path(__file__).resolve().parents[1] / "data" / "raw" / "capture-03.jsonl"
    if not capture.exists():
        pytest.skip(f"capture-03 not present at {capture}; nothing to price")
    rows = [json.loads(line) for line in capture.read_text().splitlines() if line.strip()]
    record = next((r for r in rows
                   if (r.get("usage") or {}).get("cache_creation_input_tokens") == 21933),
                  None)
    assert record is not None, "the compaction turn is missing from capture-03"

    usage = normalise("anthropic", record["usage"])
    assert (usage.cache_read, usage.cache_write_5m, usage.cache_write_1h) == (49537, 0, 21933)

    # (49537*0.50 + 0*6.25 + 21933*10.00 + 2*5.00 + 2*25.00) / 1e6, per MTok
    #  = (24768.5 + 0 + 219330 + 10 + 50) / 1e6
    assert cost(usage, "anthropic/claude-opus-5") == pytest.approx(0.2441585)

    # What the single-rate schema would have produced: every write at the 5m
    # price. 34% low, on the one turn whose cost E3 exists to measure.
    naive = (usage.cache_read * 0.50 + usage.cache_write * 6.25
             + usage.input_uncached * 5.0 + usage.output * 25.0) / 1e6
    assert naive == pytest.approx(0.16190975)
    assert 1 - naive / 0.2441585 == pytest.approx(0.337, abs=0.001)
