"""Prefix-diff attributor: where did the cache prefix break, and why?

Anthropic's ``cache_miss_reason`` names the *component* that diverged
(``model`` / ``system`` / ``tools`` / ``messages``) and nothing finer. This
module reproduces that attribution offline — from the two request bodies alone,
so it works for DeepSeek / OpenAI too — and drills down to the specific tool,
message index, or field.

The rule everything else hangs on: **the cache matches the tokenised prompt,
not the raw JSON envelope.** ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` are
semantically equal but byte-different — *within a node the cache is still
byte-sensitive* — but the API normalises and tokenises the request first, so
transport encodings that tokenise identically (``"content": "x"`` vs
``"content": [{"type": "text", "text": "x"}]``) must be treated as identical,
while a tool-schema key reorder must still be a divergence.

Comparison therefore never uses ``dict ==``; every node is re-serialised
byte-faithfully (the same ``json.dumps`` settings as ``proxy.serialise``), the
evidenced equivalent encodings are canonicalised by :func:`_normalise`, and the
result is compared as bytes. Each canonicalisation in :func:`_normalise` is
backed by real pairs in ``data/raw/capture.jsonl`` where the cache demonstrably
did not break (``cache_read_input_tokens`` kept climbing) — see the comments
there; no rule is added "because it looks equivalent".

The prefix itself is a *concatenation* of per-component byte strings in some
order. That order is a hypothesis, not a fact — Anthropic's docs phrase it two
ways ("tools, system, messages" on the prompt-caching page, "model, system,
tools" on the cache-diagnostics page). It lives in :data:`SEGMENT_ORDER` and is
measured, not assumed, by ``scripts/calibrate_attributor.py``.
"""
from __future__ import annotations

import json

from .codec import serialise
from dataclasses import dataclass

# The components that participate in the cache prefix. "params" is the catch-all
# for every remaining top-level field that affects the cache (tool_choice,
# thinking, context_management, output_config, output_format, cache_control, …).
COMPONENTS: tuple[str, ...] = ("model", "system", "tools", "messages", "params")

# Prefix concatenation order. A hypothesis to be measured, not a fact.
SEGMENT_ORDER: tuple[str, ...] = ("model", "tools", "system", "messages", "params")

# Top-level fields that do NOT participate in the cache prefix. A change in one
# of these leaves a cache hit intact, so the attributor ignores it. ``diagnostics``
# is injected by the proxy and changes every turn (previous_message_id) — it is
# never a real miss. sampling / transport knobs are documented as non-invalidating.
NON_CACHE_FIELDS: frozenset[str] = frozenset(
    {
        "max_tokens",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "metadata",
        "stream",
        "diagnostics",
    }
)

# Sentinel for "this component is absent from the body".
_MISSING = object()


@dataclass(frozen=True)
class Divergence:
    component: str          # "model" | "system" | "tools" | "messages" | "params"
    detail: str             # finer than the official reason: which tool / message / field
    path: list[str | int]   # structured location, e.g. ["tools", 3, "input_schema"]
    bytes_before: int       # bytes of the current prefix before the divergence (cached)
    bytes_after: int        # bytes after it (repriced at list price)


def _dump(value: object) -> bytes:
    """Byte-faithful serialisation, delegating to the shared encoder.

    This used to re-implement the proxy's settings with a "must stay in sync"
    comment. A comment is not a constraint: changing the proxy's encoder would
    have left this one behind, and every byte offset reported here would then
    describe a stream that was never sent.
    """
    if value is _MISSING:
        return b""
    return serialise(value)


def _normalise_content(content: object) -> object:
    """Canonicalise a message ``content`` to its token-equivalent form.

    Two rules, each evidenced by real pairs in ``data/raw/capture.jsonl`` where
    the cache demonstrably did not break:

    *   **string  ⟺  single ``text`` block.**  Claude Code writes the same
        ``<system-reminder>`` message as a bare string in one turn and as a
        one-element ``[{"type": "text", "text": ...}]`` block the next. There
        are 35 such flips in the capture (e.g. record 2→3 message 1, 3→4
        message 4, 5→7 message 7); in every one ``cache_read_input_tokens``
        keeps climbing (68,388 → 68,612 → 68,638 → …) — the cache never broke,
        because the API tokenises both forms identically.

    *   **drop ``cache_control`` from ``text`` blocks.**  ``cache_control``
        marks a cache *write* breakpoint; it is not token content, so its
        presence/absence never changes the read-side prefix match. 7 assistant
        messages in the capture flip only this marker between turns (e.g.
        record 4→5 message 5, 8→9 message 11) while ``cache_read_input_tokens``
        still climbs — a moving breakpoint does not invalidate the prefix.

    Everything else is passed through unchanged: a tool-schema key reorder, a
    tool add/remove/reorder, or any change to text itself must still be a
    divergence. Within the normalised nodes comparison remains byte-faithful.
    """
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                # Preserve key order; only drop the cache-breakpoint marker.
                block = {k: v for k, v in block.items() if k != "cache_control"}
            out.append(block)
        content = out
    return content


def _normalise_message(message: object) -> object:
    """Canonicalise one message dict for comparison (content only)."""
    if not isinstance(message, dict) or "content" not in message:
        return message
    message = dict(message)
    message["content"] = _normalise_content(message["content"])
    return message


def _normalise(component: str, value: object) -> object:
    """Canonicalise a cache-prefix segment before byte comparison.

    Only ``messages`` has evidenced equivalent encodings. ``tools`` is
    deliberately left alone — a key-order change in a tool schema must still
    surface as ``tools_changed`` (AC2), so no generic "sort keys" normalisation
    is applied anywhere.
    """
    if component == "messages" and isinstance(value, list):
        return [_normalise_message(m) for m in value]
    return value


def _segments(body: dict) -> dict[str, object]:
    """Split a request body into its cache-prefix components.

    ``params`` preserves the body's own key order (JSON order as received), so a
    reordering of param fields is still caught as a byte difference.
    """
    segments: dict[str, object] = {
        comp: body.get(comp, _MISSING) for comp in ("model", "system", "tools", "messages")
    }
    params: dict = {}
    for key, value in body.items():
        if key in ("model", "system", "tools", "messages") or key in NON_CACHE_FIELDS:
            continue
        params[key] = value
    segments["params"] = params if params else _MISSING
    return segments


def prefix_bytes(body: dict, *, order: tuple[str, ...] = SEGMENT_ORDER) -> bytes:
    """The concatenated prefix bytes for one body, in ``order``.

    This is the byte string the cache would key on under the chosen segment
    order: segments are canonicalised with :func:`_normalise` first, because the
    cache keys the tokenised prompt, not the raw envelope. Its length is the
    "total serialised bytes" that a divergence's ``bytes_before`` and
    ``bytes_after`` must partition (AC6).
    """
    segments = _segments(body)
    return b"".join(_dump(_normalise(comp, segments[comp])) for comp in order)


def _common_prefix(a: bytes, b: bytes) -> int:
    """Length of the longest common byte prefix of ``a`` and ``b``."""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def _is_append(prev: object, curr: object) -> bool:
    """True when ``curr`` is ``prev`` with extra list items appended.

    Only messages legitimately *grow* across a conversation (each turn appends
    assistant + user). Growth is not a divergence: the old prefix still hits, the
    new tail is cache-write. Tools, by contrast, are supposed to be byte-stable,
    so adding a tool is ``tools_changed`` — which is why this is not applied to
    every list.
    """
    if not isinstance(prev, list) or not isinstance(curr, list):
        return False
    if len(curr) <= len(prev):
        return False
    return all(
        _dump(_normalise_message(prev[i])) == _dump(_normalise_message(curr[i]))
        for i in range(len(prev))
    )


def attribute(
    prev_body: dict, curr_body: dict, *, order: tuple[str, ...] = SEGMENT_ORDER
) -> Divergence | None:
    """Return the first prefix divergence between two consecutive request bodies.

    ``None`` means "no divergence": the two prefixes are byte-identical, or the
    only difference is messages appended onto the end (the normal per-turn
    growth, which Anthropic likewise reports as "no divergence").
    """
    prev_segments = _segments(prev_body)
    curr_segments = _segments(curr_body)
    total = len(prefix_bytes(curr_body, order=order))

    offset = 0  # bytes of curr's prefix consumed before the current segment
    for component in order:
        prev_value = prev_segments[component]
        curr_value = curr_segments[component]
        # Compare the token-equivalent (canonicalised) bytes, but keep the raw
        # values for _describe so paths stay on the original structure.
        prev_bytes = _dump(_normalise(component, prev_value))
        curr_bytes = _dump(_normalise(component, curr_value))
        if prev_bytes == curr_bytes:
            offset += len(curr_bytes)
            continue
        if component == "messages" and _is_append(prev_value, curr_value):
            # Growth, not a break: skip the whole (grown) segment.
            offset += len(curr_bytes)
            continue

        within = _common_prefix(prev_bytes, curr_bytes)
        detail, path = _describe(component, prev_value, curr_value)
        return Divergence(
            component=component,
            detail=detail,
            path=path,
            bytes_before=offset + within,
            bytes_after=total - (offset + within),
        )
    return None


def diverging_components(
    prev_body: dict, curr_body: dict, *, order: tuple[str, ...] = SEGMENT_ORDER
) -> list[str]:
    """Every component that actually diverges, in ``order``.

    Used by the calibrate script to tell "both changed, we just attributed a
    different one" apart from "we missed a change" (AC2).
    """
    prev_segments = _segments(prev_body)
    curr_segments = _segments(curr_body)
    diverged: list[str] = []
    for component in order:
        prev_value = prev_segments[component]
        curr_value = curr_segments[component]
        if _dump(_normalise(component, prev_value)) == _dump(_normalise(component, curr_value)):
            continue
        if component == "messages" and _is_append(prev_value, curr_value):
            continue
        diverged.append(component)
    return diverged


# --- structural description -------------------------------------------------
#
# Byte accounting (bytes_before/bytes_after) is computed from the byte-common
# prefix, not from this walk. This walk only answers "what changed, and where",
# for the `detail` and `path` fields. It must still detect byte-only differences
# (dict key order) because those are real misses.

def _describe(component: str, prev: object, curr: object) -> tuple[str, list]:
    path: list = [component]
    if component == "tools":
        return _describe_tools(prev, curr, path)
    if component == "messages":
        return _describe_messages(prev, curr, path)
    if component == "model":
        return f"model changed: {_show(prev)} -> {_show(curr)}", path
    detail = _diff(prev, curr, path)
    return detail, path


def _show(value: object) -> str:
    if value is _MISSING:
        return "<absent>"
    return json.dumps(value, ensure_ascii=False)


def _diff(prev: object, curr: object, path: list) -> str:
    """Walk to the first structural difference, extending ``path`` in place.

    Precondition: ``_dump(prev) != _dump(curr)``.
    """
    if prev is _MISSING:
        return "added"
    if curr is _MISSING:
        return "removed"

    if isinstance(prev, dict) and isinstance(curr, dict):
        prev_keys = list(prev)
        curr_keys = list(curr)
        if set(prev_keys) != set(curr_keys):
            for key in curr_keys:
                if key not in prev:
                    path.append(key)
                    return f"field added: {key!r}"
            for key in prev_keys:
                if key not in curr:
                    path.append(key)
                    return f"field removed: {key!r}"
        if prev_keys != curr_keys:
            # Same key set, different order: byte-different even though dict ==.
            for i in range(len(prev_keys)):
                if prev_keys[i] != curr_keys[i]:
                    path.append(prev_keys[i])
                    return f"field order changed at {prev_keys[i]!r}"
            return "field order changed"
        for key in curr_keys:
            if _dump(prev[key]) != _dump(curr[key]):
                path.append(key)
                return _diff(prev[key], curr[key], path)
        return "changed"  # unreachable given the precondition

    if isinstance(prev, list) and isinstance(curr, list):
        common = min(len(prev), len(curr))
        for i in range(common):
            if _dump(prev[i]) != _dump(curr[i]):
                path.append(i)
                return _diff(prev[i], curr[i], path)
        if len(prev) < len(curr):
            path.append(len(prev))
            return "element appended"
        path.append(len(curr))
        return "element removed"

    if isinstance(prev, str) and isinstance(curr, str):
        at = _common_prefix(prev.encode("utf-8"), curr.encode("utf-8"))
        return f"text differs at byte {at}"

    return "value changed"


def _describe_tools(prev: object, curr: object, path: list) -> tuple[str, list]:
    if not isinstance(prev, list) or not isinstance(curr, list):
        return _diff(prev, curr, path), path
    prev_names = [_tool_name(t) for t in prev]
    curr_names = [_tool_name(t) for t in curr]

    if sorted(map(str, prev_names)) == sorted(map(str, curr_names)):
        if prev_names == curr_names:
            # Same tools, same order: a tool's body changed (schema, description…).
            for i in range(len(prev_names)):
                if _dump(prev[i]) != _dump(curr[i]):
                    path.append(i)
                    sub = _diff(prev[i], curr[i], path)
                    return f"tool {prev_names[i]!r}: {sub}", path
            return "tools changed", path  # unreachable
        # Same set, different order.
        i = next(i for i in range(len(prev_names)) if prev_names[i] != curr_names[i])
        path.append(i)
        return f"tool reordered: {prev_names[i]!r} -> {curr_names[i]!r}", path

    # A tool was added, removed, or replaced.
    common = min(len(prev), len(curr))
    for i in range(common):
        if _dump(prev[i]) != _dump(curr[i]):
            path.append(i)
            return f"tool replaced at index {i}: {prev_names[i]!r} -> {curr_names[i]!r}", path
    if len(prev) < len(curr):
        path.append(len(prev))
        return f"tool added: {curr_names[len(prev):]!r}", path
    path.append(len(curr))
    return f"tool removed: {prev_names[len(curr):]!r}", path


def _describe_messages(prev: object, curr: object, path: list) -> tuple[str, list]:
    if not isinstance(prev, list) or not isinstance(curr, list):
        return _diff(prev, curr, path), path
    common = min(len(prev), len(curr))
    for i in range(common):
        if _dump(prev[i]) != _dump(curr[i]):
            path.append(i)
            sub = _diff(prev[i], curr[i], path)
            return f"message {i}: {sub}", path
    # Append is handled by attribute() before this is called; here the shorter
    # side is the current request, i.e. history was truncated.
    path.append(len(curr))
    return f"message removed at index {len(curr)} (truncated {len(prev) - len(curr)})", path


def _tool_name(tool: object) -> object:
    if isinstance(tool, dict):
        return tool.get("name")
    return None
