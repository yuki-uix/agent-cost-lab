"""The missing bridge: from "why did the cache break" to "what did that cost".

The repo has two halves that never meet. `attribute.attribute(prev, curr)` says
*why* the prefix diverged — component, detail, path, byte counts. `pricing.cost`
says *what one call cost* — a dollar figure from a rate table. Nothing says
"because `system` changed, this conversation paid $X more than it had to". This
module is that bridge.

The arithmetic is a *conservation identity*, not an estimate. `ledger.broke_cache`
already fixes that the expected cache read is the previous turn's read plus its
write::

    expected = prev.cache_read + prev.cache_creation
    broke    ⟺  curr.cache_read != expected

so the number of tokens paid for a second time is ``repaid = expected -
curr.cache_read``. That number comes straight from provider-reported usage; no
tokenizer, no byte/4 guess, no `Divergence.bytes_*` inference.

Where those `repaid` tokens land on re-billing is the one free parameter. They
go into `input_uncached`, `cache_write_5m`, or `cache_write_1h` — three buckets
whose rates span nearly 2x on Anthropic — and the usage reports only each
bucket's *total*, not how many of the repaid tokens went to which. Per the
repo's rule on free parameters, we sweep it and report an interval
(`usd_low` / `usd_high`), never a point estimate. The sweep only ranges over
buckets that actually carried tokens that turn: a turn with no 1h write may not
take 2x as its upper bound.

The ledger and the attributor are independent judgements on purpose — the ledger
is the oracle the attributor is checked against, so it must not share the
attributor's reasoning. When they disagree, the disagreement is a fact to
report, not a loose end to paper over:

*   ledger broke, attributor names one component     -> ``by_cause[component]``, priced
*   ledger broke, ≥2 components diverged             -> ``ambiguous``, priced, candidates listed
*   ledger broke, attributor returns None/suppressed -> ``unattributed``, priced
*   ledger intact, attributor reports a divergence   -> ``disputed_turns``, counted only
*   usage missing on either side                     -> ``not_measured_turns``

``unattributed`` is the honest core: money that demonstrably was spent twice,
with no story for why. Folding it into a cause, or dropping it, inflates a
"62% of the money leaked in the system prompt" headline.

Bytes and tokens are two different quantities this repo has never calibrated
against each other, so this module reports tokens and dollars and never bytes.

The conservation identity (``broke_cache`` / ``_repaid``) reads Anthropic's raw
usage keys (``cache_read_input_tokens`` / ``cache_creation_input_tokens``), so
it is only defined for Anthropic's usage shape. As a temporary limitation this
module refuses any non-Anthropic record rather than silently misreading a
DeepSeek / OpenAI payload as "not broken" (both sides would read 0). Making the
identity itself provider-neutral is a separate task — it lives in ``ledger``
and its three call sites, so it is deliberately *not* re-derived here, which
would be a fourth copy of the same rule.
"""
from __future__ import annotations

from dataclasses import dataclass

from .attribute import COMPONENTS, _canonical_candidates, attribute
from .ledger import broke_cache
from .pricing import Rate, UnverifiedRate, cost, load_rates
from .providers import Usage, normalise

_PER_MTOK = 1_000_000


class RepaidExceedsCapacity(ValueError):
    """``repaid`` tokens exceed what the turn's non-cache-read buckets carried."""


class RecordRateMismatch(ValueError):
    """A record's provider/model has no rate-table entry, so it cannot be priced."""


class MissingModel(ValueError):
    """A record has no ``model`` in its ``request_body``; refusing to guess a rate."""


class NonAnthropicProvider(ValueError):
    """The conservation identity is only defined for Anthropic's usage shape."""


@dataclass(frozen=True)
class CauseCost:
    cause: str              # "system" | "tools" | "messages" | "model" | "params" | "ambiguous" | "unattributed"
    turns: int              # turns whose cache broke for this cause
    repaid_tokens: int      # tokens paid twice, from the conservation identity
    usd_low: float          # all repaid tokens landing in the cheapest bucket seen
    usd_high: float         # all repaid tokens landing in the most expensive bucket seen
    candidates: tuple[str, ...] = ()  # set only for `ambiguous`: the ≥2 component names


@dataclass(frozen=True)
class CauseBreakdown:
    by_cause: list[CauseCost]
    ambiguous: CauseCost        # ledger broke, but ≥2 components diverged: no single cause
    unattributed: CauseCost      # ledger broke, attributor could not say why
    disputed_turns: int          # attributor saw a break, ledger did not (counted only)
    not_measured_turns: int      # usage missing, or repaid < 0 (instrument anomaly)
    hit_usd_saved: float         # money cache hits saved, reported apart from losses


def _pair_by_previous(records: list[dict]) -> list[tuple[dict, dict, int]]:
    """Pair each record with the actual previous message it names.

    A capture interleaves sub-conversations on adjacent lines, so "previous
    line" is not the previous message. ``injected_previous_message_id`` is: it
    names the exact ``response_id`` whose request the cache was compared against.
    The trailing index is the ``curr`` record's position in ``records``, so the
    caller can fetch that record's per-model rate without re-resolving it.
    """
    by_id = {r.get("response_id"): r for r in records if r.get("response_id")}
    pairs: list[tuple[dict, dict, int]] = []
    for i, curr in enumerate(records):
        ipm = curr.get("injected_previous_message_id")
        if not ipm:
            continue  # first turn of a conversation: nothing to compare
        prev = by_id.get(ipm)
        if prev is None:
            continue  # previous message not in this capture
        pairs.append((prev, curr, i))
    return pairs


def _repaid(prev: dict, curr: dict) -> int:
    """Tokens paid twice, from the same keys ``broke_cache`` reasons over."""
    prev_usage = prev.get("usage") or {}
    curr_usage = curr.get("usage") or {}
    expected = (prev_usage.get("cache_read_input_tokens", 0)
                + prev_usage.get("cache_creation_input_tokens", 0))
    return expected - curr_usage.get("cache_read_input_tokens", 0)


def _rate_for(record: dict, index: int, rates: dict[str, Rate]) -> tuple[str, Rate]:
    """Resolve the pricing key and verified rate for one record.

    Each record is priced at its own ``provider`` + ``request_body["model"]``, so
    a capture may mix models and its dollars are per-record sums — never one
    average rate over merged tokens. The gate's criterion is now "resolvable and
    verified" rather than "matches a caller-supplied key"; it refuses in the same
    two places ``pricing.cost`` does (unknown entry, unverified entry) plus the
    two things ``cost`` cannot see: a missing ``model`` and a non-Anthropic
    provider.

    Returns ``(key, rate)`` so the caller can hand the same key straight to
    ``cost`` / ``_loss_interval`` without re-deriving it.
    """
    provider = record.get("provider")
    body = record.get("request_body") or {}
    model = body.get("model")

    # The conservation identity reads Anthropic's usage keys, so a DeepSeek /
    # OpenAI record would read 0 on both sides and a real break would be judged
    # "not broken". Refuse before looking at the rate table: this module cannot
    # price a non-Anthropic record even when its rate is present and verified.
    if provider != "anthropic":
        raise NonAnthropicProvider(
            f"record {index} is from provider {provider!r}; the cache-break "
            f"conservation identity (cache_read_input_tokens / "
            f"cache_creation_input_tokens) is currently only defined for "
            f"Anthropic's usage shape, so non-Anthropic providers are refused "
            f"rather than silently misread. Making the identity provider-neutral "
            f"is a separate task touching ledger and its three call sites."
        )

    if model is None:
        raise MissingModel(
            f"record {index} has no 'model' in its request_body; refusing to "
            f"guess a rate. A record without a model cannot be priced, and "
            f"guessing the most common model would silently bill a possibly "
            f"different model at the wrong rate."
        )

    key = f"{provider}/{model}"
    try:
        rate = rates[key]
    except KeyError as exc:
        raise RecordRateMismatch(
            f"record {index} has provider {provider!r} and model {model!r} "
            f"({key!r}), which is not in the rate table; known: {sorted(rates)}. "
            f"Add the entry to fixtures/pricing.json and set verified: true "
            f"before this capture can be priced."
        ) from exc
    if not rate.verified:
        raise UnverifiedRate(
            f"rate for {key!r} is unverified (source: {rate.source}). "
            "Check the official pricing page, fill in the numbers, then set "
            '"verified": true.'
        )
    return key, rate


def _loss_interval(
    repaid: int, usage: Usage, rate: Rate, model_key: str, rates: dict[str, Rate]
) -> tuple[float, float]:
    """Bounds on the extra cost of ``repaid`` tokens re-billed after a break.

    The re-billed tokens land in one of the non-cache-read buckets, and usage
    reports each bucket's *total*, not the split. Two constraints fix the sweep:

    *   only buckets that actually carried tokens this turn may take any repaid
        token — a turn with no 1h write may not use the 2x rate as its bound;
    *   a bucket can hold at most its own reported token count, so the repaid
        tokens are packed greedily — most expensive first for ``usd_high``,
        cheapest first for ``usd_low`` — spilling the remainder into the next
        bucket in rate order.

    If the buckets' total capacity is less than ``repaid``, those tokens cannot
    all have been re-billed this turn and the conservation identity's premise is
    broken: refuse rather than scale, clamp, or guess.

    ``cost`` is called first so an unpriceable write (no TTL, tiers differ)
    refuses here exactly as it does everywhere else, rather than silently
    guessing. The loss per token is the bucket rate minus the cache-read rate
    the token would have paid had the prefix survived.
    """
    cost(usage, model_key, rates)
    buckets: list[tuple[int, float]] = []
    if usage.input_uncached:
        buckets.append((usage.input_uncached, rate.input_uncached))
    if usage.cache_write_5m:
        buckets.append((usage.cache_write_5m, rate.cache_write_5m))
    if usage.cache_write_1h:
        buckets.append((usage.cache_write_1h, rate.cache_write_1h))
    if usage.cache_write_unspecified:
        # ``cost`` above raises when the tiers differ; reaching here means they
        # are equal, so either tier's rate is the price.
        buckets.append((usage.cache_write_unspecified, rate.cache_write_5m))
    total_capacity = sum(capacity for capacity, _ in buckets)
    if total_capacity < repaid:
        raise RepaidExceedsCapacity(
            f"{repaid} repaid tokens, but this turn's non-cache-read buckets "
            f"carried {total_capacity} tokens in total; the conservation "
            f"identity's premise does not hold, so refusing to scale, clamp, "
            f"or guess."
        )
    low = _pack(repaid, sorted(buckets, key=lambda b: b[1]), rate.cache_read)
    high = _pack(repaid, sorted(buckets, key=lambda b: b[1], reverse=True), rate.cache_read)
    return low, high


def _pack(repaid: int, buckets: list[tuple[int, float]], cache_read_rate: float) -> float:
    """USD loss of packing ``repaid`` tokens into ``buckets`` in the given order.

    Each bucket takes up to its capacity; the remainder spills into the next.
    """
    remaining = repaid
    subtotal = 0.0
    for capacity, bucket_rate in buckets:
        if remaining <= 0:
            break
        filled = min(capacity, remaining)
        subtotal += filled * (bucket_rate - cache_read_rate)
        remaining -= filled
    return subtotal / _PER_MTOK


def cost_by_cause(records: list[dict]) -> CauseBreakdown:
    """Attribute every cache break to a cause and price it as an interval.

    ``records`` are capture records as written by the proxy (one JSON object per
    line). Each record is priced at its own ``provider`` + ``request_body["model"]``
    rate, so a mixed-model capture's dollars are the per-record sums — never one
    average rate over merged tokens.
    """
    rates = load_rates()
    # Resolve every record's rate before any arithmetic. The refusal (unknown
    # provider/model, unverified rate, missing model, non-Anthropic provider)
    # fires up front, naming the record, rather than after half the numbers are
    # computed — a capture that cannot be priced must not be half-priced.
    resolved = [_rate_for(record, i, rates) for i, record in enumerate(records)]
    keys = [key for key, _ in resolved]
    record_rates = [rate for _, rate in resolved]

    cause_state = {c: {"turns": 0, "repaid": 0, "low": 0.0, "high": 0.0}
                   for c in COMPONENTS}
    ambiguous = {"turns": 0, "repaid": 0, "low": 0.0, "high": 0.0, "candidates": set()}
    unattributed = {"turns": 0, "repaid": 0, "low": 0.0, "high": 0.0}
    disputed = 0
    not_measured = 0

    # Money cache hits saved, over every turn that read from cache. Reported
    # apart from the losses: savings and losses are different signposts, and
    # netting them hides which one moved. Each record uses its own rate, so a
    # mixed-model capture credits each model its own read discount.
    hit_saved = 0.0
    for record, rate in zip(records, record_rates):
        usage = normalise(record.get("provider"), record.get("usage"))
        hit_saved += usage.cache_read * (rate.input_uncached - rate.cache_read) / _PER_MTOK

    for prev, curr, curr_index in _pair_by_previous(records):
        broke = broke_cache(prev, curr)
        if broke is None:
            not_measured += 1
            continue
        if broke is False:
            d = attribute(prev.get("request_body") or {}, curr.get("request_body") or {})
            if d is not None and not d.suppressed:
                disputed += 1
            continue

        repaid = _repaid(prev, curr)
        if repaid < 0:
            # Read more than expected is an instrument anomaly, not a negative
            # loss. Never clamp it to zero and pass it off as clean.
            not_measured += 1
            continue

        d = attribute(prev.get("request_body") or {}, curr.get("request_body") or {})
        usage = normalise(curr.get("provider"), curr.get("usage"))
        low, high = _loss_interval(repaid, usage, record_rates[curr_index],
                                   keys[curr_index], rates)

        if d is None or d.suppressed:
            bucket = unattributed
        elif d.order_stability < 1.0:
            # Several components diverged: the first one is an artifact of the
            # swept segment order, so the turn cannot be charged to any single
            # cause. Priced, but only against the candidate list, never a name.
            bucket = ambiguous
            ambiguous["candidates"].update(d.candidates)
        else:
            bucket = cause_state[d.component]
        bucket["turns"] += 1
        bucket["repaid"] += repaid
        bucket["low"] += low
        bucket["high"] += high

    by_cause = [
        CauseCost(cause=c, turns=s["turns"], repaid_tokens=s["repaid"],
                  usd_low=s["low"], usd_high=s["high"])
        for c in COMPONENTS
        if (s := cause_state[c])["turns"]
    ]
    return CauseBreakdown(
        by_cause=by_cause,
        ambiguous=CauseCost(
            cause="ambiguous",
            turns=ambiguous["turns"],
            repaid_tokens=ambiguous["repaid"],
            usd_low=ambiguous["low"],
            usd_high=ambiguous["high"],
            candidates=_canonical_candidates(ambiguous["candidates"]),
        ),
        unattributed=CauseCost(
            cause="unattributed",
            turns=unattributed["turns"],
            repaid_tokens=unattributed["repaid"],
            usd_low=unattributed["low"],
            usd_high=unattributed["high"],
        ),
        disputed_turns=disputed,
        not_measured_turns=not_measured,
        hit_usd_saved=hit_saved,
    )
