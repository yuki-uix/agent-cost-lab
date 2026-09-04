# E4 — 注入式采集：给归因器造一批有标准答案的 miss

> 这一份是**计划**，不是结果。写在跑之前。
> 预测行必须先落进 `predictions.md`（新增行，附信心排序，同一个 commit），再开跑。

---

## 1. 为什么需要它

E1 章自己写了阻塞原因，一字不改地引在这里：

> **未开始，原因是没有样本，不是没有分析。**

现状（`report/03-e1-miss-reasons.md` [实测 2026-08-18]）：

| 文件 | 记录数 | 带 diagnostics |
|---|---|---|
| capture-attempt1-failed.jsonl | 67 | 0 |
| capture-attempt2-nogzipfix.jsonl | 4 | 0 |
| capture-attempt3-prelineagefix.jsonl | 16 | 4（全是 #14 修掉的跨 lineage 串线产物）|
| capture.jsonl | 75 | 0 |

主 capture 的 63 个 threaded turn 全部 `diagnostics: null`，含义已确认为
「比对过了、命中」而非「接口没生效」。第二份 capture（66 条）捕到**唯一一次**
真实断裂：记录 61，预期读 157,991、实际读 155,169，机制是 TTL 切换。

`scripts/calibrate_attributor.py` 的 docstring 把这件事说得最直白：

> This is the signal available in the 75-record Claude Code capture: **it never
> breaks** (`cache_read_input_tokens` climbs monotonically), so every comparable
> turn is "no divergence".

**所以归因器至今只被负例检验过。** 18 个 `fixtures/attribution/*.json` 是手工构造的
请求对，验的是单元级逻辑；`calibrate_attributor.py` 在真实流量上跑，但那批流量里
没有 miss。两者之间缺一环：**真实流量里、原因已知的 miss。**

自然采集补不上这一环——agent 正常跑就是不断缓存。**必须主动注入。**

---

## 2. 注入项

每一项都对应一个已有的 fixture，于是 fixture 是单元测试、注入是集成测试，
同一个判断在两个层级上被检验。

| ID | 注入动作 | 对应 fixture | 预期归因 | 断裂频率 |
|---|---|---|---|---|
| **I0** | 不注入（baseline） | `identical.json` | **无**（误报检查）| — |
| **I1** | system prompt 尾部插入 ISO 时间戳 | `system_timestamp.json` | `component=system` | 每 turn |
| **I2** | 工具定义序列化前打乱 key 顺序 | `tools_schema_key_order.json` | `component=tools`，detail 指到具体 tool | 间歇 |
| **I3** | 会话中途改一个 tool 的 description | `messages_edited.json` / `tools_*` | `component=tools`，单次 | 一次 |
| **I4** | 会话中途换模型 | `model_changed.json` | `component=model` | 一次 |
| **I5** | 触发一次历史压缩 | `messages_truncated.json` | `component=messages`，detail 指到消息索引 | 一次 |

**I2 是最难的一项**，因为它间歇性——key 顺序有时候碰巧一致。它是唯一含随机性的注入，
按项目纪律（机械效应 n=1 可信，含随机性的要看离散度）**必须 n≥5**。
其余各项是机械效应，n=1 即可采信。

**I5 最贵**：要先把上下文累积到压缩阈值才能触发。可以复用 E3 的 `capture-03.jsonl`
路径（那次的 `/compact` 已经采到），若能复用则不重复花钱。

**采集协议（#72 的预防）。** 每次 capture 的 task prompt 开头必须带一个 run number
或随机 nonce，让两条会话的 `messages[0]` 必然不同。原因：`_lineage_key` 只对
`messages[0]` 取哈希，两次跑的任务开头一字不差时会被合并成同一条 lineage，撞车的
seam 会被误当成一次缓存断裂（#72）。nonce 是**预防**；`scripts/capture_health.py`
的 lineage-collision gate 是**兜底**——它不依赖 nonce 存在，只在真的撞车时报 FAIL。

---

## 3. Ground truth：三方对照

这是这个实验相对其他 benchmark 的关键优势——**答案有三个独立来源**：

| 来源 | 是什么 | 独立性 |
|---|---|---|
| **A · 注入原因** | 我们自己造的，最强 | 设计出来的，不依赖任何测量 |
| **B · `ledger.broke_cache`** | 守恒式 `curr.cache_read == prev.cache_read + prev.cache_creation` | 数字全部来自 provider 自报 usage，**不依赖归因器** |
| **C · 官方 `cache_miss_reason`** | Anthropic beta 通道，`*_changed` 类型 | provider 的判断，粒度到 component |

`ledger.py` 的模块 docstring 已经把 B 的定位写清楚了：

> Independent of `attribute` on purpose: it is the oracle the attributor is
> checked against, so it must not share the reasoning it is checking.

**三方一致 = 强证据。任何两方不一致，本身就是一个发现**，要写进报告而不是抹平。

特别地：C 只给到 component 级（`model` / `system` / `tools` / `messages`），
而 `attribute.py` 声称能下钻到具体 tool / message index / field。
**下钻那一层没有外部 ground truth，只能靠 A。** 这是 A 不可替代的原因。

---

## 4. 预测（待落入 `predictions.md` 后方可开跑）

按该文件的锁定协议，以下作为**新增行**提交，附信心排序，与本文件同一个 commit。
`predicted` / `Falsified if` 一旦落地即冻结，只有 `actual` 和 `verdict` 可写。

| # | 预测 | Falsified if | 信心 |
|---|---|---|---|
| 4.1 | I1–I5 每种注入都至少产生 1 个被 B 判定为断裂的 turn | 任一种注入跑满预定 turn 数仍无断裂 | 高 |
| 4.2 | component 级归因，≥4/5 与 A 一致 | ≤3/5 | 中 |
| 4.3 | detail 级归因（具体 tool / message index / field），≥3/5 指对 | ≤2/5 | 低 |
| 4.4 | 在带 C 的 turn 上，A 与 C 的 component 一致率 ≥90% | <90% | 中 |
| 4.5 | I0 baseline 中被 B 判定断裂的 turn 数为 0 | ≥1 次且无法归因到 TTL 或已知外部原因 | 中 |
| 4.6 | I2 在 n≥5 下的断裂率落在 20%–80%（间歇性的证据） | 落在 0% 或 100% | 低 |

4.3 信心低是诚实的：下钻层没有外部 ground truth，且 `attribute.py` 的 `SEGMENT_ORDER`
本身是**假设不是事实**（模块内已注明 Anthropic 文档两处表述不一致）。
多个 component 同时分歧时该先怪谁，这次实验正好能测。

4.5 的例外条款是必要的：记录 61 那次断裂机制是 **TTL 切换**，与注入无关。
baseline 里若出现 TTL 型断裂，不计为误报，但**必须单独标注**，不许悄悄归入正常。

---

## 5. 停止条件

写在前面，免得跑完之后靠感觉判断。

- **4.2 被证伪（component 级 ≤3/5）** → 归因不成立。工具的价值全在「为什么断」，
  只报「断了」的话 provider 自己就给了。**停，不做下去。**
- **4.5 被证伪且无法解释** → oracle 或归因器有缺陷，**先修，不许带病继续采集**。
- **4.4 被证伪（与官方一致率 <90%）** → 不必停，但报告里必须把分歧逐条列出并给出解释；
  若解释不了，则归因器的可信度降级为「仅供参考」，不得用于成本估算。

---

## 6. 成本

按项目规矩，**跑之前报预估，等确认再跑**。这里给算式，不给拍脑袋的金额。

需要的 turn 数（估）：

```
I0 baseline      10 turn
I1 时间戳         5 turn   （每 turn 都断，最省）
I2 key 顺序      5 turn × 5 次重复 = 25 turn   （唯一需要 n≥5 的）
I3 改 tool       8 turn
I4 换模型        8 turn
I5 压缩         复用 capture-03，若不可复用则需累积到阈值（最贵）
                                     ────────
                              约 56 turn + I5
```

**provider 选择是一个真实的取舍**：

| | Anthropic | DeepSeek |
|---|---|---|
| 官方 `cache_miss_reason`（来源 C） | ✅ beta 通道已确认可用 | ❌ 无 |
| 单位成本 | 高 | 低 |

建议拆成两批：**DeepSeek 跑量**（只用 A+B 两方对照，验 4.1/4.2/4.3/4.5/4.6），
**Anthropic 跑小批**（补 4.4 的官方对照）。这样把最贵的那部分压到最小。

注意 `pricing.py` 的核实闸：`cost()` 拒绝在未经官方定价页核实的费率上运行。
开跑前先确认 `fixtures/pricing.json` 里两家的费率都带 source + retrieved_at 且是当前值。

---

## 7. 产出

- `data/raw/capture-inject-*.jsonl` —— 每种注入一份（gitignored）
- `data/redacted/` —— 过脱敏闸后可提交的版本
- `report/03-e1-miss-reasons.md` —— 从「未开始」写到有结论
- `predictions.md` —— 4.1–4.6 的 `actual` 与 `verdict` 填入
- 若 4.2 成立：归因器第一次拥有**真实流量上的正例证据**，
  这是从「自用仪器」走向「别人能装的工具」的前提条件

---

## 8. 不做的事

- 不在这一轮里做 CLI、打包、文档。**先证明归因成立，再谈给别人用。**
- 不因为结果不好看就改 `predicted`。错的预测留着，附 `actual` 和诊断。
- 不用手工构造的请求对充当「真实流量」。fixture 是单元层，这一轮验的是集成层，
  两者不可互相替代——项目 NOTES 里那条「自己拼的『实测』比不测更危险」说的就是这个。
