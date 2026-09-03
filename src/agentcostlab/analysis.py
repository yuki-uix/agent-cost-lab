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

*   ledger broke, attributor names a component      -> ``by_cause[component]``, priced
*   ledger broke, attributor returns None/suppressed -> ``unattributed``, priced
*   ledger intact, attributor reports a divergence   -> ``disputed_turns``, counted only
*   usage missing on either side                     -> ``not_measured_turns``

``unattributed`` is the honest core: money that demonstrably was spent twice,
with no story for why. Folding it into a cause, or dropping it, inflates a
"62% of the money leaked in the system prompt" headline.

Bytes and tokens are two different quantities this repo has never calibrated
against each other, so this module reports tokens and dollars and never bytes.
"""
from __future__ import annotations

from dataclasses import dataclass

from .attribute import COMPONENTS, attribute
from .ledger import broke_cache
from .pricing import Rate, UnverifiedRate, cost, load_rates
from .providers import Usage, normalise

_PER_MTOK = 1_000_000


@dataclass(frozen=True)
class CauseCost:
    cause: str              # "system" | "tools" | "messages" | "model" | "params"
    turns: int              # turns whose cache broke for this cause
    repaid_tokens: int      # tokens paid twice, from the conservation identity
    usd_low: float          # all repaid tokens landing in the cheapest bucket seen
    usd_high: float         # all repaid tokens landing in the most expensive bucket seen


@dataclass(frozen=True)
class CauseBreakdown:
    by_cause: list[CauseCost]
    unattributed: CauseCost      # ledger broke, attributor could not say why
    disputed_turns: int          # attributor saw a break, ledger did not (counted only)
    not_measured_turns: int      # usage missing, or repaid < 0 (instrument anomaly)
    hit_usd_saved: float         # money cache hits saved, reported apart from losses


def _pair_by_previous(records: list[dict]) -> list[tuple[dict, dict]]:
    """Pair each record with the actual previous message it names.

    A capture interleaves sub-conversations on adjacent lines, so "previous
    line" is not the previous message. ``injected_previous_message_id`` is: it
    names the exact ``response_id`` whose request the cache was compared against.
    """
    by_id = {r.get("response_id"): r for r in records if r.get("response_id")}
    pairs: list[tuple[dict, dict]] = []
    for curr in records:
        ipm = curr.get("injected_previous_message_id")
        if not ipm:
            continue  # first turn of a conversation: nothing to compare
        prev = by_id.get(ipm)
        if prev is None:
            continue  # previous message not in this capture
        pairs.append((prev, curr))
    return pairs


def _repaid(prev: dict, curr: dict) -> int:
    """Tokens paid twice, from the same keys ``broke_cache`` reasons over."""
    prev_usage = prev.get("usage") or {}
    curr_usage = curr.get("usage") or {}
    expected = (prev_usage.get("cache_read_input_tokens", 0)
                + prev_usage.get("cache_creation_input_tokens", 0))
    return expected - curr_usage.get("cache_read_input_tokens", 0)


def _verified_rate(rates: dict[str, Rate], model_key: str) -> Rate:
    """Return the verified rate for ``model_key``, refusing an unverified one.

    Mirrors the two checks at the top of ``pricing.cost`` (unknown key, unverified
    entry) so this module refuses to publish a number exactly where ``cost`` does.
    """
    try:
        rate = rates[model_key]
    except KeyError as exc:
        raise KeyError(
            f"no rate entry for {model_key!r}; known: {sorted(rates)}"
        ) from exc
    if not rate.verified:
        raise UnverifiedRate(
            f"rate for {model_key!r} is unverified (source: {rate.source}). "
            "Check the official pricing page, fill in the numbers, then set "
            '"verified": true.'
        )
    return rate


def _loss_interval(
    repaid: int, usage: Usage, rate: Rate, model_key: str, rates: dict[str, Rate]
) -> tuple[float, float]:
    """Bounds on the extra cost of ``repaid`` tokens re-billed after a break.

    The re-billed tokens land in one of the non-cache-read buckets, but usage
    only reports each bucket's total, not the split. Sweep the free parameter:
    low = every repaid token in the cheapest bucket that actually carried tokens
    this turn, high = every repaid token in the most expensive one.

    ``cost`` is called first so an unpriceable write (no TTL, tiers differ)
    refuses here exactly as it does everywhere else, rather than silently
    guessing. The loss is the bucket rate minus the cache-read rate the tokens
    would have paid had the prefix survived.
    """
    cost(usage, model_key, rates)
    buckets: list[float] = []
    if usage.input_uncached:
        buckets.append(rate.input_uncached)
    if usage.cache_write_5m:
        buckets.append(rate.cache_write_5m)
    if usage.cache_write_1h:
        buckets.append(rate.cache_write_1h)
    if usage.cache_write_unspecified:
        # ``cost`` above raises when the tiers differ; reaching here means they
        # are equal, so either tier's rate is the price.
        buckets.append(rate.cache_write_5m)
    if not buckets:
        raise ValueError(
            f"{repaid} repaid tokens but no non-cache-read bucket in this turn's "
            f"usage ({usage}) to bill them to; refusing to guess."
        )
    low, high = min(buckets), max(buckets)
    return (
        repaid * (low - rate.cache_read) / _PER_MTOK,
        repaid * (high - rate.cache_read) / _PER_MTOK,
    )


def cost_by_cause(records: list[dict], model_key: str) -> CauseBreakdown:
    """Attribute every cache break to a cause and price it as an interval.

    ``records`` are capture records as written by the proxy (one JSON object per
    line). ``model_key`` is a pricing key such as ``anthropic/claude-sonnet-5``.
    """
    rates = load_rates()
    rate = _verified_rate(rates, model_key)

    cause_state = {c: {"turns": 0, "repaid": 0, "low": 0.0, "high": 0.0}
                   for c in COMPONENTS}
    unattributed = {"turns": 0, "repaid": 0, "low": 0.0, "high": 0.0}
    disputed = 0
    not_measured = 0

    # Money cache hits saved, over every turn that read from cache. Reported
    # apart from the losses: savings and losses are different signposts, and
    # netting them hides which one moved.
    hit_saved = 0.0
    for record in records:
        usage = normalise(record.get("provider"), record.get("usage"))
        hit_saved += usage.cache_read * (rate.input_uncached - rate.cache_read) / _PER_MTOK

    for prev, curr in _pair_by_previous(records):
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
        low, high = _loss_interval(repaid, usage, rate, model_key, rates)

        bucket = unattributed if (d is None or d.suppressed) else cause_state[d.component]
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
