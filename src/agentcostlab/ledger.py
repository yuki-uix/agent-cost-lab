"""Whether a turn kept the cache, judged only by the usage the API billed.

One definition, because this is load-bearing in three places — the calibration
script's ground truth and two attributor regression tests — and it was written
out longhand in all three. Three copies of a concept this central drift, and
when they do, a test validates the attributor against a different rule than the
script reports, with nobody the wiser.

Independent of `attribute` on purpose: it is the oracle the attributor is
checked against, so it must not share the reasoning it is checking.
"""
from __future__ import annotations


def broke_cache(prev: dict, curr: dict) -> bool | None:
    """Did ``curr`` lose cached tokens relative to ``prev``? ``None`` if unknown.

    A surviving prefix satisfies::

        curr.cache_read == prev.cache_read + prev.cache_creation

    Everything the previous turn read, plus everything it wrote, is what this
    turn should read back.

    Not ``cache_read == 0``. That rule cannot see a **partial** break, and the
    only break either capture contains is partial: capture-02 record 61 read
    155,169 of an expected 157,991 — non-zero, so the older rule called it clean
    while 2,822 tokens were being paid for a second time.

    An absent or empty ``usage`` is "not measured", never "read zero". Record 46
    of the 2026-08-18 capture carries ``usage: {}`` and is not a cache break; a
    probe that read it as one invented a witness out of a missing measurement.
    """
    usage, prev_usage = curr.get("usage"), prev.get("usage")
    if not usage or not prev_usage:
        return None
    expected = (prev_usage.get("cache_read_input_tokens", 0)
                + prev_usage.get("cache_creation_input_tokens", 0))
    return usage.get("cache_read_input_tokens", 0) != expected
