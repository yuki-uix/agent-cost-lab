# cache-aware attribution — delivery notes

## Deliverables

- `src/agentcostlab/attribute.py` — `Divergence` gains a `suppressed: bool` field;
  `attribute()` now measures the prev request's last `cache_control` breakpoint
  and returns a *suppressed* divergence (not a break) when the first divergence
  falls after it. `_describe` is now passed the same normalised values the byte
  comparison used.
- `scripts/calibrate_attributor.py` — counts `suppressed` as "no divergence" and
  reports the suppressed total.
- `tests/test_attribute.py` — 6 new tests pinning the six AC2 counterexamples,
  plus the fixture test now asserts `suppressed` when a fixture records it.
- `fixtures/attribution/` — 2 updated (`messages_edited`,
  `messages_content_str_vs_block_different_text`: their recorded path moved to
  the normalised `…, "content", 0, "text"` form), 1 added
  (`messages_after_breakpoint`, a suppressed case).

## 1. AC1 — agreement rate, measured on `data/raw/capture.jsonl`

Ground truth is the same proxy as before (`usage.cache_read_input_tokens > 0`;
no `diagnostics` in any of the 75 records). Full raw output:

```
records: 75   pairs (by injected_previous_message_id): 63
official signal: usage.cache_read_input_tokens > 0
comparable pairs (signal present): 62
signal buckets:
  no_divergence              62

segment-order agreement rate (comparable pairs only):
  100.0%  (62/62)  model → tools → system → messages → params      ← default SEGMENT_ORDER
  100.0%  (62/62)  model → system → tools → messages → params
   … (60 orders at 100.0%, 60 orders at 58.1%; see below) …
   58.1%  (36/62)  messages → model → system → tools → params
   58.1%  (36/62)  messages → tools → system → params → model

best order: model → system → tools → messages → params   agreement 100.0% (62/62)

suppressed (text diverged after the last cache_control block, cache intact): 26

disagreement breakdown (best order):
  none
```

**58.1% → 100.0% (62/62).** The 26 former over-reports are all now `suppressed`
— every one is a text divergence that lands after the prev request's last
`cache_control` block, so the cache never broke.

The sweep is no longer order-independent. The breakpoint is a byte offset in the
*chosen* concatenation order, so "the last `cache_control` block" moves with the
order. The 60 orders at 100% are exactly the ones where `messages` comes after
`system` (tools has no `cache_control` in this capture, so its position is
irrelevant); the 60 at 58.1% put `messages` before `system`, under which the
last breakpoint is `system`'s and a message edit is inside the cached prefix.
The default order is in the 100% group. This is the first time the sweep has
discriminated — the breakpoint rule carries real evidence about segment order,
not just about divergence.

## 2. AC2 — the six counterexamples, each measured

The rule must suppress *only* "divergence after the last breakpoint". Each case
below is asserted by a test in `tests/test_attribute.py`; results are the actual
`attribute()` output.

| # | counterexample | expected | measured |
|---|---|---|---|
| 1 | change **before** the breakpoint | report | `messages`, `suppressed=False`, path `["messages",0,"content",0,"text"]` |
| 2 | change **in the breakpoint block itself** | report | `messages`, `suppressed=False`, path `["messages",1,"content",0,"text"]` |
| 3 | request with **no breakpoint** | report | `messages`, `suppressed=False` |
| 4 | `system` change | report | `system`, `suppressed=False` |
| 5 | `tools` change | report | `tools`, `suppressed=False` |
| 6 | change **after** the breakpoint | suppress | `messages`, `suppressed=True`, path `["messages",2,"content",0,"text"]` |

None of the first five are eaten. The suppression is byte-position based
(`divergence_offset >= breakpoint`), so a change *inside* the breakpoint block
(`within < breakpoint`) still reports, and a change before it reports too.

## 3. AC3 — `_describe` now sees the normalised values

`attribute()` previously compared `_dump(_normalise(...))` but handed `_describe`
the raw values. The result: whenever a real text change followed a normalised-away
`cache_control` difference, the reported path pointed at the discarded marker —
25 of the 26 real over-reports were that. Now:

```python
detail, path = _describe(component, prev_norm, curr_norm)
```

so the path is the real one. This does not change the agreement rate (the
divergence is re-located, not removed), but the reported location is now the
token-equivalent structure. Consequence: message `content` is always a list in
the normalised form, so a string-content text change is reported at
`["messages", i, "content", 0, "text"]` instead of `["messages", i, "content"]`.
The two fixtures that recorded the old raw path are updated:
`messages_edited.json`, `messages_content_str_vs_block_different_text.json`.

## 4. AC4 — the trace

Suppression returns a `Divergence` with `suppressed=True`, never `None`. The
`component` / `detail` / `path` / `bytes_*` still describe the suppressed change,
so a caller can see *that a post-breakpoint edit happened* and where. The
calibrate script counts `suppressed` as "no divergence" for agreement, and prints
the total separately (`suppressed …: 26`) so the suppression is visible in the
instrument output, not swallowed.

## 5. Considered but rejected

- **Ignore whole messages after the breakpoint (message granularity).** Rejected
  — the task's earlier runs showed it explains 1/26; the breakpoint is a *block*,
  so a same-message-later-block edit must also be suppressed.
- **"content layer is pure append".** Rejected — 0/26.
- **Returning `None` for suppressed (silent).** Rejected — violates AC4; the
  caller would lose the post-breakpoint edit entirely.
- **Computing the breakpoint in the fixed default `SEGMENT_ORDER` regardless of
  the sweep order.** Considered; it would keep the sweep "order-independent" at
  100%, but then the breakpoint and the divergence offset would be measured in
  two different byte spaces under non-default orders — a comparison that means
  nothing. I compute the breakpoint in the same `order` the comparison uses.
- **A separate sentinel type / `Optional`-wrapper for suppressed.** Rejected — a
  boolean field on the frozen `Divergence` is simpler and backward-compatible
  (existing callers that only read `component`/`path` are unaffected).
- **Making `diverging_components` suppress too.** Rejected — it is a lower-level
  "which text changed" primitive; the cache-break signal lives in `attribute()`'s
  return. It still lists `messages` for suppressed pairs, which is correct for
  its (text) meaning.

## 6. Uncertainties

- **The `>=` boundary is unexercised.** Every real suppressed pair has
  `bytes_before − breakpoint ∈ {24, 52}` (one block boundary away); a divergence
  landing *exactly* on the breakpoint byte is not in the data. `>=` treats it as
  "after", which I believe is right (the cached prefix is `[0, breakpoint)`), but
  it is untested.
- **Breakpoint is prev-only.** The rule is "after *prev*'s last `cache_control`";
  I don't model curr moving its own breakpoint earlier/later. Not needed for
  this capture (curr's markers are normalised away in messages and stable in
  system).
- **`bytes_before`'s gloss "(cached)" is now imprecise for suppressed cases** —
  for a suppressed divergence only `[0, breakpoint)` is cached; the run from the
  breakpoint to the divergence is uncached tail. The field remains byte-accurate
  (it still partitions the prefix); only the parenthetical gloss is loose. Left
  as-is rather than complicating the field's contract.
- **Top-level `params.cache_control` is ignored.** No such case in the 75 records
  (all markers are in `system` blocks or message content), and a top-level
  `cache_control` is not a real API shape.
- **n = 1 capture.** Still the single 75-record session. The 100% number and the
  60/60 order split are from one session; no generalisation claimed. The segment
  order is still a hypothesis, though the breakpoint rule now gives the sweep
  discriminating power it didn't have before.

## 7. Verification

```
$ .venv/bin/python -m pytest -q
128 passed in 12.52s
```

119 existing tests + 6 AC2 counterexample tests + 1 new fixture (exercised by
the 3 parametrised fixture/byte tests). No existing test was weakened; the two
fixture updates are path re-locations mandated by AC3, not assertion relaxations.
