"""The two gates every number and every exported record must pass through.

Both are written as coverage tests rather than checklists: the provider list and
the field-policy map are the source of truth, so adding one without the other
fails here instead of shipping.
"""
from __future__ import annotations

import pytest

from agentcostlab.pricing import Rate, UnverifiedRate, cost, load_rates
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
          "cache_creation_input_tokens": 50, "output_tokens": 20},
         Usage(cache_read=900, cache_write=50, input_uncached=100, output=20)),
        ("deepseek",
         {"prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
          "prompt_tokens": 1000, "completion_tokens": 20},
         Usage(cache_read=900, cache_write=0, input_uncached=100, output=20)),
        ("openai",
         {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 900},
          "completion_tokens": 20},
         Usage(cache_read=900, cache_write=0, input_uncached=100, output=20)),
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
    unverified = {"x/y": Rate(1, 0.1, 1.25, 5, "blog post", "2026-08-17", False)}
    with pytest.raises(UnverifiedRate):
        cost(Usage(0, 0, 1000, 0), "x/y", unverified)


def test_cost_splits_cache_read_from_list_price():
    verified = {"x/y": Rate(input_uncached=5.0, cache_read=0.5, cache_write=6.25,
                            output=25.0, source="official", retrieved_at="2026-08-17",
                            verified=True)}
    # 1M cached reads must not be billed as 1M list-price input.
    cached = cost(Usage(cache_read=1_000_000, cache_write=0, input_uncached=0, output=0),
                  "x/y", verified)
    listed = cost(Usage(cache_read=0, cache_write=0, input_uncached=1_000_000, output=0),
                  "x/y", verified)
    assert cached == pytest.approx(0.5)
    assert listed == pytest.approx(5.0)
    assert listed / cached == pytest.approx(10.0)


def test_shipped_rate_table_is_still_unverified():
    """Deliberate tripwire: delete this test when the rates are filled in."""
    rates = load_rates()
    assert not any(r.verified for r in rates.values()), (
        "rates were verified — remove this tripwire and start reporting costs"
    )


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
