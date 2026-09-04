"""The one place request bytes are produced.

Both the proxy (which sends them) and the attributor (which diffs them) must
agree byte for byte, or the attributor reports divergences against a stream
that was never transmitted. Keeping two copies in sync by comment has already
been tried here; this module exists so there is nothing to keep in sync.

Lives apart from proxy.py so that pure-logic callers do not pull in the web
stack just to encode a dict.
"""
from __future__ import annotations

import json


def serialise(value: object) -> bytes:
    """Re-emit a JSON value without rewriting it.

    The default json.dumps escapes non-ASCII to \\uXXXX and pads separators,
    which inflated a CJK payload by 39% in testing. An instrument that silently
    rewrites its subject is worse than no instrument, so both settings are
    load-bearing, not style.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def strip_cache_control(value: object) -> object:
    """Drop every ``cache_control`` key, at every depth and every block type.

    ``cache_control`` marks a cache-*write* breakpoint, not conversation content.
    Claude Code walks that breakpoint down the conversation every turn, so an
    identity that hashes ``messages[0]`` verbatim splits one conversation into
    several as the marker moves between blocks. Stripping it everywhere — top
    level, nested inside a ``content`` array, on ``tool_use`` / ``tool_result``
    blocks alike — is what keeps "the same first message" the same first message.

    This is deliberately broader than ``attribute._normalise_content``, which
    strips the marker from ``text`` blocks only. The two answer different
    questions: for identity, where the marker sits is irrelevant; for divergence
    attribution, it is the evidence itself.
    """
    if isinstance(value, dict):
        return {
            key: strip_cache_control(item)
            for key, item in value.items()
            if key != "cache_control"
        }
    if isinstance(value, list):
        return [strip_cache_control(item) for item in value]
    return value
