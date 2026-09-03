# 委托任务 08 — 归因带候选集与置信度

**执行方**：DeepSeek（独立 worktree `.claude-worktrees/confidence`，分支 `feat/attribution-confidence`）
**验收方**：Claude
**对应 issue**：#51

---

## 背景

`cost_by_cause()`（#50，已合并）现在能说「因为 `system` 变了，这个月多花了 $X」。
问题在于**那个「因为 `system`」有时候是猜的**。

`attribute.attribute()` 返回**第一个**发现的分歧：

```python
for component in order:
    ...
    return Divergence(component=component, ...)
```

谁排第一由 `SEGMENT_ORDER` 决定，而 `attribute.py` 的模块 docstring 自己写明
**这是假设不是事实**——两份 Anthropic 官方文档互相矛盾（prompt-caching 说前缀是
`tools, system, messages`「in that order」，cache-diagnostics 把 `unavailable` 表述为
「model, system, and tools match」）。

多个组件同时变化时，用户拿到的是一个未验证假设的产物，**而且不带任何标记**。

对研究这是待办（#10）。**对工具这是产品缺陷**：用户会照着「62% 的钱漏在 system prompt」
去改自己的 system prompt。**一个会自信说错话的诊断器比没有更糟。**

`diverging_components()` 已经能列出全部分歧，但只被 `calibrate_attributor.py` 用来解释
不一致，从没进过 `attribute()` 的返回值。

---

## 验收方已经做掉的一件事：`order_stability` 的闭式

**不要跑 120 种排列。** 有精确闭式，验收方已在真实数据上验过：

`attribute()` 沿 `order` 走，返回**第一个**分歧的组件。若共有 `k` 个组件分歧，
那么在 5 个组件的全部 120 种排列中，任一特定的分歧组件排在其余 `k-1` 个之前的比例
恰好是 `1/k`（对称性）。所以：

```
order_stability = 1 / len(candidates)
```

`k == 1` 时为 `1.0`——顺序无关紧要，结论是确定的。

**已验证**（验收方，2026-09-03，`capture-02.jsonl`）：18 对有分歧的记录，
逐对跑满 120 种排列统计当选比例，与闭式**零不符**。

**但这份证据的覆盖面必须如实说**：那 18 对里 **17 对是 k=1，只有 1 对是 k=2**，
**k ≥ 3 在真实数据上没有任何样本**。所以：

> 闭式在 k=1（n=17）与 k=2（n=1）上被真实数据支持；k≥3 必须用合成用例覆盖，
> **不许声称已被真实数据验证**。

---

## 交付物

### 1. `src/agentcostlab/attribute.py` — `Divergence` 增加两个字段

```python
@dataclass(frozen=True)
class Divergence:
    ...                       # 既有字段一个不改
    candidates: tuple[str, ...]   # 全部分歧的组件，按 CACHE_LAYOUT 顺序
    order_stability: float        # 1 / len(candidates)
```

**这是唯一允许改动 `attribute.py` 的地方**：新增字段与填充它们所需的最小改动。
既有的比较逻辑、`suppressed` 规则、`SEGMENT_ORDER` 默认值**一个字不许动**。

### 2. `src/agentcostlab/analysis.py` — 新增 `ambiguous` 桶

```python
@dataclass(frozen=True)
class CauseBreakdown:
    by_cause: list[CauseCost]
    ambiguous: CauseCost         # 账本说断了，但成因有多个候选
    unattributed: CauseCost      # 账本说断了，归因器一个分歧都没找到
    ...                          # 其余既有字段不变
```

### 3. `tests/test_attribute.py` / `tests/test_analysis.py` 的新增测试

---

## 关键设计约束

### 一、`order_stability < 1.0` 的轮次不许进 `by_cause`

这是本任务的**全部意义**。一个成因只有在 `order_stability == 1.0`（即只有它一个组件
分歧）时，才能被写进 `by_cause[该成因]` 并带上金额。

否则整条轮次进 `ambiguous`，**并在 `CauseCost` 里保留候选列表**。

这样「62% 的钱漏在 system prompt」这句话，永远不会被一个「可能是 system 也可能是
tools」的轮次抬高。

### 二、`ambiguous` 照样标价，但绝不挂在某个成因名下

**这一条修正 issue #51 原文里「最低档只报现象、不报美元」的说法。**

理由：那笔钱是真花掉的。丢掉它会让总损失少报，而少报和错报是同一类错误的两个方向。
既有的 `unattributed` 已经确立了这个先例——**成因不明但钱是实的，照样计价**。

所以三个桶的语义是：

| 桶 | 含义 | 标价 |
|---|---|---|
| `by_cause[X]` | 账本说断了，且**只有** X 一个组件分歧 | 是 |
| `ambiguous` | 账本说断了，有 **≥2** 个候选组件 | **是**，但列候选、不指定成因 |
| `unattributed` | 账本说断了，归因器**一个分歧都没找到**（或全被 suppressed） | 是 |

`unattributed` 的含义因此收窄为「什么都没找到」，不再兼容「找到好几个」。

### 三、闭式必须对着暴力枚举验证，不许只写断言

闭式是**验收方给的推导**，不是可以直接相信的前提。测试里必须有一条对着
**真实的 120 排列枚举**验证它，而不是把 `1/k` 抄进断言两遍。

做法：对每个测试用例，跑
`itertools.permutations(SEGMENT_ORDER)` 全枚举，统计每个组件当选的比例，
断言其最大值等于 `order_stability`。

### 四、`candidates` 的顺序必须与 `order` 无关

`candidates` 按 `CACHE_LAYOUT`（模块里已有的常量）排序，不按传入的 `order`。
否则同一对请求在不同 `order` 下会给出不同的候选列表，而候选集是个**物理事实**，
不该随扫描顺序变。

`order_stability` 同理：它对 `order` 必须是不变量。

### 五、不许动的东西

`ledger.py` / `pricing.py` / `providers.py` / `proxy.py` / `redact.py` 一个字不许动。
`attribute.py` 只许做约束一里说的最小新增。

---

## 验收标准（AC）

### AC1 — 闭式对着暴力枚举成立

- 对 `k = 1, 2, 3, 4, 5` **各至少一个合成用例**，跑满 120 种排列枚举，
  断言 `order_stability == 最高当选比例`，且等于 `1/k`
- `k ≥ 3` 只有合成用例，**在 NOTES 里如实写明这一点**

### AC2 — 真实数据上的候选分布如实报告

- 在 `data/raw/capture-02.jsonl` 上跑一遍，**报告 k 的分布**
- 验收方实测是 `{1: 17, 2: 1}`。**你的数字必须与之一致；不一致就是有回归，如实报出来**

### AC3 — `order_stability` 与 `candidates` 对 `order` 不变

- 属性测试：对同一对请求体，跑全部 120 种 `order`，断言 `candidates` 与
  `order_stability` **每次都相同**（`component` 允许变，那正是不稳定的表现）

### AC4 — 多候选轮次不进 `by_cause`

- 构造一个双组件分歧且账本判定断裂的用例，断言：
  - `by_cause` 为空
  - `ambiguous.turns == 1`，且带金额（区间两端都在）
  - `ambiguous` 的候选列表含两个组件名
- **变异守护**：把「`order_stability < 1.0` 才进 ambiguous」的判据改成恒假，测试必须失败

### AC5 — 三个桶互斥且穷尽

- 一条测试断言：对任意一份 capture，
  `sum(by_cause.turns) + ambiguous.turns + unattributed.turns` 等于账本判定断裂的轮数
- 用 `capture-02` 跑一遍，断言等式成立

### AC6 — 防回归红线

- `capture-02` 的 `repaid_tokens` 仍然正好 **2822**
- `usd_low` 仍然是 **0.0050796**，`usd_high` 仍然是 **0.0055986**
- 那次断裂是 **k=1**（只有 `system` 分歧），所以它**仍然在 `by_cause` 里**，不进 `ambiguous`
- #50 建立的四道闸全部不许弱化：字节/token 单位闸、未核实费率闸、
  `AmbiguousCacheWrite` 传播、桶容量约束

### AC7 — 顺带补掉 #50 遗留的三条测试

PR #56 验收时列为「不阻塞」的三条，这次一并做掉：

- `_pack()` 的**三桶溢出**合成用例（现有合成用例最多两个非零桶，
  三桶路径只在 capture-02 上走过，而那条测试无数据时会 skip）
- 「完全没有非 cache_read 桶」这条分支的专门测试（行为已正确，缺测试点名）
- 性质测试：随机用量下 `0 <= usd_low <= usd_high` 恒成立

---

## Kill criterion

若暴力枚举与闭式**不一致**：**以枚举为准，报告闭式不成立**，
不要调整枚举去迁就闭式，也不要在实现里对特定 k 做特判。

闭式是验收方的推导，推导可以是错的。数据不会。

---

## 明确不要做的事

- 不要 `git push`，不要开 PR，不要用 `gh`（验收方来做）
- 不要改约束五列的五个模块；`attribute.py` 只做最小新增
- 不要碰 `predictions.md` / `fixtures/pricing.json`
- 不要引入新依赖
- 不要为了让某个数字好看而做特判
- 不要写 CLI（那是 #52）
- commit message 全英文，格式 `type: description`

## 交付要求

写 `docs/delegation/08-attribution-confidence-NOTES.md`，必须包含：

1. **AC1 的枚举结果**：每个 k 的合成用例，枚举出的当选比例分布，与闭式的对比
2. **AC2 的真实 k 分布**，以及是否与 `{1: 17, 2: 1}` 一致
3. AC6 三个红线数字的实测值
4. 测试数变化（`N passed / M skipped`），skip 的每条点名缺什么
5. 你认为规格里不对、或没能验证的地方，如实写出来

**不要声称「全部通过」。把数字贴出来。**

---

## Erratum — AC6 的「k=1」是错的（验收方，2026-09-03）

AC6 写着 record 61 那次断裂是 **k=1（只有 `system` 分歧）**、因此应留在 `by_cause`。
**这一条是错的**，执行方按 kill criterion 忠实于数据、如实报告了矛盾，做法正确。

验收方用 **main 上未经本次改动的 `attribute.py`** 独立复算：

```
record 61 的前驱 = record 60
diverging_components = ['system', 'messages']   -> k = 2
120 排列当选分布: {'system': 60, 'messages': 60}
(component, suppressed) 分布: ('system', False) 60, ('messages', False) 60
```

两个候选都 `suppressed=False`——**都真的打断了缓存**，不是一个真断一个被抑制。

执行方给的机制解释（`_normalise_content` 只剥 `text` 块的 `cache_control`、
不剥 `tool_use` 块）经复核**部分成立但不是全部原因**。实测：

- prev 的 marker 在一个 `text` 块上、带 `ttl:"1h"`；curr 的在一个 `tool_use` 块上、无 `ttl`。
  marker **换了块类型**，归一化的不对称确实制造了一部分字节差。
- 但对称剥离掉**所有**块类型的 `cache_control` 之后，`messages` **仍然分歧**——
  curr 在历史中间插入了一个新的 text 块（`"CRITICAL: Respond with TEXT ONLY..."`）。

**所以 k=2 是真实的物理事实，不是归一化的产物。** AC6 的错误在验收方，与实现无关。

### 这个错误的影响超出本任务

PR #56 合并进 main 的那个数字，报的是「因为 `system`，$0.0055986」。而成因实际是
`system` 与 `messages` 五五开。**那句话是带着虚假确定性发布的**——正是 #51 要修的
失效模式，而它已经在已合并的代码里活着了。

这不是锦上添花：**#51 修正的是本仓库已经发布过的一个断言。**

### 裁定

按**红线数字**判，不按「k=1」判。三个数字（2822 / 0.0050796 / 0.0055986）逐位保留，
桶从 `by_cause[system]` 移到 `ambiguous`（候选 `('system','messages')`）——
这正是本任务想要的行为。
