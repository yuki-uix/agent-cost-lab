# E4 剩余任务

交接文档。**每一节可以原样贴成一个 GitHub issue**（标题就是小标题）。
拿这份文档的会话不需要之前的上下文——背景在 §0，方案在
[`e4-injection-campaign.md`](./e4-injection-campaign.md)，代码已在
分支 `feat/e4-injection-campaign`（commit `56b4937`）。

---

## §0 背景（新会话必读）

**这个仓库在测什么**：agent 的上下文成本。核心立论——token 不是钱。
一个缓存命中的 input token 只要标价的约 10%，而压缩通过重写历史来省 token，
**会打断缓存前缀，让其后所有内容重新按全价计费**。所以「省了 40% token」
和「账单变高」完全可以同时成立。市面上的压缩工具全在宣传 token 降低率，
**没有一个公布美元数字**。

**E4 要解决的具体阻塞**：`report/03-e1-miss-reasons.md` 自己写了——

> **未开始，原因是没有样本，不是没有分析。**

四份 capture 里正确串接的 `cache_miss_reason` 数量是 **0**：健康的 agent
不会打断自己的前缀，所以自然采集产生不了 miss。归因器
（`src/agentcostlab/attribute.py`，能离线复现 Anthropic 的 `cache_miss_reason`
并下钻到具体 tool / message index / field）**至今只被负例检验过**。

**已完成**（commit `56b4937`）：五种故障注入、代理接线、脱敏闸登记、
20 个测试、三方对照打分脚本。全套 202 passed / 13 skipped。

**没做的**：一次真实采集都还没跑。

---

## §1 把预测 4.1–4.6 落进 `predictions.md`

**阻塞**：T2–T5 全部。**这是第一件事，不做完不许开跑。**
**需要本人判断**：是。`predictions.md` 已 LOCKED，有五轮 review 的历史。

草稿在 `e4-injection-campaign.md` §4：

| # | 预测 | Falsified if | 信心 |
|---|---|---|---|
| 4.1 | I1–I5 每种注入至少产生 1 个被 ledger 判定为断裂的 turn | 任一种跑满预定 turn 数仍无断裂 | 高 |
| 4.2 | component 级归因 ≥4/5 与注入原因一致 | ≤3/5 | 中 |
| 4.3 | detail 级归因（具体 tool / message index / field）≥3/5 指对 | ≤2/5 | 低 |
| 4.4 | 带官方 verdict 的 turn 上，自建归因与官方 component 一致率 ≥90% | <90% | 中 |
| 4.5 | I0 baseline 中被判定断裂的 turn 数为 0 | ≥1 次且无法归因到 TTL 或已知外部原因 | 中 |
| 4.6 | I2 在 n≥5 下断裂率落在 20%–80% | 落在 0% 或 100% | 低 |

**按该文件的协议**：新增行必须与信心排序在**同一个 commit** 落地；
落地后 `predicted` 和 `Falsified if` 即冻结，只有 `actual` / `verdict` 可写；
发现错误走 Errata 追加，不许原地改。

**完成标准**：`predictions.md` 里有 4.1–4.6，且 git 历史显示它们早于任何采集数据。

---

## §2 核实 Anthropic 与 DeepSeek 的当前费率

**阻塞**：T4、T5。**`pricing.py` 的核实闸会拒绝在未核过的费率上出数**，
所以这一步不做，后面跑出来的成本数字一个都拿不到。

**做什么**：更新 `fixtures/pricing.json`，两家都要有：

- `cache_read` / `cache_write`（**两档 TTL 分开**，`#47` 已把它们拆开定价）
- `input_uncached` / `output`
- 每条带 `source`（官方定价页 URL）与 `retrieved_at`

**注意**：`test_gates.py` 里有 `test_a_verified_rate_goes_stale_instead_of_staying_true_forever`
——费率会过期，不是核过一次就永远为真。

**完成标准**：`.venv/bin/python -m pytest tests/test_gates.py -q` 全绿，
且 `cost()` 能对两家出数不抛异常。

---

## §3 决定 I5（历史压缩）怎么处理

**需要本人判断**：是。这是唯一一个**不能从代理注入**的故障。

原因写在 `inject.py` 模块 docstring 里：压缩由客户端驱动（`/compact`），
在传输途中截断 messages 会让客户端下一轮引用到上游已经没有的上下文。

**两个选项**：

- **A · 复用 `capture-03.jsonl`**。E3 那次的 `/compact` 已经采到了
  （`report/05-e3-compact-payback.md`：压缩前 6 轮、压缩后 46 轮）。
  **省钱，但那份 capture 没有 `injection` 字段**，`score_injection.py` 会拒绝打分
  （§4 的脚本要求 capture 声明自己属于哪个 arm）。需要决定：
  给它补一个 arm 标签（等于承认这是事后贴的），还是把 I5 排除在 4.1–4.3 的计分之外。
- **B · 重新采一次**，客户端里手动 `/compact`，代理正常记录。
  数据干净，但要先把上下文累积到压缩阈值，**是所有故障里最贵的一项**。

**建议**：先做 A 的可行性检查——把 `capture-03.jsonl` 喂给
`score_injection.py`，看它报什么。若拒绝，再决定 A' 还是 B。

---

## §4 跑采集（要花钱，跑前报预估）

**阻塞于**：§1、§2。**需要本人批准金额**：是。

**规矩**（来自项目 NOTES）：要花钱的运行，先报预估金额和它能回答什么问题，
等确认再跑。不后台直接起。

### 先看可测性矩阵：不是每种故障都能在便宜的那一侧测

**这一节推翻了原来「跑量全放 DeepSeek」的分批方案**，依据是 #63 定下的短缺判据：

```
break  ⟺  curr.cache_read  <  prev.cache_read + prev.cache_write
```

Anthropic 上报缓存写入，DeepSeek 不报（自动缓存无写入计费，`cache_write` 恒 0）。
于是在 DeepSeek 上式子退化成 `curr.read < prev.read`——**检测要求上一轮真的缓存过东西**。

实测（用真实判据算，2026-09-04）：

| 故障 | 形态 | Anthropic | DeepSeek |
|---|---|---|---|
| **I0** baseline | 不改动，正常增长 | ✅ 正确判「没断」 | ✅ 正确判「没断」 |
| **I1** system 时间戳 | **每轮都断** | ✅ 可测 | ❌ **永远测不出** |
| **I2** tool schema key 顺序 | 多数轮断，偶尔洗回原序 | ✅ 可测 | ⚠️ **系统性低估** |
| **I3** 改 tool 描述 | 一次性，第 5 轮 | ✅ 可测 | ✅ 可测 |
| **I4** 换模型 | 一次性，第 5 轮 | ✅ 可测 | ✅ 可测 |

**I1 为什么在 DeepSeek 上测不出**：它每轮都改 system prompt，所以 DeepSeek
从头到尾缓存不上任何东西（探针实测三条记录 `read=0`）。

```
Anthropic: prev(read=0, write=30000) -> curr(read=0)
  expected = 30000;  0 < 30000  -> True   检出
DeepSeek : prev(read=0, write 不上报) -> curr(read=0)
  expected = 0;      0 < 0      -> False  检不出
```

**短缺规则需要「上一轮缓存过什么」当基准。每轮全断的故障，没有基准可缩水。**

**I2 为什么是低估而非全盲**：只有在「上一轮侥幸洗回原序、命中了缓存」之后的那一次
断裂才检得出。多数断裂发生在「上一轮也没命中」之后，`expected = 0`，检不出。
4.6 要的是断裂率落在 20%–80%，而 DeepSeek 上测到的率会被压向 0%——
**会以仪器原因触发 4.6 的证伪条件**。

**I3/I4 为什么可测**：一次性故障前几轮正常缓存，第 5 轮 `read` 从数万掉到 0，
有基准可缩水。

### 因此分批改成

| 批次 | provider | 跑哪些故障 | 为什么 |
|---|---|---|---|
| **B1** | **Anthropic** | I1、I2 | 这两种只有在上报写入的 provider 上才可测 |
| **B2** | **DeepSeek** | I0、I3、I4 | 便宜，且这三种在隐式缓存上可测 |

**顺带的好处**：B1 在 Anthropic 上跑，本身就带官方 `cache_miss_reason`，
**4.4 不再需要单独的第三批**——用 B1 的轮次打分即可。

turn 数不变（I1 5 + I2 25 = 30 在 Anthropic，I0 10 + I3 8 + I4 8 = 26 在 DeepSeek），
但成本结构变了：原方案把 56 turn 里的大头压在便宜侧，新方案有 30 turn 在贵侧。
**开跑前重新报预估。**

### 这条限制要写进结论，不只写在这里

若最终只在 Anthropic 上验证了 I1/I2，那么 4.1 的「每种故障都产生断裂」
**是在上报写入的 provider 上成立的结论**，不能直接外推到隐式缓存的一侧。
这与 #42 的 compaction 盲点同一形状：**仪器的可测边界必须随数字一起被引用。**

turn 数估算（`e4-injection-campaign.md` §6）：

```
I0 baseline   10      I1 时间戳    5      I2 key 顺序  5×5=25
I3 改 tool     8      I4 换模型    8      I5           见 §3
                                          ─────────
                                   约 56 turn + I5
```

**怎么跑**：

```bash
# 每种故障一份 capture，arm 必须显式声明（i0 也要）
AGENTCOSTLAB_INJECT=i0 ACL_CAPTURE=data/raw/capture-inject-i0.jsonl \
  .venv/bin/python -m uvicorn agentcostlab.proxy:app --port 8787
# 客户端指向 http://127.0.0.1:8787，正常跑够 turn 数

# I4 额外需要目标模型，没有默认值
AGENTCOSTLAB_INJECT=i4 AGENTCOSTLAB_INJECT_I4_MODEL=<model> ...
```

**采集完先过健康检查**（`scripts/capture_health.py`），
再确认 `injection` 字段在每条记录上都在——包括 `applied: false` 的那些。

---

## §5 打分并填 `actual` / `verdict`

**阻塞于**：§4。

```bash
.venv/bin/python scripts/score_injection.py data/raw/capture-inject-i1.jsonl
```

脚本输出三方对照的 tally 和**逐条列出的分歧**。

**分歧不许抹平。** 脚本刻意把它们打印出来而不是调和——报告要承载它们。

**填表规则**：只写 `actual` 和 `verdict`。预测错了就留着错的，附 `actual` 和诊断。

**停止条件**（写在前面，免得跑完靠感觉判断）：

- **4.2 被证伪（component 级 ≤3/5）** → 归因不成立。工具的价值全在「为什么断」，
  只报「断了」provider 自己就给了。**停，不做 §7。**
- **4.5 被证伪且解释不了** → oracle 或归因器有缺陷，**先修，不许带病继续采集**。
- **4.4 被证伪** → 不必停，但报告须逐条列出分歧并解释；
  解释不了则归因器降级为「仅供参考」，**不得用于成本估算**。

---

## §6 把 `report/03-e1-miss-reasons.md` 从「未开始」写到有结论

**阻塞于**：§5。

要回答的两个问题（章节抬头自己写的）：真实 session 里 miss 因何而起？
自建归因器与官方 `cache_miss_reason` 的一致率是多少？

**格式规矩**：所有数字带 `[一手]` / `[实测]` / `[二手·未核]` 标签，见 `docs/sources.md`。

**必须写进去的两件事**：

1. **记录 61 那次 TTL 型断裂**（capture-02，预期读 157,991 实际 155,169）与注入型断裂
   是不同机制，不要混为一谈。
2. **n=1 的项要标明**。按项目纪律，机械效应（字节、前缀）n=1 可信；
   I2 含随机性，必须报离散度而不是点估计。

顺带把 `report/06-conclusions.md`（现在是「未开始」）补上，
以及**更新 README** —— 它现在还写着 "Status: instrument built, nothing measured yet"，
而 E3 早已出结果，这是过时的。

---

## §7 若 4.2 成立：从「自用仪器」做成「别人能装的工具」

**阻塞于**：§5，且 **4.2 必须成立**。4.2 不成立就不做这一节。

这是产品化那一步，**不属于验证**，所以刻意排在最后。

要做的：

- CLI：`proxy` / `report` 两个子命令，别人不用读源码就能跑
- 接入文档：怎么把 Claude Code / OpenCode / Cursor 指向代理
- 报告输出：从当前的 tally 变成可执行建议
  （「把时间戳移到消息末尾，按你的量级每月省 $X」）
- 打包发布

**做之前想清楚形态**：一次性诊断的留存天然低（用一次、修好、卸载）。
更硬的形态是**持续检查**——放进 CI，报「这个 PR 让缓存命中率掉了 12%」。
这个决定影响 CLI 怎么设计，**在写第一行 CLI 代码之前定**。

---

## 不要做的事

- **不要重新讨论方向。** 选题经过了两轮否决（闸覆盖检查器、agent 事务层），
  依据是对 57 个开源项目的实测和对 5 个同类实现的调研。
- **不要跳过 §1 的锁定协议**去抢跑采集。那个文件的价值全在 git 时间戳。
- **不要用手工构造的请求对充当真实流量。** `fixtures/attribution/*.json` 是单元层，
  E4 验的是集成层，两者不可互相替代——NOTES 里那条
  「自己拼的『实测』比不测更危险」说的就是这个。
- **不要在 4.2 未成立时做 §7。**
