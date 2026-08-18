# prefix-attributor — delivery notes

## Deliverables

- `src/agentcostlab/attribute.py` — `Divergence` + `attribute()`, plus
  `prefix_bytes()` and `diverging_components()` helpers.
- `tests/test_attribute.py` — 49 tests, every one calls `attribute()` (AC4);
  offline and independent of `data/raw/` (AC5).
- `fixtures/attribution/*.json` — 12 hand-crafted request pairs.
- `scripts/calibrate_attributor.py` — permutation sweep + agreement rate +
  disagreement classification.

## 1. Segment-order experiment — **not yet measurable**

`data/raw/capture.jsonl` is gitignored and does **not exist in this worktree**,
so the real per-permutation agreement rates and the real AC1 number **cannot be
computed here**. Do not treat this delivery as "AC1 passed". Run the calibration
against real data:

```
.venv/bin/python scripts/calibrate_attributor.py --path data/raw/capture.jsonl
```

What I *did* verify and what I believe the answer is:

- The permutation sweep works. I ran it against a synthetic `capture.jsonl`
  (request bodies + official `diagnostics` I wrote by hand, `11` records /
  `10` adjacent pairs). It ranks the orders correctly and finds the intended
  one as the top hit.
- The two official docs disagree on the tools/system order — which is exactly
  the ambiguity this task wants measured:
  - prompt-caching doc: the cached prefix is `tools`, `system`, `messages`
    "(in that order)".
  - cache-diagnostics doc: `unavailable` is phrased as "`model`, `system`, and
    `tools` match but another prompt-affecting parameter differs".
- Default `SEGMENT_ORDER` is therefore my best guess, **not a measured fact**:
  `model → tools → system → messages → params` (model is the per-model
  namespace, so it is outermost; params last, matching `unavailable`).

On the synthetic smoke data the default order scores **100% (7/7)** and the
second-best family scores **85.7% (6/7)** — but that data was written to match
the default order, so this is a plumbing check, not evidence.

## 2. AC1 (agreement rate) — **not measured**

No real data ⇒ no real number. The calibrate script prints `AC1 = …%` from
whatever `--path` is given. The number above (100%) is synthetic and must not be
quoted as the AC1 result.

## 3. Disagreement classification (AC2)

The calibrate script buckets every pair. Comparable pairs (official
`*_changed` or `no_divergence`) drive AC1. These are excluded and reported
separately, per the spec:

- `unavailable` — the API couldn't pinpoint; we break it down further by what
  `attribute()` found (I expect `params` to dominate, since a params-only change
  surfaces as `unavailable`).
- `previous_message_not_found` — no stored fingerprint; no ground truth.
- `pending` — `cache_miss_reason: null`, comparison still running.
- `first_turn` / `cross_session` — no valid official comparison for the pair.

For comparable disagreements the script further classifies each row as
`missed`, `over-reported`, `both changed`, `official-early-only`, or `mismatch`,
using `diverging_components()` to distinguish "both components really diverged,
we attributed a different one" from "we missed a change".

## 4. What I am unsure about

- **The real segment order and the real AC1 number** — unmeasurable without
  `data/raw/capture.jsonl`. This is the single biggest open item.
- **`params` membership.** I treat `max_tokens`, `temperature`, `top_p`,
  `top_k`, `stop_sequences`, `metadata`, `stream`, `diagnostics` as
  non-cache-affecting (ignored), which matches the prompt-caching doc's
  invalidation table (it lists tool/web-search/citations/speed/tool_choice/
  images/thinking/effort, not sampling knobs). Everything else top-level —
  `tool_choice`, `thinking`, `context_management`, `output_config`,
  `output_format`, `cache_control`, `web_search`, `citations`, `speed`, and any
  unknown field — lands in `params`. If a field I ignored turns out to be
  cache-affecting, this will show up as `missed` rows against real data.
- **Sub-ordering inside `params`.** The sweep permutes the five top-level
  segments; the order of fields *within* `params` is left as the body's own
  JSON key order and is not independently swept.
- **`unavailable` from very long conversations.** Official reports `unavailable`
  when the divergence is beyond its comparison horizon; my attributor will still
  report a concrete component. Those rows land in the known-reasonable bucket,
  but they are a place where my answer can be *more* specific than the API's —
  worth eyeballing, not counting against AC1.
- **Byte-accounting semantics for removals/truncation.** `bytes_before +
  bytes_after == len(prefix_bytes(curr))` holds by construction (AC6). For a
  truncation, `bytes_after` is small (only the re-serialised tail of the
  current, shorter request is "repriced"); the dropped history lives in the
  *previous* request's bytes, not the current one's. I measure on the current
  request because that is what the cache re-prices.
