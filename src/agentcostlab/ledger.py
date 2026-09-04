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

from dataclasses import dataclass, fields

from .providers import EXTRACTORS, Usage, normalise

# The keys that identify each provider's usage shape. They must be mutually
# exclusive: ``_normalise`` resolves a usage by which provider's keys it carries,
# so a payload carrying two providers' keys has an ambiguous shape and is refused
# rather than guessed. ``normalise`` reads exactly these keys, so a usage dict
# carrying none of them has no cache signal for any provider and is refused as
# "not measured" rather than read as "not broken".
#
# OpenAI is keyed by ``prompt_tokens_details`` alone, not ``prompt_tokens``:
# DeepSeek's native usage also reports ``prompt_tokens`` (it equals hit + miss),
# so ``prompt_tokens`` cannot tell the two apart. ``prompt_tokens_details`` is
# OpenAI-only.
# Each provider's identifying keys, and what a *readable* one has to be. The
# kind is not decoration: a key that says "this record is OpenAI" while holding
# `null` identifies a shape it cannot supply, and `_openai` then reads
# `(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0` as a
# confident cached=0. Identification and readability were separate mechanisms
# and nothing connected them; declaring the kind here is what joins them.
COUNT, OBJECT = "count", "object"


@dataclass(frozen=True)
class Key:
    """What an identifying key has to hold to be readable.

    `required` is what an OBJECT must actually carry. An empty container passes
    an `isinstance(dict)` test and then reads as a confident zero, which cannot
    be told apart from "the provider did not report it" — so the counts the
    extractor pulls out of it are named here, and a container missing them is
    not measurable.
    """

    kind: str
    required: tuple[str, ...] = ()


SHAPE_KEYS: dict[str, dict[str, Key]] = {
    "anthropic": {"cache_read_input_tokens": Key(COUNT),
                  "cache_creation_input_tokens": Key(COUNT)},
    "deepseek": {"prompt_cache_hit_tokens": Key(COUNT),
                 "prompt_cache_miss_tokens": Key(COUNT)},
    "openai": {"prompt_tokens_details": Key(OBJECT, ("cached_tokens",))},
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
    The same goes for a usage that matches no provider's shape, or several: it
    has no unambiguous cache signal, so it is "not measured" rather than read as
    "not broken". The provider label does not enter this decision — the shape
    alone says how to read the numbers, and which provider's rates to charge is
    ``cost_by_cause``'s separate question.
    """
    prev_usage = _normalise(prev)
    curr_usage = _normalise(curr)
    if prev_usage is None or curr_usage is None:
        return None
    expected = prev_usage.cache_read + prev_usage.cache_write
    return curr_usage.cache_read < expected


def _normalise(record: dict) -> Usage | None:
    """``record``'s usage as a ``Usage``, or ``None`` when it is not measurable.

    Resolves the usage by its *shape*, not its provider label. Each registered
    provider owns a distinctive set of keys; the usage is read with the one
    provider whose shape it matches. Exactly one match -> read it; zero matches
    -> no cache signal, not measured; several matches -> ambiguous shape, not
    measured (the shape registry is meant to be mutually exclusive, and guessing
    which colliding provider to read would pass a misread off as a read).

    The label is irrelevant here: it decides which provider's rates to charge
    (``cost_by_cause``), not which keys to read.
    """
    usage = record.get("usage")
    # `usage` comes from JSON and is not guaranteed to be an object. `key in x`
    # is a *substring* test on a string, so a bare `usage: "cache_read_input_
    # tokens"` would match the anthropic shape and then blow up inside
    # `normalise`. Refusing here keeps the documented contract: unmeasurable is
    # None, never an exception the callers have to know about.
    if not isinstance(usage, dict) or not usage:
        return None
    matches = [provider for provider in EXTRACTORS
               if any(key in usage for key in SHAPE_KEYS.get(provider, {}))]
    if len(matches) != 1:
        return None
    if not _identifying_keys_are_readable(usage, SHAPE_KEYS[matches[0]]):
        return None
    if not _raw_counts_are_sane(usage):
        return None
    try:
        counted = normalise(matches[0], usage)
    except (AttributeError, TypeError, ValueError):
        return None
    return counted if _counts_are_sane(counted) else None


def _identifying_keys_are_readable(usage: dict, shape: dict[str, str]) -> bool:
    """A key that identifies this shape must, if present, hold what it claims.

    Shape matching is key *presence*, so `prompt_tokens_details: false` both
    names the record OpenAI and gives `_openai` nothing to read — it falls back
    to `or {}` and reports cached=0, which against a previous turn that cached
    1,000 tokens is a total break the record never had. The same hole the falsy
    *counts* had, one level up, in the container that decides the shape.

    Only *identifying* keys are held to this. A non-identifying container may
    legitimately be falsy: anthropic's `cache_creation` absent means "no TTL
    breakdown", and `_anthropic` routes the write to `cache_write_unspecified`,
    which `cost` then refuses to price rather than guessing a tier. That is
    graceful degradation the extractor documents; this is a shape claiming to be
    something it is not.
    """
    for key, spec in shape.items():
        if key not in usage:
            continue
        value = usage[key]
        if spec.kind == OBJECT:
            if not isinstance(value, dict):
                return False
            if not all(_is_count(value.get(name)) for name in spec.required):
                return False
        elif not _is_count(value):
            return False
    return True


def _is_count(value: object) -> bool:
    """A token count: a non-negative whole number. `bool` is not one."""
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _raw_counts_are_sane(usage: dict) -> bool:
    """Every token count *present* in the raw payload is a non-negative whole number.

    This has to run **before** `normalise`, because the extractors read counts as
    ``u.get(key, 0) or 0`` — which cannot tell "absent" from "present but falsy".
    An explicit ``null``, ``false``, ``0.0``, ``""``, ``[]`` or ``{}`` is coerced
    to 0 and then passes a post-normalisation check as a perfectly ordinary zero.
    That is not academic: against a previous turn that read 1,000 tokens, any of
    those values reads as a *total* break the capture never had — malformed data
    inventing a witness, which is worse than the crashes this guard replaced.

    Absent is still fine and still means 0: a provider that omits a field did not
    report it, and 0 is the honest reading. It is the *explicit* non-integer that
    is malformed — DeepSeek, for one, documents both cache token fields as
    required integers rather than nullable.

    The rule keys on the name: every count the extractors read is spelled
    ``*_tokens`` (twelve of them across the three providers, nested ones
    included), while the non-count fields around them — ``service_tier``,
    ``cache_creation``, ``prompt_tokens_details`` — are not. A count added under
    some other name would slip past; `test_every_count_key_is_named_tokens` keeps
    that assumption visible rather than tacit.
    """
    for key, value in usage.items():
        # The suffix decides first: a `*_tokens` key holding an object is
        # malformed data, not a container to descend into. Checking `isinstance
        # dict` first let `cache_read_input_tokens: {}` recurse into an empty
        # dict, come back "sane", and then normalise to 0.
        if key.endswith("_tokens"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
        elif isinstance(value, dict):
            if not _raw_counts_are_sane(value):
                return False
    return True


def _counts_are_sane(usage: Usage) -> bool:
    """Every token count is a non-negative whole number.

    A count that is a string, a nested object, or negative is malformed data, not
    a measurement. Left unchecked it either raises inside the arithmetic (a
    string count crashes `expected + ...`) or, worse, sails through: a
    `cache_read_input_tokens: -5` used to read as a perfectly ordinary "not
    broken", which is the silent misread this module is built to refuse.

    Driven by the dataclass fields rather than a hand-written list, so a count
    added to `Usage` later is covered without anyone remembering to come back.
    `bool` is excluded deliberately — it is a subclass of `int`, and `True` is
    not a token count.

    A key present with an explicit `null` is *not* refused here: `providers.
    normalise` coerces it to 0, and a provider reporting no cached tokens for a
    turn where caching did not apply is reading zero, not failing to measure.
    Overriding that in the ledger alone would leave it calling a turn
    unmeasurable while `pricing.cost` happily bills it at $0. Considered and
    left alone, not overlooked.
    """
    for field in fields(usage):
        if field.type in ("int", int):
            value = getattr(usage, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
    return True
