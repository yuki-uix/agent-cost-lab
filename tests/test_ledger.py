"""The oracle the attributor is checked against needs checking itself.

It was written out longhand in three places before it became a module; each of
these cases is one the longhand version got wrong or would have.
"""
from __future__ import annotations

from agentcostlab.ledger import broke_cache


def rec(read=None, creation=0, usage=True):
    if not usage:
        return {"usage": None}
    if read is None:
        return {"usage": {}}
    return {"usage": {"input_tokens": 1, "cache_read_input_tokens": read,
                      "cache_creation_input_tokens": creation}}


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
