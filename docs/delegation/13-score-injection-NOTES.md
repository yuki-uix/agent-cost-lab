# 委托任务 13 交付说明 — 打分脚本在真实数据上崩溃

一个 commit：`fix: handle attribute()'s single Divergence in score_injection`。

改动只落在 `scripts/score_injection.py` 和新增的 `tests/test_score_injection.py`。
`src/agentcostlab/` 下 `attribute.py` / `ledger.py` / `analysis.py` / `pricing.py` /
`providers.py` 一行未动（`git diff --stat` 只有 `scripts/score_injection.py`）。

修的是脚本第 133 行：`attribute()` 的签名是 `-> Divergence | None`，返回**单个**
`Divergence`，脚本却写 `getattr(result, "divergences", result)` 当成可迭代集合去解包。
改为 `attributed_component()` 直接处理单值：

```python
result = attribute(prev_body, curr_body)
if result is None or result.suppressed:
    return None
return result.component
```

`suppressed` 的分歧仍排除在归责之外（AC3 第三条）。

---

## 一、两份真实 fixture 的输出全文

### `fixtures/injection/capture-inject-i0.jsonl`（baseline，3 条，1 对可配对）

```
capture : fixtures/injection/capture-inject-i0.jsonl
fault   : i0   expected component: (none — baseline)
records : 3

tally
  A: not fired, blamed tools         1
  B: intact                          1

no pairwise disagreement
```

### `fixtures/injection/capture-inject-i3.jsonl`（I3 arm，4 条，故障从未开火）

```
capture : fixtures/injection/capture-inject-i3.jsonl
fault   : i3   expected component: tools
records : 4

tally
  A: not fired, blamed tools         1
  B: intact                          1

fault never fired: no turn in this capture recorded `injection.applied: true`, so the arm's fault was never armed and there is nothing to attribute.

no pairwise disagreement
```

两份都**不抛异常、退出码 0**。i3 明确打印「fault never fired」，没有伪造开火状态、没有假装打分。

---

## 二、三份既有 capture 的拒绝信息（闸未弱化）

三份都**退出码 2**，信息逐字不变：

```
data/raw/capture-02.jsonl: no `injection` field on any record. This capture does not declare a campaign arm and cannot be scored as one. Re-capture with AGENTCOSTLAB_INJECT set (use i0 for the baseline).
```

```
data/raw/capture-03.jsonl: no `injection` field on any record. This capture does not declare a campaign arm and cannot be scored as one. Re-capture with AGENTCOSTLAB_INJECT set (use i0 for the baseline).
```

```
data/raw/capture.jsonl: no `injection` field on any record. This capture does not declare a campaign arm and cannot be scored as one. Re-capture with AGENTCOSTLAB_INJECT set (use i0 for the baseline).
```

变异守护：`test_a_capture_without_injection_is_rejected_not_skipped` 断言 `code == 2` 且
stderr 逐字等于上面这句话、stdout 为空。把闸改成静默跳过（`return 0`、不打消息）这条测试会红。

---

## 三、测试数变化

- 基线：**319 passed**。
- 现在：**327 passed, 0 failed**（`.venv/bin/python -m pytest -q`）。
- 净增 **+8**，全部在新增的 `tests/test_score_injection.py`：

  - `test_attributed_component_names_a_real_break`（AC3：单个非 suppressed Divergence → blamed）
  - `test_attributed_component_is_none_when_nothing_diverged`（AC3：None → None）
  - `test_attributed_component_excludes_a_suppressed_divergence`（AC3：suppressed 不计入归责）
  - `test_score_attributes_a_single_divergence_without_throwing`（AC3：整条记录走 `score()`，复现原崩溃的那条路径不抛）
  - `test_a_capture_without_injection_is_rejected_not_skipped`（AC2：拒绝闸 + 变异守护）
  - `test_a_pairwise_disagreement_is_printed_not_reconciled`（AC4：C 说 `system`、归因器说 `messages`，三方统计分开、分歧打印不调和）
  - `test_an_armed_fault_that_never_fired_is_reported`（AC1：i3 未开火 → 打印「fault never fired」）
  - `test_baseline_does_not_report_never_fired`（AC1：i0 baseline 不打印该通知，只报对照结果）

---

## 四、我认为规格不对 / 没能验证的地方

1. **两份「真实 fixture」的唯二可配对 pair，都带一次有机 tool 变更，归因器都 blame 了 `tools`。**
   不是脚本的 bug，是数据本身：i0 和 i3 各自的 1 对可配对 pair 里，
   `attribute()` 返回 `tool replaced at index 24: 'WaitForMcpServers' -> 'WebFetch'`
   （`suppressed=False`，真断裂）。两个 arm 都没 `applied` 任何故障（i0 是 baseline、i3 没到阈值），
   所以脚本如实记成 `A: not fired, blamed tools`——这正是 4.5（baseline 无假阳性）要数的候选。
   规格把这两份 fixture 描述为「baseline」和「故障从未开火」，但没预告它们自带一次工具清单变更。
   **结论：这不是「归因错误」，而是真实假阳性样本，脚本行为是对的。**

2. **「任何两方分歧要打印」这句话，当前实现只覆盖了部分方向。** 归因器（`blamed`）与 B
   （ledger）直接打架时，没有一条 disagreement 行。i0 那份 pair 里归因器说 `tools` 断了、
   B 说 `intact`（`cache_read` 0→10240 是增长不是收缩），这个 A↔B 方向的分歧只落成两行 tally
   （`A: not fired, blamed tools` + `B: intact`），没有进 `disagreements` 列表。现在的列表只有
   「injected vs attributed」（fired 时）和「official vs attributed/ledger」两条。
   规格第三节说「语义不许改」，所以我**没有**补 A↔B 这条边，只如实记录：docstring 里
   「any pairwise disagreement is printed」和实现之间有一条缺口，要不要补由验收方定。

3. **「fault never fired」的触发条件规格没写死，我选了 `expected is not None`。** 即只有非
   baseline arm（i1–i5）在 `fired_turns == 0` 时才打印；i0（baseline，`expected is None`）不打，
   只报对照结果。这符合 AC1 的字面（i0 报「baseline 对照结果」、i3 报「未开火」），但规格没
   明说这条 gate 条件，特此记录。

4. **fixture 的 provider 标签与 usage 形状不一致。** 两份 fixture 记录标 `provider: "deepseek"`、
   模型 `deepseek-v4-pro`，但 `usage` 是 Anthropic 形状（`cache_read_input_tokens` /
   `cache_creation_input_tokens` / `input_tokens` / `service_tier`）。ledger 按形状解析（#63），
   所以 B 按 Anthropic 读数、判「intact」。这是 #63 确立的「形状不是标签」在做它该做的事，
   不是我该改的，但读 NOTES 的人要知道 B 是从哪套键算出来的。

5. **样本量 n=1。** 每份 fixture 只有 1 对可配对（i0 的 record 0→1，i3 的 record 2→3；
   其余记录是另一条 lineage 或 `response_id` 缺失、`injected_previous_message_id` 为 null）。
   按度量纪律，n=1 不下总量结论——本任务只要求「跑通并如实报告」，上面贴的 tally 是单 pair 结果，
   不是 E4 的 arm 结论。
