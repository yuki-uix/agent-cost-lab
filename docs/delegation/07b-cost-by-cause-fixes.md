# 委托任务 07b — cost_by_cause 的三处修正

**执行方**：DeepSeek（复用 worktree `.claude-worktrees/cost-by-cause`，分支 `feat/cost-by-cause`）
**验收方**：Claude
**对应 PR**：#56（changes requested）

---

## 背景

#50 的实现已合规交付并通过验收，随后的代码评审找出三处正确性问题，验收方逐条独立复算，**三条全部成立**。

**其中 P1 是规格的错，不是实现的错**——规格写的是「全部 repaid 落进该轮最贵的桶」，
漏掉了桶的容量约束。执行方严格照规格实现了。这份修正单纠正规格本身。

只改 `src/agentcostlab/analysis.py` 和 `tests/test_analysis.py`。
**`ledger.py` / `attribute.py` / `pricing.py` / `providers.py` / `proxy.py` / `redact.py`
仍然不许动。**

---

## P1 · 区间必须受每个桶的实际容量约束

### 问题

`_loss_interval()` 现在把**全部** `repaid` 个 token 放进最便宜或最贵的那个非零桶，
不管那个桶这一轮实际只计费了多少个 token。

记录 61：`repaid = 2822`，而桶的实际容量是 `input_uncached = 3780`、`cache_write_5m = 1038`。
**把 2,822 个 token 放进一个只计费了 1,038 个的桶里，物理上不可能。**

### 正确算法

按单价排序后**贪心装填**，每个桶最多装它自己的容量：

- `usd_high`：**最贵的桶先装满**，装不下的溢到次贵的，依此类推
- `usd_low`：**最便宜的桶先装满**，同理

记录 61 的正确上界：

```
1038 × (2.50 − 0.20)  = 2387.4      （5m 桶装满）
1784 × (2.00 − 0.20)  = 3211.2      （剩下的溢到 input_uncached）
                        ────────
                        5598.6 / 1e6 = 0.0055986
```

下界不变：`input_uncached` 容量 3780 ≥ 2822，全部装得下 → `2822 × 1.80 / 1e6 = 0.0050796`。

**当前实现的上界 0.0064906 高估约 16%。**

### 容量不足时必须拒绝

若**全部非 cache_read 桶的 token 数之和 < repaid**，说明这些 token 不可能都在这一轮被重新计费，
守恒式的前提不成立。

**抛出异常拒绝出数，不要按比例缩放、不要 clamp、不要只用装得下的部分算。**
这是 kill criterion 的同一条：算不出来就说算不出来。

### AC

- `test_capture_02_interval_matches_the_hand_calc` 的期望值改为 `usd_high == 0.0055986`
  （下界 `0.0050796` 不变），并把上面那段贪心算式写进 docstring
- 新增测试：`repaid` 超过全部非 cache_read 桶容量之和 → 抛出，且异常信息里同时出现
  `repaid` 与容量合计两个数字
- 新增测试：`repaid` 恰好等于容量合计 → 不抛，且 `usd_high` 等于「每个桶按各自容量装满」的手算值
- 新增测试：最贵的桶容量为 0 时，它不参与上界（既有行为，补测）
- 既有的两桶/单桶测试按新算法更新期望值，**期望值必须是手算常量，不许从被测代码回读**

---

## P2 · 费率必须对每条记录校验

### 问题

`cost_by_cause(records, model_key)` 拿一个 `model_key` 给**全部**记录定价，而
`normalise()` 是按每条记录自己的 `provider` 取数的。于是：

- 混模型 / 混 provider 的 capture 被单一费率静默计价
- 即使是单一模型的 capture，调用方传错 key 也不会有任何提示

验收方实测：一条 anthropic + 一条 deepseek 的 capture，DeepSeek 那轮**按 Anthropic 费率
出了价，零告警**。评审实测 10x 的模型错配产生 `$0.0009` 而非 `$0.009`。

capture-02 是 66 条全 `claude-sonnet-5`/anthropic，所以既有数字不受影响——
**但这是一条静默出错的路径，不是报错的路径。**

### 要求

在开始计价之前，校验每条记录与 `model_key` 一致：

- 记录的 `provider` 必须与 `model_key` 的 provider 前缀一致（`anthropic/claude-sonnet-5` → `anthropic`）
- 记录的 `request_body["model"]` 若存在，必须与 `model_key` 的模型部分一致
- 任一条不一致即抛出，异常信息点名**第几条记录**、它的 provider/model、以及传入的 `model_key`

不要「跳过不一致的记录继续算」——那是另一种静默少报。

### AC

- 新增测试：混 provider 的记录 → 抛出，异常信息含记录下标与两边的值
- 新增测试：单一 provider 但 `model_key` 的模型部分对不上 → 抛出
- 新增测试：`request_body` 里没有 `model` 字段的记录不因此被拒（只校验存在的字段）
- capture-02 走 `anthropic/claude-sonnet-5` 仍然通过，AC1 的 2822 不变

---

## P3 · 非 Anthropic provider 必须显式拒绝

### 问题

`ledger.broke_cache()` 与 `_repaid()` 读的是 Anthropic 的原始 usage 键
（`cache_read_input_tokens` / `cache_creation_input_tokens`）。DeepSeek 的 payload 用的是
`prompt_cache_hit_tokens` 等，于是两边都取到 0，`0 != 0` 为假，**真实断裂被判成「没断」**。

验收方实测：DeepSeek 形状、预期读 1,200 实际命中 800 的一对记录，
`broke_cache()` 返回 `False`；同一对数据归一化之后按同一条守恒式算是断了 400 个 token。

### 修法（这一条按这个来，不要自行选另一种）

**不要在 `analysis.py` 里对归一化后的 `Usage` 重算守恒式。**

理由是 `ledger.py` 自己的模块 docstring：它存在的意义就是让这个判据只有一份定义，
「三份拷贝会漂移，届时测试拿一条规则验归因器、脚本报的是另一条，没人察觉」。
在 analysis 里重算就是第四份。

**本任务只做一件事：遇到非 `anthropic` 的 provider，显式抛出拒绝出数**，
异常信息要说明原因是守恒式目前只对 Anthropic 的 usage 形状成立，并指向后续 issue。

让守恒式本身跨 provider 是另一个任务（要改 `ledger` 及其三个调用点），不在本次范围。

### AC

- 新增测试：DeepSeek 形状的记录 → 抛出，异常信息里出现 provider 名与「守恒式」相关说明
- 新增测试：Anthropic 记录不受影响
- 异常类型自选但要有名字（不要裸 `ValueError`），并在模块 docstring 里说明这是暂时限制

---

## 明确不要做的事

- 不要 `git push`，不要开 PR，不要用 `gh`（验收方来做）
- 不要改那六个模块
- 不要碰 `predictions.md` / `fixtures/pricing.json`
- 不要引入新依赖
- 不要为了让某个数字好看而做特判
- commit message 全英文，格式 `type: description`

## 交付要求

追加写进 `docs/delegation/07-cost-by-cause-NOTES.md`（不要新建文件，接在原文后面），
标题 `## 07b 修正`，包含：

1. **P1 修正后 capture-02 的完整手算**：贪心装填的每一步、每个桶装了多少、单价、小计
2. 三条各自新增了哪些测试、测试数变化（`N passed / M skipped`）
3. 你认为规格里仍然不对、或没能验证的地方，如实写出来

**不要声称「全部通过」。把数字贴出来。**
