"""Export gate: nothing reaches data/redacted/ without an explicit classification.

The proxy captures whole request bodies — source code, file paths, prompts,
auth headers. The failure mode we are guarding against is a *new* capture field
silently carrying secrets into the repo. So the gate fails closed: an
unclassified key raises rather than passing through.
"""
from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any


class Policy(Enum):
    KEEP = "keep"      # non-identifying, safe to publish
    HASH = "hash"      # shape is useful, content is not publishable
    DROP = "drop"      # never leaves the machine


class UnclassifiedField(RuntimeError):
    """A captured field has no policy. Classify it before exporting."""


# Every key the proxy can emit must appear here. Adding a capture field without
# adding a policy makes redact() raise and the test suite fail.
POLICIES: dict[str, Policy] = {
    "t_start": Policy.KEEP,
    "t_end": Policy.KEEP,
    "provider": Policy.KEEP,
    "model": Policy.KEEP,
    "client_bytes": Policy.KEEP,
    "request_bytes": Policy.KEEP,
    "first_byte_ms": Policy.KEEP,
    "usage": Policy.KEEP,
    "diagnostics": Policy.KEEP,
    "diagnostics_present": Policy.KEEP,
    "injected_previous_message_id": Policy.KEEP,
    # E4 campaign label. KEEP, and load-bearing: a redacted capture that lost it
    # would read as organic traffic, and the baseline arm (prediction 4.5, no
    # false positives) would be indistinguishable from "nobody armed anything".
    # The value is this repo's own metadata — fault id, turn number, and a
    # description written here — so it carries nothing of the user's.
    "injection": Policy.KEEP,
    "response_id": Policy.KEEP,
    "status_code": Policy.KEEP,
    "stream_complete": Policy.KEEP,
    "error": Policy.KEEP,
    # Shape is the whole point of the prefix-diff work; content is not ours to publish.
    "request_body": Policy.HASH,
    "system_prompt": Policy.HASH,
    "tool_names": Policy.HASH,
    # Never.
    "headers": Policy.DROP,
    "api_key": Policy.DROP,
    "authorization": Policy.DROP,
}


def _digest(value: Any) -> str:
    raw = repr(value).encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def redact(record: dict) -> dict:
    """Return a publishable copy. Raises on any key without a policy."""
    unknown = sorted(set(record) - set(POLICIES))
    if unknown:
        raise UnclassifiedField(
            f"unclassified capture field(s): {unknown}. "
            "Add a Policy in redact.POLICIES before exporting."
        )

    out: dict = {}
    for key, value in record.items():
        policy = POLICIES[key]
        if policy is Policy.KEEP:
            out[key] = value
        elif policy is Policy.HASH:
            out[key] = _digest(value)
        # DROP: omitted entirely.
    return out
