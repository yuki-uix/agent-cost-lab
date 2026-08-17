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

    cache_read: int       # served from cache, heavily discounted
    cache_write: int      # written to cache this turn (Anthropic bills a premium)
    input_uncached: int   # billed at list price
    output: int

    @property
    def input_total(self) -> int:
        return self.cache_read + self.cache_write + self.input_uncached

    @property
    def hit_rate(self) -> float:
        return self.cache_read / self.input_total if self.input_total else 0.0


class UnknownProvider(KeyError):
    """Raised when a payload arrives from a provider we have not registered."""


def _anthropic(u: dict) -> Usage:
    return Usage(
        cache_read=u.get("cache_read_input_tokens", 0) or 0,
        cache_write=u.get("cache_creation_input_tokens", 0) or 0,
        input_uncached=u.get("input_tokens", 0) or 0,
        output=u.get("output_tokens", 0) or 0,
    )


def _deepseek(u: dict) -> Usage:
    # Automatic prefix caching: no separate write charge.
    # prompt_tokens == hit + miss, so miss alone is the list-priced part.
    return Usage(
        cache_read=u.get("prompt_cache_hit_tokens", 0) or 0,
        cache_write=0,
        input_uncached=u.get("prompt_cache_miss_tokens", 0) or 0,
        output=u.get("completion_tokens", 0) or 0,
    )


def _openai(u: dict) -> Usage:
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    prompt = u.get("prompt_tokens", 0) or 0
    written = (u.get("prompt_tokens_details") or {}).get("cache_write_tokens", 0) or 0
    return Usage(
        cache_read=cached,
        cache_write=written,
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
