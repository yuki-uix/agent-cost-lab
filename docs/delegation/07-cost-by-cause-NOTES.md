# cost-by-cause — delivery notes

## Deliverables

- `src/agentcostlab/analysis.py` — `cost_by_cause()` + the `CauseCost` /
  `CauseBreakdown` dataclasses. Read-only consumer of `ledger` / `attribute` /
  `pricing` / `providers`; none of those four were touched.
- `tests/test_analysis.py` — 13 tests, every one calls `cost_by_cause()` (AC6),
  offline except the one real-capture test that skips and names its file (AC7).

## AC1 — record 61 hand reconciliation (the hard gate)

`cost_by_cause()` over `data/raw/capture-02.jsonl` returns `repaid_tokens` of
**exactly 2,822**, matching the report (`report/03-e1-miss-reasons.md`): expected
read 157,991, actual read 155,169, difference 2,822.

The conservation identity is `ledger.broke_cache`'s own rule, applied to record 61
against its actual predecessor (record 60, `msg_011CeBNt9KcUTBzwtWst943S`):

```
expected = prev.cache_read + prev.cache_creation
         = 155,169 + 2,822
         = 157,991
repaid   = expected − curr.cache_read
         = 157,991 − 155,169
         = 2,822                  ✓
```

**Dollar interval — hand arithmetic.** Rate entry `anthropic/claude-sonnet-5`
(`fixtures/pricing.json`, `retrieved_at: 2026-08-19`, `verified: true`, source
<https://platform.claude.com/docs/en/about-claude/pricing>):

| bucket | USD / MTok |
|---|---|
| `input_uncached` | 2.00 |
| `cache_read` | 0.20 |
| `cache_write_5m` | 2.50 |
| `cache_write_1h` | 4.00 |

Record 61's usage carries `input_uncached` 3,780 and `cache_write_5m` 1,038, and
**no 1h write** (`ephemeral_1h_input_tokens: 0`). So the two non-cache-read buckets
actually present that turn are input (2.00) and the 5m write (2.50); the 4.00 1h
rate is correctly *not* available as an upper bound. The loss per token is the
bucket rate minus the cache-read rate the token would have paid had the prefix
survived:

```
usd_low  = 2,822 × (2.00 − 0.20) / 1,000,000
         = 2,822 × 1.80 / 1,000,000
         = 5,079.6 / 1,000,000
         = 0.0050796                 ✓

usd_high = 2,822 × (2.50 − 0.20) / 1,000,000
         = 2,822 × 2.30 / 1,000,000
         = 6,490.6 / 1,000,000
         = 0.0064906                 ✓
```

**Digit-by-digit against the function output:**

```
function: CauseCost(cause='system', turns=1, repaid_tokens=2822,
                    usd_low=0.0050796, usd_high=0.006490599999999999)
```

`usd_low` matches to all 8 printed digits. `usd_high` prints as
`0.006490599999999999` because `2.30` is not exactly representable in IEEE-754
double (`2822 * 2.3 == 6490.599999999999`); `0.006490599999999999` *is* the
double nearest to the hand value 0.0064906. The two agree; the tail is floating
point, not a discrepancy.

## AC2 — attribution of the break

The record-61 break is attributed by `attribute()` to **`system`** — detail
`field removed: 'ttl'`, path `['system', 1, 'cache_control', 'ttl']`,
`suppressed=False`. It therefore lands in `by_cause` under `system`, **not** in
`unattributed` (`unattributed.turns == 0`).

The mechanism is the TTL switch: 65 of 66 records carry `ttl: "1h"`; record 61
dropped it, so the 1h bucket's tail was not found in the new 5m bucket. The
attributor's `cache_control`-directive comparison catches this, so no special
case was added anywhere to force it into a cause. This is a *real* attribution,
which is why the honest `unattributed` bucket stays empty.

## AC3 — both ends of the interval appear

`test_interval_spans_when_two_buckets_are_present` builds a turn whose usage
carries both `input_uncached` and a `cache_write_1h` write, and asserts
`usd_high > usd_low` (1.9× the read rate vs 0.9×). `test_a_single_bucket_degrades_to_a_point`
covers the one allowed point estimate. No output prints a midpoint anywhere.

## AC4 — the bytes/tokens unit gate

`test_bytes_and_tokens_never_share_a_numeric_column` enumerates `Divergence`'s
byte fields (`bytes_before` / `bytes_after`) and asserts neither `CauseCost` nor
`CauseBreakdown` carries a field with those names. It iterates the actual field
lists, so a future byte column in an output type fails the suite rather than
slipping through a checklist.

## AC5 — unverified rates and unpriceable writes propagate

`test_unverified_rate_raises_instead_of_a_partial_result` feeds an unverified
rate and asserts `UnverifiedRate`; `test_an_unpriceable_cache_write_propagates`
feeds a write with no TTL and asserts `AmbiguousCacheWrite`. Neither is caught.

One implementation note for the reviewer: the verified/unverified check is
*re-stated* in `analysis._verified_rate` (mirroring the top of `pricing.cost`),
because `cost()` only validates when handed a `Usage` and this module must refuse
before it has one. The per-turn `AmbiguousCacheWrite` gate is *not* re-stated —
`_loss_interval` calls `cost()` on the turn's usage so that gate stays in one place.

## AC6 — tests call the real function

All 13 tests call `cost_by_cause()` and assert its output against hand-computed
or externally-recorded numbers. None re-implements the bucket sweep.

## AC7 — offline

No test touches the network. The single real-capture test
(`test_capture_02_repaid_tokens_is_exactly_2822` + siblings) reads
`data/raw/capture-02.jsonl` and skips, naming that file, when absent — it was
present here, so it ran (not skipped).

## Test counts (actual, not "all green")

`218 passed, 10 skipped`. The 13 new tests all pass.

The 10 skips, each with the file it is missing:

| count | file | missing |
|---|---|---|
| 1 | `tests/test_attribute.py::…real capture…` | `data/raw/capture.jsonl` |
| 1 | `tests/test_calibrate.py::…` | `data/raw/capture.jsonl` |
| 5 | `tests/test_compaction_payback.py::…` | `data/raw/capture-03.jsonl` |
| 1 | `tests/test_compaction_payback.py::…` | `data/raw/capture-01.jsonl` |
| 1 | `tests/test_gates.py::…end to end…` | `data/raw/capture-03.jsonl` |
| 1 | `tests/test_proxy_sse.py::…` | `data/raw/capture-03.jsonl` |

(`capture-02.jsonl` is present; `capture.jsonl`, `capture-01.jsonl`,
`capture-03.jsonl` are not in this worktree.)

## Side number, not an AC

`hit_usd_saved` for `capture-02` is `7.674973200000001` — the cache-read discount
(2.00 − 0.20 per MTok) summed over every read across the 66 records. It is
reported apart from the losses and never netted against them.
