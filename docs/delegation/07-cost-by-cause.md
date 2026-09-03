# 委托任务 07 — 按成因分摊美元

**执行方**：DeepSeek（独立 worktree）
**验收方**：Claude（review 后由验收方 push / 开 PR）
**分支**：`feat/cost-by-cause`
**对应 issue**：#50

---

## 背景（执行方从零开始读，只需要知道这些）

这个 repo 在测量 coding agent 的 prompt cache 成本。核心立论：**token 不是钱**。
一个缓存命中的 input token 只要标价约 10%，所以「省了 40% token」和「账单变贵」
完全可以同时成立。市面上的上下文压缩工具全在宣传 token 降低率，**没有一个公布美元**。

仓库现在有两半，**中间没有桥**：

- `attribute.attribute(prev_body, curr_body)` 说「因为 `system` 变了」——给的是
  component、detail、path、字节数
- `pricing.cost(usage, model_key)` 说「这次调用花了多少美元」——只吃 `providers.Usage`
- **没有任何函数能说「因为 `system` 变了，多花了 $X」**

本任务就是这座桥。它是这个仓库从「报命中率」变成「报诊断」唯一缺的东西。

---

## 交付物

### 1. `src/agentcostlab/analysis.py`（新文件）

```python
@dataclass(frozen=True)
class CauseCost:
    cause: str              # "system" | "tools" | "messages" | "model" | "params"
    turns: int              # 该成因造成断裂的轮数
    repaid_tokens: int      # 重付的 token 数（守恒式给出，不是估的）
    usd_low: float          # 全部重付 token 落进最便宜的桶
    usd_high: float         # 全部重付 token 落进最贵的桶

@dataclass(frozen=True)
class CauseBreakdown:
    by_cause: list[CauseCost]
    unattributed: CauseCost      # 账本说断了、归因器说不出为什么
    disputed_turns: int          # 归因器说分叉、账本说没断（只计数，不标价）
    not_measured_turns: int      # usage 缺失，既不算断也不算没断
    hit_usd_saved: float         # 命中省下的钱（单独报，不与损失合并）

def cost_by_cause(records: list[dict], model_key: str) -> CauseBreakdown: ...
```

### 2. `tests/test_analysis.py`

### 3. PR 描述里的手算对账（见 AC1）

---

## 关键设计约束

以下四条是**已定的设计判断，不要自行改动**。有异议写进交付 NOTES，不要直接改实现。

### 一、重付的 token 数不许估，守恒式已经给出

`ledger.broke_cache()` 用的是：

```
expected = prev.cache_read + prev.cache_creation
断了  ⟺  curr.cache_read != expected
```

于是**重付的 token 数**就是：

```
repaid = expected - curr.cache_read          （仅当 > 0）
```

全部来自 provider 自报的 usage，**不引入任何新的估算面**。

- ❌ 不要引入 tokenizer
- ❌ 不要用 `Divergence.bytes_before` / `bytes_after` 反推 token 数
- ❌ 不要用「字节数 ÷ 4」这类近似

`repaid < 0` 的情况（这一轮读到的比预期还多）**不是负损失**，是仪器异常：
计入 `not_measured_turns` 并在返回值里可见，不要 clamp 成 0 悄悄放过。

### 二、损失是「实际计费 − 反事实计费」，不是「重付 token × 缓存价」

少读的 token 没有消失，它们被重新计费了——落进 `cache_write_5m` / `cache_write_1h`
/ `input_uncached` 其中之一。所以：

```
损失 = repaid × (它实际所在桶的费率 − cache_read 费率)
```

Anthropic 的费率跨度（见 `fixtures/pricing.json` 与 `pricing.Rate`）：
`cache_read` ≈ 0.1×base，`input_uncached` = 1×base，`cache_write_5m` = 1.25×base，
`cache_write_1h` = 2×base。

### 三、「落进哪个桶」是自由参数，所以出区间不出点估计

我们只知道这一轮的**总**用量，不知道那 `repaid` 个 token 具体进了哪个桶。
两端差近 2 倍。

**本仓库的纪律是：有自由参数就把它在区间上扫一遍，报区间，不报点估计。**
（先例：`report/05-e3-compact-payback.md` 把反事实里那个自由参数在 0 / 571 / 900
上扫过，结论是 18–19 而不是一个漂亮的单值。）

所以：

- `usd_low` = 全部 `repaid` 落进该轮实际出现过的**最便宜**的非 cache_read 桶
- `usd_high` = 全部 `repaid` 落进该轮实际出现过的**最贵**的非 cache_read 桶
- 只考虑该轮 usage 里**实际非零**的桶。该轮没有 1h 写入，就不许拿 2× 当上界
- 若该轮除 cache_read 外只有一个非零桶，`usd_low == usd_high`，区间退化成点——
  这是唯一允许出点估计的情形

**任何对外输出都必须成对呈现 low 与 high。** 不许在别处偷偷取中点、取均值、或只印一个数。

### 四、三个「说不清」的桶必须单独存在，不许摊掉

账本（`ledger`）和归因器（`attribute`）是**互相独立的两个判据**——`ledger.py` 的
模块 docstring 明确写了它是用来检验归因器的 oracle，所以不能共享后者的推理。
两者不一致时，不一致本身就是要报告的事实：

| 情形 | 归入 | 标价吗 |
|---|---|---|
| 账本说断了，`attribute()` 返回具体 component | `by_cause[该 component]` | 是 |
| 账本说断了，`attribute()` 返回 `None` 或 `suppressed=True` | `unattributed` | **是**（钱确实花了，只是不知道为什么） |
| 账本说没断，`attribute()` 报了分叉 | `disputed_turns` | **否**，只计数 |
| `usage` 缺失或空（`broke_cache()` 返回 `None`） | `not_measured_turns` | 否 |

`unattributed` 是**诚实性的核心**。把它摊进某个成因，或者干脆丢掉，都会让
「62% 的钱漏在 system prompt」这种结论虚高。它必须在返回结构里，也必须在
任何输出里可见。

### 五、字节与 token 是两个量，禁止同列

`Divergence.bytes_before` / `bytes_after` 是 JSON 字节数，`repaid` 是计费 token 数，
**两者的关系本仓库没有验证过**。

- 同一张输出表里禁止把二者放进同一个数值列
- 写一个测试断言这件事（见 AC4）

这不是洁癖：本仓库有过一次「用总 prompt token 当成本指标，其中 66% 是便宜十倍的
缓存命中，指标高估 3 倍」的教训。混单位就是同一个错误的另一种形状。

### 六、只读消费，不改上游

`analysis.py` 是 `ledger` / `attribute` / `pricing` / `providers` 的**只读消费者**。

不要为了方便去改这四个模块的任何签名或行为。若确实发现上游缺口，写进交付 NOTES
由验收方判断，不要自行改。

---

## 验收标准（AC）

AC 不是「测试通过」。**每一条都要核对外部事实本身，不是核对测试状态。**

### AC1 — 记录 61 手算对账（硬门槛）

`data/raw/capture-02.jsonl` 记录 61 是本仓库**唯一一次真实缓存断裂**，
`report/03-e1-miss-reasons.md` 已记录：

| | |
|---|---|
| 预期读取 | 157,991 |
| 实际读取 | 155,169 |
| 差额 | **2,822 token 付了两遍** |

要求：

1. `cost_by_cause()` 在这份 capture 上跑出的 `repaid_tokens` 必须**正好是 2,822**
2. 手工按费率算出 `usd_low` / `usd_high`，**过程写进交付 NOTES**（列出用的是哪条
   费率、`retrieved_at` 是哪天、算式长什么样）
3. 函数输出与手算**逐位一致**

**手算过程必须出现在 NOTES 里。** 只写「测试通过」不算完成 AC1——本项目已经被
「编译过、测试绿，但配置指向一个不存在的东西」打过脸。

若 `data/raw/capture-02.jsonl` 在你的 worktree 里不存在（它是 gitignored），
**如实报告「AC1 未测」，不要用合成数据冒充**，也不要因此调整实现去迎合一个想象中的数字。

### AC2 — 记录 61 的成因归属如实报告

这次断裂的机制是 **TTL 切换**（`report/03` 已判定）。跑完之后如实报告它落进了
`by_cause` 的哪个 component，还是落进了 `unattributed`。

**两种结果都是合格交付。** 落进 `unattributed` 说明归因器指不出 TTL 类断裂——
这是一个真实发现，正是 #54（E4 故障注入）要去验的东西。

❌ 不许为了让它落进某个成因而在 `analysis.py` 里做特判。

### AC3 — 区间两端都出现

任何返回值与任何打印输出里，`usd_low` 与 `usd_high` 都成对出现。
写一个测试：构造一轮同时含 `cache_write_1h` 与 `input_uncached` 的 usage，
断言 `usd_high > usd_low`。

### AC4 — 单位闸有测试守着

写一个测试断言字节量与 token/美元量不会出现在同一个数值列。
参考仓库既有做法：`tests/test_gates.py` 是「闸由测试守住而不是靠清单」的范式。

### AC5 — 未核实费率一律不出数

`pricing.cost()` 已有 `UnverifiedRate` / `AmbiguousCacheWrite` 两道闸。
`analysis.py` 必须让它们照常抛出，**不许 try/except 吞掉换成 0 或跳过**。

写一个测试：喂一条未核实费率，断言 `cost_by_cause()` 抛出而不是返回残缺结果。

### AC6 — 测试调用真函数

测试必须调用 `cost_by_cause()` 本身，不许在测试里手工复刻一遍分摊逻辑。

理由（本项目实际踩过）：**测试若手工复刻被测逻辑而不调用它，等于没测**——
将来有人加一条绕过闸门的路径，这种测试照样绿。

### AC7 — 离线可跑

单元测试不联网、不依赖 `data/raw/` 是否存在。依赖真实 capture 的测试要
显式 skip 并**点名缺哪个文件**（仓库既有做法：157 passed / 3 skipped，每条都点名）。

---

## Kill criterion

若守恒式在真实数据上给出负差额、或无法与官方 usage 对上：
**报「算不出来」，不要挑一个看起来合理的近似。**

一个错的美元数字比没有数字更糟——这个仓库全部的可信度都建立在数字是对的上面。

---

## 明确不要做的事

- ❌ 不要 `git push`，不要开 PR，不要用 `gh`（验收方来做）
- ❌ 不要改 `ledger.py` / `attribute.py` / `pricing.py` / `providers.py` / `proxy.py` / `redact.py`
- ❌ 不要碰 `predictions.md`（LOCKED，有专门协议）
- ❌ 不要往 `fixtures/pricing.json` 里填价格
- ❌ 不要做任何付费 API 调用
- ❌ 不要引入新依赖（标准库 + 已有依赖足够）
- ❌ 不要为了让某个数字好看而做特判
- ❌ 不要写 CLI（那是 #52，另一个任务）

---

## 提交要求

- commit message 全英文，格式 `type: description`，**不许出现中文字符**
- 交付时写 `docs/delegation/07-cost-by-cause-NOTES.md`，必须包含：
  - **AC1 的手算过程**（费率来源、`retrieved_at`、算式、逐位对账结果）
  - AC2 的实际归属结果（落进哪个 component 还是 `unattributed`）
  - 测试实际数字（`N passed / M skipped`），**skip 的每条点名缺什么**
  - 你认为不成立、或没能验证的每一条 AC，如实写「未测」
- **不要声称「全部通过」。把实际数字贴出来。**
