# order-independent suppression — delivery notes

## Deliverables

- `src/agentcostlab/attribute.py` — new module-level `CACHE_LAYOUT` constant
  (`tools → system → messages`); `_cache_breakpoint_offset()` deleted and replaced
  by `_cache_breakpoint()` (returns `(component, offset-within-component)`, walked
  in cache-layout order) and `_is_suppressed()` (the structural rule).
  `attribute()`'s suppression decision now consumes only the cache-layout
  breakpoint, never `order`.
- `scripts/calibrate_attributor.py` — the all-tie branch no longer prints a
  "best order"; the disagreement-breakdown header is neutralised.
- `tests/test_attribute.py` — six AC2 counterexample tests now assert under all
  120 orders (plus a new `model` change test); AC1 real-capture invariance test;
  AC4 multi-component-breakpoint test.
- `fixtures/attribution/multi_component_breakpoints.json` — the 46-record shape
  (system: 2 `cache_control` blocks, messages: 1).
- `report/02-method.md` — stated limitation: the suppression rule assumes the
  `tools → system → messages` layout.

## 1. Reproduction

The exact probe from the issue, run before any change:

```
36 (None, '-', None, '-')
26 ('messages', True, 'messages', False)
```

36 comparable pairs show no divergence under either order; the other 26 all
diverge in `messages` and flip verdict by order: `system`-first reports
`suppressed=True`, `messages`-first reports `suppressed=False`. Same pair, same
physical fact, opposite answer. Confirmed.

Measured `cache_control` block counts over **all 75 records** (system, tools,
messages):

```
(2, 0, 1) -> 46
(1, 0, 3) -> 19
(0, 0, 0) -> 10
```

This matches the issue's stated numbers exactly. (`tools` never carries a
`cache_control` block in this capture.) Claude Code marks **both** `system` and
`messages`; whichever of those two lands last in `order` captures the single
`last` byte offset.

## 2. Root cause

`_cache_breakpoint_offset()` walked `order` and kept one `last` byte offset —
the end of the last `cache_control`-bearing block *anywhere* in the flattened
concatenation. `attribute()` then suppressed when `offset + within >= breakpoint`.

Because the breakpoint is an absolute offset in the *chosen* `order`'s byte
space, "the last breakpoint" moves with `order`. When `messages` is walked last,
the messages marker wins and a post-marker messages edit is (correctly)
suppressed; when `system` is walked last, the system marker wins, and the same
messages edit now looks like it sits *before* the breakpoint — a break. The
suppression decision was consuming a swept hypothesis (`order`) as if it were a
physical fact.

## 3. Design chosen

Two orders were conflated; I separated them:

- **`order` / `SEGMENT_ORDER`** — a hypothesis about which component to blame
  *first* when several diverge. Still a swept parameter; `attribute()` still
  walks it to find the first divergence, and `bytes_before`/`bytes_after` are
  still measured in its byte space.
- **`CACHE_LAYOUT = ("tools", "system", "messages")`** — Anthropic's documented
  request layout. Fixes where the cacheable prefix ends. The constant carries a
  comment stating plainly that it encodes an assumption about Anthropic's layout
  and that the suppression rule degrades on a provider that lays the prefix out
  differently.

The suppression rule is structural in `CACHE_LAYOUT`:

- `model` / `params` → never suppressed (they invalidate the whole prefix).
- no breakpoint at all → never suppressed.
- diverging component strictly **after** the component holding the last
  breakpoint (in `CACHE_LAYOUT`) → suppressed.
- strictly **before** → not suppressed.
- **same component** → compare `within >= offset-within-component`.

The last branch reuses `_list_breakpoint_end` / `_messages_breakpoint_end`,
which were already order-independent and measured in the *component's own*
normalised byte space. That is the only place two byte offsets are compared, and
they are then in the same space — so the "two different byte spaces" trap from
the #22 notes does not arise. Cross-component suppression is decided by
`CACHE_LAYOUT` index, never by offsets.

Block granularity is preserved: within `messages`, `_messages_breakpoint_end`
returns the end of the last `cache_control` *content block*, so a
same-message-later-block edit suppresses while an edit before/inside the marker
block does not. This is the granularity #22 established (message granularity
explained 1/26; block granularity held), and I did not revisit it.

## 4. Rejected

- **Compute the breakpoint in the fixed default `SEGMENT_ORDER` regardless of
  sweep order.** Rejected for the reason already recorded in the #22 notes — it
  compares a divergence offset in `order` space against a breakpoint in a
  different space. The structural rule avoids the comparison entirely for
  cross-component cases and confines the same-component comparison to one space.
- **Literal block-index bookkeeping** (return a `(message_idx, content_idx)`
  tuple for both breakpoint and divergence and compare lexicographically). It is
  equivalent to `within >= offset` for every exercised shape, but it needs
  special handling for divergences that are not in `content` (a `role` change, a
  tool-call field, truncation), where there is no clean content-block index. The
  byte-offset form handles those for free because it already runs on the
  normalised component bytes. See §7 for the one edge where they diverge.
- **Keeping `_cache_breakpoint_offset` and re-walking it in `CACHE_LAYOUT`.**
  It returned a single absolute offset; the structural rule needs the *component*
  too, so a `(component, offset)` return is the natural shape. Deleted.

## 5. Acceptance criteria

| AC | status |
|---|---|
| AC1 invariance on real data | **satisfied** — `test_suppression_is_order_invariant_on_real_capture` reads `data/raw/capture.jsonl`, skips (with reason) if absent, and asserts `.suppressed` is identical across all 120 orders for every comparable pair (62). |
| AC2 sweep ties, no best order | **satisfied** — `scripts/calibrate_attributor.py --path data/raw/capture.jsonl` takes the all-tie branch and prints no "best order". Output reproduced in §6. |
| AC3 six counterexamples under every order | **satisfied** — all five "break" tests plus a new `model` change test loop over all 120 orders; `test_change_after_breakpoint_is_suppressed` loops too. |
| AC4 multi-component breakpoints | **satisfied** — new fixture + test: system change before the system marker breaks, messages change after the last messages marker suppresses, both under every order. |
| AC5 assumption written down | **satisfied** — `CACHE_LAYOUT` comment + `report/02-method.md` limitation. |
| AC6 128 existing tests | **satisfied** — 134 passed (128 + 6 new; the new fixture is exercised by the three parametrised fixture/byte tests). |

## 6. Verification

```
$ .venv/bin/python scripts/calibrate_attributor.py --path data/raw/capture.jsonl
records: 75   pairs (by injected_previous_message_id): 63
official signal: usage.cache_read_input_tokens > 0
comparable pairs (signal present): 62
signal buckets:
  no_divergence              62

agreement rate: 100.0%  (62/62)  (all 120 segment orders tie — suppression is decided in cache-layout order)
suppressed (text diverged after the last cache_control block, cache intact): 26

disagreement breakdown:
  none

$ .venv/bin/python -m pytest -q
134 passed in 31.69s
```

The 26 formerly order-flipping pairs now all report `suppressed=True` under every
order; the suppressed total (26) is unchanged from #22, as expected — the fix
changes *which* order agrees, not *what* the correct verdict is.

## 7. Uncertainties

- **Byte-offset vs literal block-index for the same-component branch.** The fix
  text says "compare block index"; I compared `within >= offset-within-component`.
  These agree for every real shape (blocks are contiguous in byte order). The
  single divergence is a `cache_control`-bearing content block with a field
  *after* the `cache_control` key: byte-offset treats that tail as suppressed,
  block-index as a break. I have not found such a block in the data and do not
  believe it is a real API shape; flagging it because it is an unexercised edge,
  not because I think it matters.
- **The `>=` boundary remains unexercised.** As in #22, no real pair lands a
  divergence exactly on the breakpoint byte. `>=` ("on the marker is after") is
  my reading of "prefix is `[0, breakpoint)`".
- **`bytes_before`/`bytes_after` are still order-dependent.** They partition the
  prefix in `order` space (kept per the instructions — other code reads them).
  Under a non-default order, `bytes_before` no longer equals "bytes cached before
  the divergence" in the physical sense. AC1 only requires `.suppressed`
  invariance; I did not assert byte-field invariance because it would be wrong to
  promise it.
- **`CACHE_LAYOUT` order is taken from the module docstring's citation**
  ("tools, system, messages" on the prompt-caching page), which the module
  already recorded before this change. I did not re-verify the doc against
  Anthropic's current page — the comment states the assumption explicitly so a
  change there is a one-line edit.

## 8. Out of scope / noticed

- `diverging_components()` still takes and walks `order`. It reports *which text
  changed*, not whether the cache broke, so leaving it order-aware is correct;
  only the suppression decision had to stop consuming `order`.
- `prefix_bytes()` still takes `order`; unchanged.
- The `Divergence.bytes_before` parenthetical "(cached)" remains imprecise for
  suppressed cases (pre-existing, recorded in the #22 notes). Not touched here.
- Nothing under `proxy.py` / `redact.py` / `codec.py` / `providers.py` /
  `predictions.md` / `data/` was modified.
- **`tests/test_proxy_sse.py` is flaky in this environment, independent of this
  change.** It binds hardcoded ports 8788/8801 via a module-scoped fixture of
  real subprocesses. If a previous run's servers are still shutting down (or a
  stale pair holds the ports), the next run fails with `502 Bad Gateway` and
  state leakage (`/_last` returning another test's body). I hit 9 → 4 → 12
  failures across three runs with no code change to the proxy path; after the
  ports were freed, the full suite passed 134/134. Not touched here, but a
  reviewer re-running may see it.
