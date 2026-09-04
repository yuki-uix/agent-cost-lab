"""The oracle the attributor is checked against needs checking itself.

It was written out longhand in three places before it became a module; each of
these cases is one the longhand version got wrong or would have.
"""
from __future__ import annotations

import json
from pathlib import Path

from agentcostlab import providers
from agentcostlab.ledger import SHAPE_KEYS, _normalise, broke_cache


def rec(read=None, creation=0, usage=True, provider="anthropic"):
    if not usage:
        return {"provider": provider, "usage": None}
    if read is None:
        return {"provider": provider, "usage": {}}
    return {"provider": provider,
            "usage": {"input_tokens": 1, "cache_read_input_tokens": read,
                      "cache_creation_input_tokens": creation}}


# --- the original conservation-identity cases --------------------------------

def test_a_surviving_prefix_reads_back_what_was_read_plus_written():
    assert broke_cache(rec(5000, creation=800), rec(5800)) is False


def test_a_partial_break_is_a_break():
    """capture-02 record 61: 155,169 read of an expected 157,991. Non-zero, so
    a `cache_read > 0` rule calls it clean while 2,822 tokens are paid twice."""
    assert broke_cache(rec(155169, creation=2822), rec(155169)) is True


def test_a_total_break_is_a_break():
    assert broke_cache(rec(5000, creation=800), rec(0)) is True


def test_an_empty_usage_is_not_read_zero():
    """Record 46 of the 2026-08-18 capture carries `usage: {}`. Reading that as
    cache_read == 0 invents a break out of a missing measurement."""
    assert broke_cache(rec(5000, creation=800), rec(None)) is None
    assert broke_cache(rec(None), rec(5000)) is None


def test_a_missing_usage_is_unknown_not_false():
    assert broke_cache(rec(5000), rec(usage=False)) is None
    assert broke_cache(rec(usage=False), rec(5000)) is None


def test_unknown_is_distinguishable_from_no_break():
    """`None` and `False` must not be conflated by callers, so they must not be
    conflated here: this repo's recurring defect is "unknown" folded into "no"."""
    assert broke_cache(rec(None), rec(None)) is not False


# --- AC4: the three branches of the shortfall criterion ----------------------

def test_exact_readback_is_not_a_break():
    assert broke_cache(rec(5000, creation=800), rec(5800)) is False


def test_partial_shortfall_is_a_break():
    """prev.read (5000) < curr.read (5600) < expected (5800): some of the prefix
    survived, some did not."""
    assert broke_cache(rec(5000, creation=800), rec(5600)) is True


def test_read_more_than_expected_is_not_a_break():
    """capture-03 record 66: read 92,756 of an expected 92,658 — 98 *more*. The
    old `!=` called it a break, then `repaid < 0` dumped it in not_measured. A
    shortfall criterion sees no loss, so the turn is clean, not an anomaly."""
    assert broke_cache(rec(5000, creation=800), rec(5900)) is False


# --- deliverable 1: all three write buckets count toward expected ------------

def test_all_three_write_buckets_count_toward_expected():
    """`expected` is prev.cache_read + cache_write_5m + cache_write_1h +
    cache_write_unspecified. Dropping the `unspecified` remainder (the shortfall
    between the breakdown and the aggregate) would under-count expected and a
    genuine break would read as clean."""
    prev = {"provider": "anthropic",
            "usage": {"input_tokens": 1, "output_tokens": 1,
                      "cache_read_input_tokens": 5000,
                      "cache_creation_input_tokens": 350,
                      "cache_creation": {"ephemeral_5m_input_tokens": 100,
                                         "ephemeral_1h_input_tokens": 200}}}
    # 100 (5m) + 200 (1h) + 50 (unspecified) = 350; expected = 5000 + 350 = 5350
    exact = {"provider": "anthropic",
             "usage": {"input_tokens": 1, "output_tokens": 1,
                       "cache_read_input_tokens": 5350}}
    assert broke_cache(prev, exact) is False
    short = {"provider": "anthropic",
             "usage": {"input_tokens": 1, "output_tokens": 1,
                       "cache_read_input_tokens": 5349}}
    assert broke_cache(prev, short) is True


# --- the shape resolves, the label does not gate the reading -----------------

def test_a_mislabelled_shape_is_read_by_shape_not_refused():
    """provider="anthropic" but DeepSeek's native keys. The label no longer gates
    reading: the usage is DeepSeek-shaped, so it is read as DeepSeek and judged —
    both sides read 5000 of an expected 5000, so "not broken" (False), not the
    "not measured" (None) the old label gate returned."""
    deepseek_usage = {"prompt_cache_hit_tokens": 5000,
                      "prompt_cache_miss_tokens": 100,
                      "completion_tokens": 10}
    prev = {"provider": "anthropic", "usage": deepseek_usage}
    curr = {"provider": "anthropic", "usage": deepseek_usage}
    assert broke_cache(prev, curr) is False


def test_the_label_does_not_change_the_reading():
    """Shape decides how to read, so the provider label — anthropic, deepseek, or
    absent — must not change ``broke_cache``'s verdict on the same usage.

    Mutation guard: restoring label-gating fails this, because the
    deepseek-labelled Anthropic usage would be refused (None) instead of read
    (True)."""
    prev = {"cache_read_input_tokens": 5000, "cache_creation_input_tokens": 800}
    curr = {"cache_read_input_tokens": 5600}   # shortfall -> break
    anthropic = broke_cache({"provider": "anthropic", "usage": prev},
                            {"provider": "anthropic", "usage": curr})
    deepseek = broke_cache({"provider": "deepseek", "usage": prev},
                           {"provider": "deepseek", "usage": curr})
    unlabelled = broke_cache({"usage": prev}, {"usage": curr})
    assert anthropic is deepseek is unlabelled is True


def test_a_usage_with_no_known_shape_is_not_measured():
    assert broke_cache({"usage": {"foo": 1}}, {"usage": {"foo": 1}}) is None


def test_a_usage_matching_two_shapes_is_not_measured():
    """Anthropic's and DeepSeek's keys in one payload: ambiguous, refused rather
    than guessed. Guessing would read whichever extractor it picked and pass a
    misread off as a read."""
    ambiguous = {"cache_read_input_tokens": 5000, "prompt_cache_hit_tokens": 5000}
    assert broke_cache({"usage": ambiguous}, {"usage": ambiguous}) is None


# --- AC2: cross-provider consistency, driven by EXTRACTORS -------------------

def _payload(provider, read, write):
    """One usage dict in ``provider``'s native shape, encoding ``read`` cached
    tokens and ``write`` newly-written tokens. Raises for an unregistered
    provider so a new extractor without an equivalent payload fails this test
    instead of being silently skipped."""
    if provider == "anthropic":
        return {"input_tokens": 999, "output_tokens": 10,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write}
    if provider == "deepseek":
        # Implicit caching: no write is ever reported. ``prompt_tokens`` (== hit
        # + miss) is present on the real API and used to collide with OpenAI's
        # shape; it must not, now that OpenAI is keyed by prompt_tokens_details.
        return {"prompt_cache_hit_tokens": read,
                "prompt_cache_miss_tokens": 999,
                "prompt_tokens": read + 999,
                "completion_tokens": 10}
    if provider == "openai":
        return {"prompt_tokens": read + write + 999,
                "prompt_tokens_details": {"cached_tokens": read,
                                          "cache_write_tokens": write},
                "completion_tokens": 10}
    raise AssertionError(f"no equivalent payload defined for provider {provider!r}")


def _written(provider, requested):
    """The write actually reported by ``provider``: 0 for implicit cachers."""
    return 0 if provider == "deepseek" else requested


# Representative native usage per provider, matching the shapes in
# test_gates.test_usage_normalises_to_the_same_shape. DeepSeek's carries
# ``prompt_tokens`` because the native API reports it (== hit + miss) — the
# collision that forces OpenAI's shape to be ``prompt_tokens_details`` alone.
_SHAPE_SAMPLES = {
    "anthropic": {"input_tokens": 100, "cache_read_input_tokens": 900,
                  "cache_creation_input_tokens": 50, "output_tokens": 20},
    "deepseek": {"prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
                 "prompt_tokens": 1000, "completion_tokens": 20},
    "openai": {"prompt_tokens": 1000,
               "prompt_tokens_details": {"cached_tokens": 900},
               "completion_tokens": 20},
}


def test_each_provider_shape_matches_exactly_one_shape():
    """The shape registry is mutually exclusive, proven by iterating the real
    ``EXTRACTORS`` registry rather than three hand-copied cases. A new provider
    without a sample fails here (KeyError on ``_SHAPE_SAMPLES``); a provider
    whose sample matches zero or several shapes fails the resolution assertion.

    The DeepSeek sample carries ``prompt_tokens`` on purpose: that key used to be
    part of OpenAI's shape, so a DeepSeek payload would match two providers and
    be refused as ambiguous. ``prompt_tokens_details`` is the OpenAI-only key
    now, so exactly one provider matches each sample.
    """
    assert set(SHAPE_KEYS) == set(providers.EXTRACTORS), (
        "SHAPE_KEYS and EXTRACTORS have drifted; every registered provider needs "
        "a distinctive shape and every shape needs a registered extractor.")
    for provider in providers.EXTRACTORS:
        sample = _SHAPE_SAMPLES[provider]   # KeyError -> new provider, no sample
        resolved = _normalise({"usage": sample})   # no label: shape is the only input
        assert resolved is not None, (
            f"{provider}'s sample matched zero or several shapes: {sample}")
        assert resolved == providers.normalise(provider, sample), (
            f"{provider}'s sample resolved to the wrong shape: "
            f"{resolved} != {providers.normalise(provider, sample)}")


def test_cross_provider_cache_conservation_agrees():
    """The same semantic pair — read back the full prefix, read N fewer, read N
    more — expressed in every registered provider's own shape, judged alike.

    Iterates ``providers.EXTRACTORS`` so a newly registered provider is pulled
    in automatically and fails (rather than being skipped) until ``_payload``
    grows an equivalent shape for it."""
    read, write = 5000, 800
    for provider in providers.EXTRACTORS:
        written = _written(provider, write)
        prev = {"provider": provider,
                "usage": _payload(provider, read, written)}
        intact = {"provider": provider,
                  "usage": _payload(provider, read + written, written)}
        short = {"provider": provider,
                 "usage": _payload(provider, read + written - 100, written)}
        over = {"provider": provider,
                "usage": _payload(provider, read + written + 100, written)}

        assert broke_cache(prev, intact) is False, provider
        assert broke_cache(prev, short) is True, provider
        assert broke_cache(prev, over) is False, provider


# --- AC5: implicit-cache provider behaviour, on the real DeepSeek fixture ----

def _deepseek_turns():
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ledger" \
        / "deepseek_healthy_growth.json"
    return json.loads(fixture.read_text())["turns"]


def _implicit_records():
    """Fixture turns as records. The DeepSeek /anthropic compat endpoint returns
    Anthropic-shaped usage (write always 0), so the label matching the keys is
    anthropic — the fixture exists precisely because implicit caching is
    indistinguishable from anthropic at the usage level. Write=0 and a
    monotonically growing read is the AC5 shape."""
    return [{"provider": "anthropic", "usage": t["usage"]}
            for t in _deepseek_turns()]


def test_implicit_cache_growth_is_not_a_break():
    records = _implicit_records()
    verdicts = [broke_cache(records[i], records[i + 1])
                for i in range(len(records) - 1)]
    breaks = [i for i, verdict in enumerate(verdicts) if verdict is True]
    # One genuine shrink — turn 41 (read 164,352) -> turn 42 (read 23,040) —
    # and nothing else. Monotonic growth never reads as a break.
    assert breaks == [41]


def test_implicit_cache_shrink_is_a_break():
    records = _implicit_records()
    assert broke_cache(records[41], records[42]) is True


# --- the E4 cheap arm: deepseek label, Anthropic-shaped usage ----------------

def _compat_records():
    """Fixture turns labelled ``deepseek`` — the E4 cheap arm: Claude Code pointed
    at DeepSeek's ``/anthropic`` compat endpoint. The label says who bills, the
    usage shape says how to read; ``broke_cache`` must follow the shape, not the
    label, so this must judge identically to the anthropic-labelled version."""
    return [{"provider": "deepseek", "usage": t["usage"]}
            for t in _deepseek_turns()]


def test_compat_endpoint_is_measured_not_refused():
    """provider="deepseek" + Anthropic-shaped usage used to return None on all 61
    pairs (label != shape). Shape now decides how to read, so all 61 are measured
    and judged: the one genuine shrink (turn 41 -> 42) is a break, the other 60
    are clean."""
    records = _compat_records()
    verdicts = [broke_cache(records[i], records[i + 1])
                for i in range(len(records) - 1)]
    assert None not in verdicts, "the compat endpoint must be measurable"
    breaks = [i for i, verdict in enumerate(verdicts) if verdict is True]
    assert breaks == [41]


def test_compat_endpoint_shrink_is_a_break():
    records = _compat_records()
    assert broke_cache(records[41], records[42]) is True
