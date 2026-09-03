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

## 07b 修正

三处修正都落在 `src/agentcostlab/analysis.py` + `tests/test_analysis.py`，六个上游模块未动。

### P1 修正后 capture-02 的完整手算

费率 `anthropic/claude-sonnet-5`（`fixtures/pricing.json`，`retrieved_at: 2026-08-19`，
`verified: true`）：input 2.00，cache_read 0.20，cache_write_5m 2.50，cache_write_1h 4.00。

记录 61 这一轮的 usage：`input_uncached = 3780`，`cache_write_5m = 1038`，
`cache_write_1h = 0`（无 1h 写入）。`repaid = 2822`（守恒式不变，仍是 2822）。

非 cache_read 桶按单价贪心装填，每个桶最多装自己的容量：

**下界 usd_low（最便宜的先装满）：**

| 顺序 | 桶 | 单价 (USD/MTok) | 容量 | 装入 | 单价 − cache_read | 小计 |
|---|---|---|---|---|---|---|
| 1 | input_uncached | 2.00 | 3780 | 2822（全装下，3780 ≥ 2822） | 1.80 | 2822 × 1.80 = 5079.6 |

```
usd_low = 5079.6 / 1e6 = 0.0050796   （不变）
```

**上界 usd_high（最贵的先装满）：**

| 顺序 | 桶 | 单价 (USD/MTok) | 容量 | 装入 | 单价 − cache_read | 小计 |
|---|---|---|---|---|---|---|
| 1 | cache_write_5m | 2.50 | 1038 | 1038（装满） | 2.30 | 1038 × 2.30 = 2387.4 |
| 2 | input_uncached | 2.00 | 3780 | 1784（溢出：2822 − 1038） | 1.80 | 1784 × 1.80 = 3211.2 |

```
合计 = 2387.4 + 3211.2 = 5598.6
usd_high = 5598.6 / 1e6 = 0.0055986   （旧实现 0.0064906，高估约 16%）
```

容量校验：3780 + 1038 = 4818 ≥ 2822，不触发拒绝。

**函数输出对账：**

```
CauseCost(cause='system', turns=1, repaid_tokens=2822,
          usd_low=0.0050796, usd_high=0.0055986000000000005)
```

`usd_high` 打印为 `0.0055986000000000005`，是 IEEE-754 double 里最接近
`5598.6 / 1e6` 的那个数（`5598.6` 不是二进制精确表示）；与手算值
0.0055986 逐位一致，尾部是浮点，不是偏差。

### 三条各自新增的测试、测试数变化

**P1（桶容量约束）新增 3 条**（`tests/test_analysis.py`）：

- `test_repaid_exceeding_bucket_capacity_refuses` — repaid 400 超过容量合计 150，
  断言抛 `RepaidExceedsCapacity` 且异常信息同时含 `400` 与 `150`。
- `test_repaid_equal_to_capacity_fills_every_bucket_to_the_brim` — repaid 150 恰好
  等于容量合计（input 100 + 5m 50），不抛，`usd_high` 等于「每桶装满」手算值
  `(100×0.90 + 50×1.15)/1e6`。
- `test_a_zero_capacity_bucket_never_sets_the_bound` — 1h 桶单价 2.00 但容量 0，
  不参与上界（既有行为，补测）。

既有 `test_capture_02_interval_matches_the_hand_calc` 的 `usd_high` 期望由
`0.0064906` 改为 `0.0055986`，docstring 写进上面的贪心算式。另有三条既有区间测试
（两桶/单桶/ unattributed）的桶容量从不足 repaid 改到 ≥ repaid，期望值仍为手算常量。

**P2（费率逐条校验）新增 3 条**：

- `test_mixed_provider_records_refuse_and_name_the_record` — 混 provider 抛
  `RecordRateMismatch`，异常含 `record 1` 与两边的 provider 值。
- `test_a_mismatched_model_refuses` — 单一 provider 但模型部分对不上抛
  `RecordRateMismatch`。
- `test_a_record_without_a_model_field_is_not_rejected` — `request_body` 无 `model`
  字段的记录不被拒，整条流水线仍正常出数。

为让 P2 校验成立，测试 fixture 的 `SIMPLE` 主键由 `"x/y"` 改为
`"anthropic/claude-sonnet-5"`（与 `rec()`/`body()` helper 的 provider/model 对齐），
并新增一条 verified 的 `"deepseek/deepseek-chat"` 供 P3 用。

**P3（非 Anthropic 显式拒绝）新增 2 条**：

- `test_a_deepseek_record_refuses_naming_the_conservation_limit` — DeepSeek 形状
  （provider=deepseek，usage 为 hit/miss 键）抛 `NonAnthropicProvider`，异常含
  `deepseek` 与 `conservation`。
- `test_anthropic_records_are_unaffected_by_the_provider_gate` — Anthropic 记录不受影响。

**测试数变化：** `218 passed, 10 skipped` → **`226 passed, 10 skipped`**（新增 8 条，
skip 数与理由不变，仍全部点名缺哪个 capture 文件）。

### 我认为规格里仍然不对、或没能验证的地方

1. **P3 的闸只看 `provider` 字段，不看 usage 形状。** 一个 `provider="anthropic"`
   但 usage 键是 DeepSeek 形状的记录会通过这道闸，然后 `broke_cache` 两边读到 0，
   被判成「没断」——同一条静默路径换个伪装又回来了。规格明确把「守恒式跨 provider」
   划给另一个任务，所以这符合规格；但按本仓库「出口闸保证性质、不靠 prompt」的纪律，
   真正要守的闸在 usage 形状上，不在 provider 标签上。记录下来，不属本次范围。

2. **`provider` 字段缺失的记录会被 P2 以「provider None 不匹配」拒绝，而不是一个
   「缺 provider」的专门错误。** 语义上没错（没法校验就不出数），但错误信息会把
   `None` 当作一个值来点名，读起来像「provider 是 None」而不是「没提供 provider」。

3. **P1 的「无桶」情形被并入了容量检查。** 原来 `_loss_interval` 对「没有非 cache_read
   桶」有一条独立的 `ValueError`；现在这个情形（repaid > 0、容量合计 = 0）由
   `RepaidExceedsCapacity` 以「carried 0 tokens」一并覆盖，少了一条分支。测试未专门
   覆盖「单桶都没有」这条边（现有测试里每条断轮至少有一个非零桶），但 `repaid > 0`
   时 `0 < repaid` 恒真，行为与容量不足一致，不是静默放过。
