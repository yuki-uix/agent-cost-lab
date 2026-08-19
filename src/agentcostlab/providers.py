"""Normalisation gate: every provider's usage payload becomes the same 4-tuple.

Every cost number in this repo passes through `normalise()`. Adding a provider
without registering its extractor *and* its pricing makes the test suite fail —
coverage is enforced by code, not by a checklist.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Usage:
    """Provider-neutral token accounting.

    The split matters: `cache_read` is ~10x cheaper than `input_uncached` on
    Anthropic and DeepSeek, so a total input-token count is not a cost proxy.
    """

    cache_read: int            # served from cache, heavily discounted
    input_uncached: int        # billed at list price
    output: int
    # Anthropic bills a cache write at 1.25x base input for the 5-minute TTL and
    # 2x for the 1-hour one — a 60% spread. Collapsing them into one number made
    # the write component wrong by up to that much, and 99.5% of the tokens
    # written across this repo's three captures went to the 1h bucket.
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    # A write whose TTL the payload did not report. Kept apart rather than
    # folded into the cheaper bucket: `cost()` refuses to price it when the two
    # rates differ. Present on 0 of 252 observed records, so this is about the
    # case nobody has seen, which is exactly the one that would slip through.
    cache_write_unspecified: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h + self.cache_write_unspecified

    @property
    def input_total(self) -> int:
        return self.cache_read + self.cache_write + self.input_uncached

    @property
    def hit_rate(self) -> float:
        return self.cache_read / self.input_total if self.input_total else 0.0


class UnknownProvider(KeyError):
    """Raised when a payload arrives from a provider we have not registered."""


def _anthropic(u: dict) -> Usage:
    """Anthropic reports the per-TTL split in `cache_creation`; use it.

    `cache_creation_input_tokens` is the aggregate. When the breakdown is
    present it is authoritative; when it is missing the aggregate goes to
    `cache_write_unspecified`, which `cost()` will refuse rather than price at
    a guessed rate. Any shortfall between the breakdown and the aggregate is
    treated the same way — a partial breakdown is not a complete one.
    """
    total = u.get("cache_creation_input_tokens", 0) or 0
    detail = u.get("cache_creation")
    if not isinstance(detail, dict):
        return Usage(
            cache_read=u.get("cache_read_input_tokens", 0) or 0,
            input_uncached=u.get("input_tokens", 0) or 0,
            output=u.get("output_tokens", 0) or 0,
            cache_write_unspecified=total,
        )
    five = detail.get("ephemeral_5m_input_tokens", 0) or 0
    hour = detail.get("ephemeral_1h_input_tokens", 0) or 0
    return Usage(
        cache_read=u.get("cache_read_input_tokens", 0) or 0,
        input_uncached=u.get("input_tokens", 0) or 0,
        output=u.get("output_tokens", 0) or 0,
        cache_write_5m=five,
        cache_write_1h=hour,
        cache_write_unspecified=max(total - five - hour, 0),
    )


def _deepseek(u: dict) -> Usage:
    # Automatic prefix caching: no separate write charge.
    # prompt_tokens == hit + miss, so miss alone is the list-priced part.
    return Usage(
        cache_read=u.get("prompt_cache_hit_tokens", 0) or 0,
        input_uncached=u.get("prompt_cache_miss_tokens", 0) or 0,
        output=u.get("completion_tokens", 0) or 0,
    )


def _openai(u: dict) -> Usage:
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    prompt = u.get("prompt_tokens", 0) or 0
    written = (u.get("prompt_tokens_details") or {}).get("cache_write_tokens", 0) or 0
    return Usage(
        cache_read=cached,
        # No TTL tiers: one write price, so the bucket it lands in is arbitrary
        # as long as the rate table gives both the same number.
        cache_write_5m=written,
        # OpenAI's prompt_tokens includes the cached portion; subtract it out.
        input_uncached=max(prompt - cached - written, 0),
        output=u.get("completion_tokens", 0) or 0,
    )


# The single source of truth for "which providers exist". Tests iterate this.
EXTRACTORS: dict[str, Callable[[dict], Usage]] = {
    "anthropic": _anthropic,
    "deepseek": _deepseek,
    "openai": _openai,
}


def normalise(provider: str, usage: dict) -> Usage:
    try:
        extract = EXTRACTORS[provider]
    except KeyError as exc:
        raise UnknownProvider(
            f"no usage extractor registered for {provider!r}; "
            f"known: {sorted(EXTRACTORS)}"
        ) from exc
    return extract(usage or {})
