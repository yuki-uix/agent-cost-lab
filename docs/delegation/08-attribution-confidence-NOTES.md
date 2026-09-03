# 归因置信度 — delivery notes

## Deliverables

- `src/agentcostlab/attribute.py` — `Divergence` gains `candidates` and
  `order_stability`; the two helpers `_diverged_components()` (every component
  that diverges) and `_canonical_candidates()` (CACHE_LAYOUT order) feed them.
  No comparison logic, no `suppressed` rule, no `SEGMENT_ORDER` default changed.
- `src/agentcostlab/analysis.py` — `CauseBreakdown` gains `ambiguous: CauseCost`;
  `CauseCost` gains `candidates`. `cost_by_cause()` routes
  `order_stability < 1.0` turns into `ambiguous` (priced, candidate list kept),
  never into `by_cause`.
- `tests/test_attribute.py` — 2 new tests (AC1 enumeration, AC3 invariance).
- `tests/test_analysis.py` — 5 new tests (AC4 routing guard, AC5 exhaustiveness,
  3 × AC7 leftovers); 3 existing capture-02 tests updated for the ambiguous bucket.

`ledger.py` / `pricing.py` / `providers.py` / `proxy.py` / `redact.py` untouched.

---

## AC1 — closed form vs. full 120-permutation enumeration

For each `k` a synthetic pair with exactly `k` diverging components is swept over
all `itertools.permutations(COMPONENTS)` (120 orders), and the winner counts are
compared against the closed form `order_stability = 1 / len(candidates)`. The
assertion uses the *measured* maximum elected proportion, not `1/k` copied in twice.

| k | candidates (CACHE_LAYOUT order) | winner counts per 120 | measured max-proportion | closed form | match |
|---|---|---|---|---|---|
| 1 | `('model',)` | `model: 120` | 1.0 | 1.0 | ✓ |
| 2 | `('model', 'system')` | `model: 60, system: 60` | 0.5 | 0.5 | ✓ |
| 3 | `('model', 'tools', 'system')` | `model: 40, tools: 40, system: 40` | 0.333… | 0.333… | ✓ |
| 4 | `('model', 'tools', 'system', 'messages')` | 30 each | 0.25 | 0.25 | ✓ |
| 5 | all five | 24 each | 0.2 | 0.2 | ✓ |

Zero mismatch at every k. **Coverage honesty**: k ≥ 3 is synthetic-only — there
is no real capture with ≥3 diverging components, exactly as the spec's own note
says; the closed form is backed by real data only at k=1 (n=17) and k=2 (n=1).

---

## AC2 — real k distribution on `capture-02.jsonl`

`_diverged_components()` over every pair in the capture:

```
k distribution: {1: 17, 2: 1}
18 diverging pairs of 49 total
```

**Matches the delegator's `{1: 17, 2: 1}` exactly.** The single k=2 pair is the
record-61 break.

---

## AC3 — candidates / order_stability are order-invariant

`test_candidates_and_order_stability_are_order_invariant`: a two-component pair
(`system` + `tools`) is swept over all 120 orders; `candidates` and
`order_stability` are asserted identical every time, and `component` is allowed
to vary (it does — that is the instability being measured). Candidates come back
in CACHE_LAYOUT order `('tools', 'system')`, not in the swept order.

---

## AC4 — multi-candidate turns never enter `by_cause`

`test_a_two_component_break_is_ambiguous_not_charged_to_a_cause` builds a
`system` + `messages` divergence with a ledger-confirmed break and asserts
`by_cause == []`, `ambiguous.turns == 1`, both ends of the dollar interval
present, and `ambiguous.candidates == {'system', 'messages'}`.

Mutation guard: the routing predicate is `elif d.order_stability < 1.0` in
`cost_by_cause()`. Flipping it to always-false would drop the turn into
`cause_state[d.component]` (`by_cause['system']`), and the `by_cause == []`
assertion fails — verified by construction, not by a checklist.

---

## AC5 — the three buckets are exhaustive and disjoint

`test_the_three_buckets_are_exhaustive_and_disjoint` runs three breaks through
one capture — one single-cause (`by_cause['system']`), one two-component
(`ambiguous`), one identical-bodies (`unattributed`) — and asserts
`sum(by_cause.turns) + ambiguous.turns + unattributed.turns == 3` with repaid
tokens summing to 1,200 and `disputed == not_measured == 0`. The existing
`test_capture_02_repaid_tokens_is_exactly_2822` asserts the same equality on
capture-02 (sum == 1 broke turn).

---

## AC6 — red-line numbers (and a spec error)

The three red-line numbers are preserved **digit-for-digit**, but the bucket they
land in changes:

```
repaid_tokens = 2822
usd_low       = 0.0050796
usd_high      = 0.0055986
```

**Spec error — AC6 says the break is k=1 and must stay in `by_cause`; it is
actually k=2 and lands in `ambiguous`.** See §规格错误 below. The money is real
either way, so #51's "no dollars" wording is corrected without dropping a cent.

---

## AC7 — the three #50 leftovers

- `test_three_bucket_overflow_refuses` — all three non-cache-read buckets carry
  tokens (input 100 + 5m 50 + 1h 50) but capacity 200 < repaid 1,000 → raises
  `RepaidExceedsCapacity`, message names `1000` and `200`.
- `test_no_non_cache_read_bucket_refuses` — repaid 400 with all three buckets at
  zero capacity → raises `RepaidExceedsCapacity` naming `400` and `0`.
- `test_every_priced_bucket_satisfies_zero_le_low_le_high` — seeded
  (`random.Random(20260903)`) 200-turn sweep over random usage, asserts
  `0 <= usd_low <= usd_high` on every priced turn.

---

## Test counts (actual, not "all green")

`233 passed, 10 skipped`. Baseline before this task was `226 passed, 10 skipped`;
**7 tests added** (2 in `test_attribute.py`, 5 in `test_analysis.py`), 3 existing
capture-02 tests updated.

The 10 skips, each naming the file it is missing:

| count | file | missing |
|---|---|---|
| 1 | `tests/test_attribute.py` | `data/raw/capture.jsonl` |
| 1 | `tests/test_calibrate.py` | `data/raw/capture.jsonl` |
| 5 | `tests/test_compaction_payback.py` | `data/raw/capture-03.jsonl` |
| 1 | `tests/test_compaction_payback.py` | `data/raw/capture-01.jsonl` |
| 1 | `tests/test_gates.py` | `data/raw/capture-03.jsonl` |
| 1 | `tests/test_proxy_sse.py` | `data/raw/capture-03.jsonl` |

(`capture-02.jsonl` is present and used; `capture.jsonl`, `capture-01.jsonl`,
`capture-03.jsonl` are absent from this worktree.)

---

## 规格里不对、或没能验证的地方

### 1. AC6 断言那次断裂是 k=1，实为 k=2 —— 红线数字保住了，但桶变了

规格 AC6 写：「那次断裂是 k=1（只有 `system` 分歧），所以它仍然在 `by_cause`
里，不进 `ambiguous`」。**按「忠实于数据」的候选定义，这是错的。**

record 61（`msg_011CeBNtUT43pFA8X6ZbUQtK`）这一对请求有 **两个** 组件分歧：

| 组件 | 分歧 | 字节差 | directive 差 |
|---|---|---|---|
| `system` | `ttl: "1h"` 被删 | ✓ | `{"type":"ephemeral","ttl":"1h"}` → `{"type":"ephemeral"}` |
| `messages` | 一个 `tool_use` 块的 `cache_control` 同样丢了 `ttl: "1h"` | ✓ | 同上 |

关键机制：`_normalise_content` 只从 **`text` 块**剥 `cache_control`，**不剥
`tool_use`/`tool_result` 块**。所以 `messages` 里 tool_use 块的 marker 变化既
是字节差、也是 directive 差，被 `_diverged_components` 如实计为分歧。全枚举
下 `system` 与 `messages` 各当选 60/120，`order_stability == 0.5`。

`attribute()` 之所以在默认 `SEGMENT_ORDER` 下报 `system`，只是因为 `system` 排在
`messages` 前——这正是本任务要暴露的那类「未验证假设的产物」。因此忠实实现把这一轮
放进 `ambiguous`（候选 `('system','messages')`），**不进 `by_cause`**。三个红线数字
（2822 / 0.0050796 / 0.0055986）原样保留在 `ambiguous` 桶里——钱是真的，#51 的
「不报美元」被修正，一分不丢。

结论：AC6 的「k=1、留在 by_cause」与真实数据矛盾；实现选择忠实于数据，红线数字仍
逐位达标。这一条请验收方裁定是按「红线数字」还是按「k=1」判——两者不可兼得。

### 2. 守恒式 / 归因都只按 Anthropic usage 形状成立，本次未动

与 07 一样：`broke_cache` 与 `_repaid` 读 Anthropic 的 `cache_read_input_tokens` /
`cache_creation_input_tokens`，跨 provider 仍是未覆盖面（规格已明确划给另一任务）。
本次没碰这五块，也不属 AC 范围，仅记录。

### 3. `ambiguous.candidates` 是去重后的集合

若同一成因在多轮里以不同组合出现，`ambiguous` 桶聚合的是所有候选的并集，再按
CACHE_LAYOUT 排一次序。因此 `ambiguous.candidates` 描述的是「整份 capture 里出现过的
多候选集合」，不是某一轮的候选。这与 `by_cause` 按成因聚合的语义一致，但一个
`ambiguous.turns >= 2` 的 capture 无法从 `candidates` 反推出「哪两个一起出现」——
规格没有要求轮级候选明细，仅记录，供将来判断是否需要。
