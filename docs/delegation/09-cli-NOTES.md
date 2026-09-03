# CLI 委托 — delivery notes

两个 commit，顺序未颠倒：

1. `feat: price each record at its own provider/model rate` — #59 逐条费率解析
2. `feat: add agentcostlab record / diagnose CLI` — #52 两条命令

---

## 第一步 — `capture-03` 的分布与总金额

`cost_by_cause()` 现在逐条解析费率后，`capture-03`（162 条：sonnet 77 + opus 85）
第一次能出数：

| 桶 | turns | repaid_tokens | usd_low | usd_high |
|---|---|---|---|---|
| `by_cause` | —（空） | — | — | — |
| `ambiguous` | 3 | 8,884 | $0.0410330 | $0.0448855 |
| `unattributed` | 0 | 0 | 0 | 0 |
| `disputed_turns` | 0 | | | |
| `not_measured_turns` | 3 | | | |
| `hit_usd_saved` | | | $37.802785 | |

总金额：**损失 $0.0410–$0.0449**，**缓存命中省下 $37.80**。四条断裂全部是 opus、
全部是 `ambiguous`（`system` + `messages` 双分歧，`order_stability=0.5`）。其中
record idx=66 一轮 `repaid < 0`（读到的比守恒式预测的多 98），按既有一致性判为
instrument 异常，计入 `not_measured`，不压成 0、不当负损失。`by_cause` 为空 —
- 与规格约束一（在真实数据上 `by_cause` 是空的）一致。

### 一轮 opus 损失的手算（record idx=59，最大的一轮）

**费率来源**：`fixtures/pricing.json` → `anthropic/claude-opus-5`，
`retrieved_at = "2026-08-19"`，`verified: true`。单位 USD/MTok：

| 项 | 值 |
|---|---|
| input_uncached | 5.00 |
| cache_read | 0.50 |
| cache_write_5m | 6.25 |
| cache_write_1h | 10.0 |

**这一轮的 usage**（来自 provider 上报）：`input_tokens = 6,688`（未命中输入）、
`cache_creation = {ephemeral_5m: 2,840, ephemeral_1h: 0}`。
守恒式：`repaid = 期望读回 − 实际读回 = 7,532`。

该轮可承载 repaid 的非 cache-read 桶只有两个（无 1h 写）：

| 桶 | 容量 | 单位价 − cache_read |
|---|---|---|
| input_uncached | 6,688 | 5.00 − 0.50 = 4.50 |
| cache_write_5m | 2,840 | 6.25 − 0.50 = 5.75 |

`usd_low`（便宜优先，input 先填）：

```
6,688 × 4.50        = 30,096.0
(7,532 − 6,688) × 5.75 = 844 × 5.75 = 4,853.0
合计 30,096.0 + 4,853.0 = 34,949.0  →  34,949 / 1e6 = $0.034949
```

`usd_high`（贵优先，5m 先填）：

```
2,840 × 5.75        = 16,330.0
(7,532 − 2,840) × 4.50 = 4,692 × 4.50 = 21,114.0
合计 16,330.0 + 21,114.0 = 37,444.0  →  37,444 / 1e6 = $0.037444
```

即这一轮 opus 损失区间为 **$0.034949–$0.037444**。另两轮（repaid 571、781）同法
相加后，三轮回合得到 `ambiguous.usd_low = 0.0410330`、`usd_high = 0.0448855`，
与 `cost_by_cause()` 输出逐位一致。

---

## 第一步 — `capture-02` 三个红线数字

`cost_by_cause()` 在 `capture-02` 上（66 条，全 sonnet），红线数字逐位不变：

```
repaid_tokens = 2822
usd_low       = 0.0050796
usd_high      = 0.0055986
```

落在 `ambiguous` 桶（`turns=1`，候选 `('system','messages')`）——与上一轮 08 的
结论一致：那次断裂是 k=2，进 `ambiguous` 不进 `by_cause`。`hit_usd_saved = 7.674973`
（规格里写 $7.6750，是本值四舍五入到 4 位）。

---

## 第二步 — `diagnose` 真实输出全文

`agentcostlab diagnose data/raw/capture-02.jsonl`（exit 0）：

```
Money
  cache hits saved   $7.674973  (4,263,874 read tokens)
  cache misses cost  $0.005080 – $0.005599  (1 turn, 2,822 tokens paid twice)

Read savings by model
  claude-sonnet-5    4,263,874 read tokens -> $7.674973 saved
    one break ~ $0.149378 (median prefix 82,988 tokens)

Cause breakdown  (attribution NOT yet validated on a real break — #54 fault injection pending)
  by_cause:  none
  ambiguous: 1 turn (100% of 1 breaks), 2,822 tokens, $0.005080 – $0.005599  [candidates, not causes: system, messages]
  unattributed: none
```

`agentcostlab diagnose data/raw/capture-03.jsonl`（exit 0）：

```
Money
  cache hits saved   $37.802785  (10,984,678 read tokens)
  cache misses cost  $0.041033 – $0.044885  (3 turns, 8,884 tokens paid twice)

Read savings by model
  claude-opus-5      6,677,913 read tokens -> $30.050609 saved
    one break ~ $0.423027 (median prefix 94,006 tokens)
  claude-sonnet-5    4,306,765 read tokens -> $7.752177 saved
    one break ~ $0.102568 (median prefix 56,982 tokens)

Cause breakdown  (attribution NOT yet validated on a real break — #54 fault injection pending)
  by_cause:  none
  ambiguous: 3 turns (100% of 3 breaks), 8,884 tokens, $0.041033 – $0.044885  [candidates, not causes: system, messages]
  unattributed: none
  not measured: 3 turns
```

两条命令都通过 `[project.scripts]` 的 `agentcostlab` 入口；`record` 是
`scripts/capture.sh` 的转发，不是重写。

---

## 测试数变化

```
基线        243 passed, 0 skipped
本任务后    252 passed, 0 skipped   （+9，无 skip）
```

- `tests/test_analysis.py`：+1（新增 `test_a_deepseek_record_among_anthropic_records_names_its_index`，
  并把三个旧的「与 model_key 一致」测试改写为逐条解析语义：`test_a_mixed_model_capture_prices_each_record_at_its_own_rate` /
  `test_an_unknown_model_refuses_and_names_the_record` / `test_a_record_without_a_model_field_refuses`），净 +1。
- `tests/test_cli.py`：+8（新文件）。

**skip 数为 0，没有需要点名的 skip。** 三份真实 capture（`capture.jsonl` /
`capture-02.jsonl` / `capture-03.jsonl`）本次都在 `data/raw/` 里，08 那轮报的
10 个 skip（缺 `capture.jsonl` / `capture-01.jsonl` / `capture-03.jsonl`）已全部消失。

---

## 规格里不对、或没能验证的地方

### 1. 「没有 `model` 字段」的二选一，选了「抛出」

规格让在「抛出」与「要求调用方显式给兜底 key」之间二选一，我选了**抛出**。理由：
兜底 key 会重新引入「一份 capture 一个 key」的歧义——`#59` 要消灭的正是这件事。逐条
解析的语义下，一条没有 `model` 的记录没法定价，猜最常用的模型会把「可能是别的模型」
按错费率静默计费。抛出并点名记录（`MissingModel`）最诚实。三份真实 capture 里所有
162+66+75 条都带 `model`，此分支只由测试覆盖。

### 2. `pip install -e .` 本次没跑成

`[project.scripts]` 已写进 `pyproject.toml`，`agentcostlab.cli.main` 也验证过能正确
分发（`main(['diagnose', ...])` 返回 exit 0，输出如上）。但本会话里 `.venv/bin/pip
install -e .` 被权限闸挡住，没能真正生成 `agentcostlab` 这个 console-script 二进制并
点它跑。验收方需在验收环境跑一次 `pip install -e .` 确认两条命令可用——这是 AC 里我
唯一没亲手验证的一步。

### 3. `capture-03` 的四条断裂全是 opus、全是 `ambiguous`

规格只要求「至少一轮 opus 手算」。实际数据里**没有一轮 sonnet 断裂**、**没有一轮
单成因断裂**（全部 `system`+`messages` 双分歧）。所以「混模型逐条解析」的美元相加，
在 `capture-03` 上只发生在 `hit_usd_saved` 的分母侧（opus 省 $30.05 + sonnet 省
$7.75），损失侧三轮回合全是 opus。真正的「同一份 capture 里 sonnet 与 opus 各断一次、
各自按自己的费率相加」没有真实数据，只被合成测试
`test_a_mixed_model_capture_prices_each_record_at_its_own_rate` 覆盖。

### 4. 「若断一次要多付多少」是整段前缀重算的上界，不是典型断裂

约束二要求零断裂时给出「若断一次要多付多少」。实现用**各模型 cache_read 的中位数**
（典型前缀长度）×（input − cache_read）折算。这是「整段前缀断一次、全部重新按未命中
计费」的上界。真实断裂往往是**部分**断裂——`capture-02` 那次只重付了 2,822 / 157,991
个 token（$0.005），而中位数前缀整断要 $0.149。输出里用 `median prefix N tokens` 标注
了这个假设，但读者若只扫一眼数字会误以为「断一次要 $0.15」。若要更准，需要一个
「典型断裂比例」的度量，规格未要求，本次没做。

### 5. 归因未在真实断裂上验证，输出已标注

`#54`（故障注入验收）没跑，所以 `Cause breakdown` 那节头部印了
「attribution NOT yet validated on a real break — #54 fault injection pending」。
这是 AC 要求的标注，但要注意：当前三份 capture 里唯一的真实断裂（capture-02 那 1 次、
capture-03 那 3 次）都落在 `ambiguous`，`by_cause` 从未被真实数据填过——「因为 system
变了多花 $X」这句话目前仍是合成测试撑着的，不是实测结论。
