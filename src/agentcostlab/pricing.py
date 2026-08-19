"""Cost computation. Refuses to run on unverified rates.

Every published number in this repo must be traceable to an official pricing
page. The rate table therefore carries `source` and `retrieved_at` per entry,
and `cost()` raises unless the entry has been verified by a human.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .providers import Usage

RATES_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "pricing.json"


class UnverifiedRate(RuntimeError):
    """The rate entry has not been checked against an official pricing page."""


class AmbiguousCacheWrite(RuntimeError):
    """Cache-write tokens arrived with no TTL, and the two TTLs cost differently."""


@dataclass(frozen=True)
class Rate:
    """USD per million tokens."""

    input_uncached: float
    cache_read: float
    # Anthropic prices these differently — 1.25x base input for the 5-minute
    # TTL, 2x for the 1-hour one. One field could not express the price list.
    cache_write_5m: float
    cache_write_1h: float
    output: float
    source: str
    retrieved_at: str
    verified: bool

    def age_days(self, today: date | None = None) -> int:
        return ((today or date.today()) - date.fromisoformat(self.retrieved_at)).days


def load_rates(path: Path | None = None) -> dict[str, Rate]:
    raw = json.loads((path or RATES_PATH).read_text())
    # Keys starting with "_" are notes for humans, not rate entries.
    return {key: Rate(**value) for key, value in raw.items() if not key.startswith("_")}


def cost(usage: Usage, model_key: str, rates: dict[str, Rate] | None = None) -> float:
    """USD for one API call.

    Note the shape difference between providers: DeepSeek and OpenAI cache
    automatically and bill no write premium, so `cache_write` is 0 there and the
    term vanishes. Anthropic bills writes above list price. One formula, but the
    rate table is what makes it correct per provider.
    """
    table = rates if rates is not None else load_rates()
    try:
        rate = table[model_key]
    except KeyError as exc:
        raise KeyError(
            f"no rate entry for {model_key!r}; known: {sorted(table)}"
        ) from exc

    if not rate.verified:
        raise UnverifiedRate(
            f"rate for {model_key!r} is unverified (source: {rate.source}). "
            "Check the official pricing page, fill in the numbers, then set "
            '"verified": true.'
        )

    # A write with no TTL cannot be priced when the tiers differ. Refusing beats
    # silently choosing the cheaper one: the two are 60% apart on Anthropic, and
    # a quiet 37.5% understatement is the kind of number this repo exists to
    # stop publishing. Where a provider prices both tiers the same (no TTL
    # concept), there is nothing to be ambiguous about and this does not fire.
    if usage.cache_write_unspecified and rate.cache_write_5m != rate.cache_write_1h:
        raise AmbiguousCacheWrite(
            f"{usage.cache_write_unspecified} cache-write tokens arrived without a "
            f"TTL breakdown, and {model_key!r} prices 5m at {rate.cache_write_5m} "
            f"and 1h at {rate.cache_write_1h} per MTok. Refusing to guess. The "
            "Anthropic payload carries the split in usage.cache_creation; if this "
            "capture predates that, the pair cannot be priced."
        )

    per_token = 1_000_000
    unspecified_rate = rate.cache_write_5m  # equal to 1h here, or we raised above
    return (
        usage.input_uncached * rate.input_uncached
        + usage.cache_read * rate.cache_read
        + usage.cache_write_5m * rate.cache_write_5m
        + usage.cache_write_1h * rate.cache_write_1h
        + usage.cache_write_unspecified * unspecified_rate
        + usage.output * rate.output
    ) / per_token
