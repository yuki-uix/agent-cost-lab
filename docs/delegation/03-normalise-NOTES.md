# attributor normalisation — delivery notes

## Deliverables

- `src/agentcostlab/attribute.py` — `_normalise()` canonicalises the two
  evidenced transport-equivalent encodings before byte comparison; everything
  else stays byte-faithful.
- `tests/test_attribute.py` — 5 new unit tests (AC1 string-vs-block, AC2 tool
  schema key order, cache_control marker, different-text-not-merged).
- `fixtures/attribution/` — 3 new fixtures:
  - `messages_content_str_vs_block.json` (string ⟺ single text block, expected `null`)
  - `messages_cache_control_marker.json` (cache_control marker flip, expected `null`)
  - `messages_content_str_vs_block_different_text.json` (different text still diverges)

## 1. AC3 — agreement rate, measured on `data/raw/capture.jsonl`

Ground truth in this capture is **not** `cache_miss_reason` — that field is
absent from all 75 records (0 have `diagnostics`). The proxy signal is
`usage.cache_read_input_tokens > 0`, and it is exact here: for all 62
comparable turns,

```
curr.cache_read == prev.cache_read + prev.cache_creation
```

i.e. every turn read the **entire** previous prefix back from cache. The cache
never breaks in this capture.

**Before (HEAD attributor):**

```
records: 75   pairs (by injected_previous_message_id): 63
official signal: usage.cache_read_input_tokens > 0
comparable pairs (signal present): 62
signal buckets:
  no_divergence              62

agreement rate: 1.6%  (1/62)  (all 120 segment orders tie — only `messages` ever diverges)
best order: model → system → tools → messages → params   agreement 1.6% (1/62)

disagreement breakdown (best order):
  over-reported        61
```

**After (this change):**

```
records: 75   pairs (by injected_previous_message_id): 63
official signal: usage.cache_read_input_tokens > 0
comparable pairs (signal present): 62
signal buckets:
  no_divergence              62

agreement rate: 58.1%  (36/62)  (all 120 segment orders tie — only `messages` ever diverges)
best order: model → system → tools → messages → params   agreement 58.1% (36/62)

disagreement breakdown (best order):
  over-reported        26
```

**1.6% → 58.1%** (61 over-reported → 26 over-reported). The rate is
order-independent: all 120 segment-order permutations tie, because only
`messages` ever diverges in this capture.

## 2. Normalisation rules, each with real-data evidence

Both rules are canonicalisations of the message `content` only; nothing else is
touched. Each is backed by pairs in `data/raw/capture.jsonl` where the cache
demonstrably did not break.

### Rule 1 — string ⟺ single `text` block

`"content": "x"` and `"content": [{"type": "text", "text": "x"}]` tokenise
identically, so they are canonicalised to the same form.

Evidence: **35** messages flip between these two encodings across the capture
(e.g. `msg_011Ce9vr → msg_011Ce9vr` message 1 and message 4,
`msg_011Ce9vu → msg_011Ce9vu` message 7, `msg_011Ce9vx → msg_011Ce9vy`
message 13). In every case the turn reads the full previous prefix
(`curr.cache_read == prev.cache_read + prev.cache_creation`), so the cache did
not break across the flip.

### Rule 2 — drop `cache_control` from `text` blocks

`cache_control: {"type": "ephemeral", "ttl": "1h"}` is a cache-*write*
breakpoint marker, not token content; its presence/absence never changes the
read-side prefix match, so it is removed from the comparison.

Evidence: **7** block-form assistant messages differ *only* in the
`cache_control` marker between turns (e.g. `msg_011Ce9vr → msg_011Ce9vu`
message 5, `msg_011Ce9vu → msg_011Ce9vx` message 11, `msg_011Ce9vy →
msg_011Ce9w3` message 17), all while `cache_read_input_tokens` keeps climbing
— a moving breakpoint does not invalidate the prefix.

## 3. Considered but rejected

- **Sorting / ignoring key order in tool schemas.** Rejected — AC2 mandates a
  tool-schema key reorder still diverges, and there is no real-data evidence
  the cache ignores it (this capture's tools never change). The existing
  byte-trap fixture proves key order is a real miss, so no generic sort is
  applied anywhere.
- **`dict ==` / semantic equality.** Rejected — the spec explicitly forbids
  relaxing to semantic equality, and it would mask the 26 genuine text changes
  below.
- **Merging multiple `text` blocks (or concatenating all blocks).** Rejected —
  all 35 string/block flips in this capture are single-block; there are no
  string-vs-multi-block cases, so there is no evidence to justify flattening
  multi-block arrays or reordering non-text blocks (images, `tool_use`).
- **Dropping `cache_control` outside `text` blocks (e.g. top-level `params`).**
  Rejected — no evidence in this capture; only the text-block marker is
  evidenced to flip.

## 4. The 26 remaining disagreements — all genuine text changes

Every one of the 26 remaining `over-reported` rows is a **text-content change**
(not string/block, not `cache_control`, not key order). They split:

- **18 transcript rewrites** — a `<transcript>` block rewritten mid-stream
  (new user/assistant turns inserted before `</transcript>`), in the final
  user message of a 2-message auxiliary (title/recap) conversation.
- **8 placeholder replacements** — an injected fixed prompt (an 1363-byte
  `[SUGGESTION MODE …]` block, or a 253-byte recap prompt) replaced by the
  user's real short input, in the final user message of the main conversation.

These correctly remain divergences under the spec: "any change to text content
itself must still be a divergence."

### A finding worth flagging

All 26 changed messages are the **final user message** of the previous request,
and for all 62 comparable turns `curr.cache_read == prev.cache_read +
prev.cache_creation` — the cache read back the *complete* previous prefix even
across these text changes. In other words, the data hints these trailing-text
edits live after the `cache_control` breakpoint (the non-cached tail) and do
**not** actually invalidate the cache.

A further normalisation — "ignore messages after the last `cache_control`
breakpoint" — would plausibly close most of the remaining 26, but it would
collapse genuine text changes, which the spec's iron rule forbids. I did not
implement it; it is the top open question below.

## 5. Uncertainties

- **`cache_miss_reason` is absent.** The proxy (`cache_read > 0`) is exact
  here only because `curr.cache_read == prev.cache_read + prev.cache_creation`
  for all 62 turns. On a capture that *does* break the cache, this proxy would
  over-simplify (a late break still reads a large prefix), and the real
  `*_changed` reasons should be used instead.
- **The 26 remaining disagreements are unresolved by design**, not by
  measurement error. They are text changes the spec mandates as divergences,
  while the data suggests they do not break the cache. The right answer depends
  on whether "the cache actually broke" or "the text changed" is the thing the
  attributor should report — a product decision, not a technical one.
- **n = 1 capture** (75 records, one session). The 35 / 7 evidence counts and
  the 58.1% ceiling are from one Claude Code session; no generalisation is
  claimed.
- **Segment order remains a hypothesis.** It is order-independent in this
  capture (only `messages` diverges), so nothing here validates the default
  `model → tools → system → messages → params` ordering.

## 6. Verification

```
$ .venv/bin/python -m pytest -q
119 passed in 12.61s
```

105 existing tests + 3 new fixtures (auto-discovered by the parametrised
fixture tests) + 5 new unit tests. The 13 original fixtures all still pass;
path stability is preserved by passing raw values to `_describe`.
