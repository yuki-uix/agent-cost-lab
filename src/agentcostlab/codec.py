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
