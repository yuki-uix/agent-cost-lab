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

Two questions were once conflated: "did the text change?" and "did the cache
break?" They are different. ``cache_control`` marks the *end* of the cacheable
prefix — everything after the request's last ``cache_control`` block is a
cache-*write* tail that was never in the cache. A divergence that falls after
that marker is therefore a text change that does **not** break the cache: the
attributor returns it ``suppressed`` (still visible to the caller, not counted
as a miss) instead of reporting a cache break.

Where that marker sits is a physical fact, not a hypothesis, so it must not be
taken from :data:`SEGMENT_ORDER`. The cacheable prefix is laid out
``tools → system → messages`` (Anthropic's documented request layout), captured
in :data:`CACHE_LAYOUT`; suppression is decided structurally in that order and
never by comparing byte offsets measured in two different spaces. Sweeping
``SEGMENT_ORDER`` still changes *which* divergence is attributed first, but it
no longer changes whether a given divergence broke the cache.
"""
from __future__ import annotations

import json

from .codec import serialise
from dataclasses import dataclass

# The components that participate in the cache prefix. "params" is the catch-all
# for every remaining top-level field that affects the cache (tool_choice,
# thinking, context_management, output_config, output_format, cache_control, …).
COMPONENTS: tuple[str, ...] = ("model", "system", "tools", "messages", "params")

# Prefix concatenation order. A hypothesis to be measured, not a fact: which
# component to blame *first* when several diverge. ``scripts/calibrate_attributor.py``
# sweeps it. It must NOT feed the suppression decision — where the cacheable
# prefix ends is a physical fact fixed by :data:`CACHE_LAYOUT`, not a hypothesis.
SEGMENT_ORDER: tuple[str, ...] = ("model", "tools", "system", "messages", "params")

# The cache prefix's physical layout: Anthropic's documented request layout is
# tools → system → messages, and that is what fixes where the cacheable prefix
# ends — the last ``cache_control`` block in *this* order is the true end of the
# cached prefix, regardless of the order the attributor is asked to blame first.
# This encodes an assumption about Anthropic's request layout; the suppression
# rule degrades on a provider that lays the prefix out differently.
CACHE_LAYOUT: tuple[str, ...] = ("tools", "system", "messages")

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
    suppressed: bool = False  # text diverged after the last cache_control block: cache intact


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


def _dict_value_offset(mapping: dict, key: str) -> int | None:
    """Byte offset within ``serialise(mapping)`` where ``key``'s value begins.

    ``serialise`` is deterministic and preserves key order, so walking the dict
    and summing ``len(serialise(k)) + 1`` for each ``"key":`` before the target
    yields the exact position — no re-serialisation guesswork.
    """
    offset = 1  # '{'
    for k, v in mapping.items():
        entry_start = offset + (0 if offset == 1 else 1)  # ',' before entries 2..n
        if k == key:
            return entry_start + len(serialise(k)) + 1  # value starts after '"key":'
        offset = entry_start + len(serialise(k)) + 1 + len(serialise(v))
    return None


def _list_breakpoint_end(value: object) -> int | None:
    """End byte offset (within ``serialise(value)``) of the last element carrying
    a ``cache_control`` key, or ``None``.

    For ``system`` and ``tools`` — both lists of blocks/tools that ``_normalise``
    leaves untouched — raw and normalised bytes coincide, so block end offsets
    are just ``serialise`` lengths accumulated over the list.
    """
    if not isinstance(value, list):
        return None
    inner = 1  # '['
    last = None
    for block in value:
        end = inner + len(serialise(block))
        if isinstance(block, dict) and "cache_control" in block:
            last = end
        inner = end + 1  # ','
    return last


def _content_breakpoint_end(
    message_offset: int, content_offset: int, raw_content: list, norm_content: list
) -> int | None:
    """Absolute end byte offset of the last ``cache_control`` block in one
    message's ``content``, or ``None``.

    ``raw_content`` and ``norm_content`` have the same length — normalisation
    drops keys, never whole blocks — so block ``j`` is breakpoint-marked iff the
    raw block ``j`` carries ``cache_control``, and its normalised end offset is
    walked from ``serialise(norm_content[j])``.
    """
    inner = 1  # '[' of the content list
    last = None
    for j, raw_block in enumerate(raw_content):
        end = inner + len(serialise(norm_content[j]))
        if isinstance(raw_block, dict) and "cache_control" in raw_block:
            last = message_offset + content_offset + end
        inner = end + 1  # ','
    return last


def _messages_breakpoint_end(messages: object) -> int | None:
    """End byte offset (within ``serialise`` of the normalised messages list) of
    the last ``cache_control``-bearing content block, or ``None``."""
    if not isinstance(messages, list):
        return None
    offset = 1  # '['
    last = None
    for raw_message in messages:
        norm_message = _normalise_message(raw_message)
        message_bytes = serialise(norm_message)
        if isinstance(raw_message, dict) and isinstance(norm_message, dict):
            raw_content = raw_message.get("content")
            if isinstance(raw_content, list) and "content" in norm_message:
                content_offset = _dict_value_offset(norm_message, "content")
                if content_offset is not None:
                    end = _content_breakpoint_end(
                        offset, content_offset, raw_content, norm_message["content"]
                    )
                    if end is not None:
                        last = end
        offset += len(message_bytes) + 1  # ','
    return last


def _cache_directives(component: str, value: object) -> frozenset[str]:
    """The set of distinct ``cache_control`` values a segment asks for.

    ``ttl`` selects which cache the lookup goes to, so changing it moves the
    write to a different bucket and the previous turn's tail is not found.
    Observed once on real traffic (capture-02 record 61, 2026-08-19): Claude
    Code dropped ``ttl: "1h"`` from every marker in one request, 2,822 tokens
    had to be paid for again, and the official diagnostics could only say
    ``unavailable``.

    A **set**, deliberately, so this stays orthogonal to position and count:

    *   `_normalise_content` strips the marker from message text blocks because
        Claude Code walks the breakpoint down the conversation every turn and
        treating each move as a change produced 25 false reports (#21). That
        evidence is about *where* the marker sits.
    *   What it says is a different matter, and is what this compares.
    *   Adding a marker whose value is already in use changes neither the set
        nor which buckets are consulted. Neither capture contains a single
        marker-count change, so there is no evidence for a count rule and this
        does not invent one.
    """
    blocks: list[object] = []
    if component == "messages" and isinstance(value, list):
        for message in value:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                blocks.extend(content)
    elif isinstance(value, list):
        blocks = list(value)
    return frozenset(
        serialise(block["cache_control"]).decode()
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("cache_control"), dict)
    )


def _cache_breakpoint(segments: dict[str, object]) -> tuple[str, int] | None:
    """(component, byte offset within it) of the end of the last
    ``cache_control``-bearing block, walking :data:`CACHE_LAYOUT`, or ``None``.

    The layout order is a physical fact, not the swept :data:`SEGMENT_ORDER`
    hypothesis: ``cache_control`` marks the *end* of the cacheable prefix, and
    the last marker in cache-layout order is where that prefix actually ends.
    The offset is measured in the component's own normalised byte space, so it is
    comparable to a divergence's ``within`` offset only when the divergence is in
    the *same* component — cross-component suppression is decided structurally
    (see :func:`attribute`), never by comparing offsets from different spaces.
    """
    for component in reversed(CACHE_LAYOUT):
        raw = segments[component]
        if component == "messages":
            rel = _messages_breakpoint_end(raw)
        else:  # "system" / "tools"
            rel = _list_breakpoint_end(raw)
        if rel is not None:
            return component, rel
    return None


def _is_suppressed(component: str, within: int, breakpoint: tuple[str, int] | None) -> bool:
    """Whether a divergence ``within`` bytes into ``component`` is a break.

    Structural in :data:`CACHE_LAYOUT`: a divergence in a component *after* the
    component that holds the last breakpoint is in the uncached write tail
    (suppressed); a component before it is still cached (a break); ``model`` and
    ``params`` invalidate the whole prefix (never suppressed). Same component:
    the divergence suppresses only when it lands after that component's own last
    breakpoint byte — ``within`` and the breakpoint offset are then both measured
    in the same component's byte space, so the comparison is meaningful.
    """
    if component in ("model", "params"):
        return False
    if breakpoint is None:
        return False
    bp_component, bp_offset = breakpoint
    div_index = CACHE_LAYOUT.index(component)
    bp_index = CACHE_LAYOUT.index(bp_component)
    if div_index > bp_index:
        return True
    if div_index < bp_index:
        return False
    return within >= bp_offset


def attribute(
    prev_body: dict, curr_body: dict, *, order: tuple[str, ...] = SEGMENT_ORDER
) -> Divergence | None:
    """Return the first prefix divergence between two consecutive request bodies.

    ``None`` means "no divergence": the two prefixes are byte-identical, or the
    only difference is messages appended onto the end (the normal per-turn
    growth, which Anthropic likewise reports as "no divergence").

    A divergence that falls *after* the prev request's last ``cache_control``
    block — the last one in :data:`CACHE_LAYOUT`, not in the swept ``order`` —
    is a text change in the non-cached tail: it is still returned (with
    ``suppressed=True``) so the caller can see it, but it did not break the
    cache. A plain ``Divergence`` means the cache did break.
    """
    prev_segments = _segments(prev_body)
    curr_segments = _segments(curr_body)
    total = len(prefix_bytes(curr_body, order=order))
    breakpoint = _cache_breakpoint(prev_segments)

    offset = 0  # bytes of curr's prefix consumed before the current segment
    for component in order:
        prev_value = prev_segments[component]
        curr_value = curr_segments[component]
        prev_norm = _normalise(component, prev_value)
        curr_norm = _normalise(component, curr_value)
        prev_bytes = _dump(prev_norm)
        curr_bytes = _dump(curr_norm)
        if prev_bytes == curr_bytes:
            # Equal bytes is not equal directives: `cache_control` was stripped
            # out of message text blocks on the way here, so a ttl change inside
            # `messages` survives normalisation invisibly. It broke the cache the
            # one time it was observed, and `system` only caught that because
            # nothing strips the marker there.
            prev_cc = _cache_directives(component, prev_value)
            curr_cc = _cache_directives(component, curr_value)
            # Compared only when both sides declare something. A marker
            # vanishing with nothing in its place is a different question —
            # whether an uncached request still reads a previously written
            # prefix — and neither capture contains one instance of it
            # (marker counts never change across 111 pairs). #21 settled that
            # case in the other direction on its own evidence, and overturning
            # it here would be asserting a rule this repo cannot support.
            if prev_cc and curr_cc and prev_cc != curr_cc:
                return Divergence(
                    component=component,
                    detail=f"cache_control changed: {sorted(prev_cc)} -> {sorted(curr_cc)}",
                    path=[component, "cache_control"],
                    bytes_before=offset,
                    bytes_after=total - offset,
                    # Never suppressed. The marker *is* the breakpoint, so a
                    # change to it moves the write bucket rather than editing an
                    # uncached tail; "after the breakpoint" does not apply.
                    suppressed=False,
                )
            offset += len(curr_bytes)
            continue
        if component == "messages" and _is_append(prev_value, curr_value):
            # Growth, not a break: skip the whole (grown) segment.
            offset += len(curr_bytes)
            continue

        within = _common_prefix(prev_bytes, curr_bytes)
        # _describe must see the same normalised values the comparison used. The
        # old raw-value call reported a cache_control difference the comparison
        # had already discarded — 25 of the 26 real over-reports pointed there.
        detail, path = _describe(component, prev_norm, curr_norm)
        return Divergence(
            component=component,
            detail=detail,
            path=path,
            bytes_before=offset + within,
            bytes_after=total - (offset + within),
            suppressed=_is_suppressed(component, within, breakpoint),
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
