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

from .providers import EXTRACTORS, Usage, normalise

# The keys that identify each provider's usage shape. They are disjoint on
# purpose: a payload that ``normalise`` would read as all-zeros — because its
# keys belong to a *different* provider than the label claims — is refused as
# "not measured" rather than read as "not broken". ``normalise`` reads exactly
# these keys, so a usage dict carrying none of them has no cache signal for that
# provider.
SHAPE_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("cache_read_input_tokens", "cache_creation_input_tokens"),
    "deepseek": ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"),
    "openai": ("prompt_tokens", "prompt_tokens_details"),
}


def broke_cache(prev: dict, curr: dict) -> bool | None:
    """Did ``curr`` read back *fewer* cached tokens than ``prev`` made available?

    ``None`` if unknown.

    A surviving prefix satisfies::

        curr.cache_read >= prev.cache_read + prev.cache_write

    Everything the previous turn read, plus everything it wrote, is available to
    read this turn. Reading strictly *less* than that means some of it was
    evicted or invalidated — the cache broke.

    The test is a *shortfall*, not an inequality. Reading *more* than expected is
    not a break: on an implicit-cache provider the write side is never reported,
    so the write term is 0 and the identity under-counts what was actually
    cached; a growing prefix then reads "more than expected" on every healthy
    turn, and only a shrinkage is a break. The flip side is the honest limit of
    the identity — with no write reported we cannot know how much *should* have
    been cached, so this rule detects shrinkage but not under-growth on such a
    provider. E4's injected faults all shrink the front of the prefix, so they
    register regardless.

    Not ``cache_read == 0``. That rule cannot see a **partial** break, and the
    only break either capture contains is partial: capture-02 record 61 read
    155,169 of an expected 157,991 — non-zero, so the older rule called it clean
    while 2,822 tokens were being paid for a second time.

    An absent or empty ``usage`` is "not measured", never "read zero". Record 46
    of the 2026-08-18 capture carries ``usage: {}`` and is not a cache break; a
    probe that read it as one invented a witness out of a missing measurement.
    The same goes for a usage whose shape does not match its provider label: a
    ``provider="anthropic"`` record whose keys are DeepSeek's would normalise to
    all zeros, reading as "not broken" when it is simply not measurable.
    """
    prev_usage = _normalise(prev)
    curr_usage = _normalise(curr)
    if prev_usage is None or curr_usage is None:
        return None
    expected = prev_usage.cache_read + prev_usage.cache_write
    return curr_usage.cache_read < expected


def _normalise(record: dict) -> Usage | None:
    """``record``'s usage as a ``Usage``, or ``None`` when it is not measurable.

    Guards the usage's *shape*, not the provider label. A record labelled
    ``anthropic`` whose usage keys belong to another provider is a silent misread
    if normalised as anthropic (both cache fields would read 0), so it is refused
    as not-measured rather than read as "not broken".
    """
    usage = record.get("usage")
    if not usage:
        return None
    provider = record.get("provider") or "anthropic"
    if provider not in EXTRACTORS:
        return None
    if not any(key in usage for key in SHAPE_KEYS.get(provider, ())):
        return None
    return normalise(provider, usage)
