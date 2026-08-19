# E1 cache miss 成因分布

> 这一章要回答：真实 session 里 miss 因何而起？自建归因器与官方 cache_miss_reason 的一致率。

_未开始，原因是没有样本，不是没有分析。_ 所有数字必须带 [一手] / [实测] / [二手·未核] 标签，见 ../docs/sources.md。

## 现状：0 个官方 verdict [实测 2026-08-18]

四份 capture 里，正确串接（#14 之后）的 `cache_miss_reason` 数量是 **0**。

| 文件 | 记录数 | 带 diagnostics |
|---|---|---|
| capture-attempt1-failed.jsonl | 67 | 0 |
| capture-attempt2-nogzipfix.jsonl | 4 | 0 |
| capture-attempt3-prelineagefix.jsonl | 16 | 4（全部是 #14 修掉的跨 lineage 串线产物） |
| capture.jsonl | 75 | 0 |

主 capture 的 63 个 threaded turn 全部返回 `diagnostics: null`。这**不是**仪器故障：
attempt3 里 null 与 dict verdict 在同一份文件内共存，且每条 null 记录的
`cache_read_input_tokens` 都是满的——`null` 的含义是「比过了，命中」。

所以这份数据是真的，它只是**没有 miss**。

## 第二份 capture（2026-08-19，66 条）：第一次真实断裂 [实测]

采集时计划的三个动作（换模型、`/compact`、增删 MCP）**一个都没做成**，会话中途断线。全程一个模型，最长链的 messages 从 2 单调涨到 89，没有压缩痕迹。

但它带来两个结果。

**一、beta 通道确认可用。** 49/49 个可作答 turn 都带回了 `diagnostics` 字段，干净命中时值为 `null`。所以 `null` 的含义确定为「比对过了、没断」，而不是「接口没生效」——这个歧义此前无法从数据中分辨。

**二、缓存断了一次，机制是 TTL 切换。** 记录 61：

| | |
|---|---|
| 预期读取 | 157,991 |
| 实际读取 | 155,169 |
| 差额 | **2,822 token 付了两遍** |
| 官方诊断 | `{"cache_miss_reason": {"type": "unavailable"}}` —— **答不上来** |
| 自建归因器 | `system` / `field removed: 'ttl'` / `['system', 1, 'cache_control', 'ttl']` |

全库 66 条里 65 条带 `ttl: "1h"`，只有这一条掉了这个字段；写入桶也随之裂成 `ephemeral_1h: 251,052` 与 `ephemeral_5m: 1,038`。**Claude Code 在某一轮把缓存 TTL 从 1 小时换成默认，1 小时桶里的尾巴在新桶里找不到。**

这是归因器第一次在真实断裂上工作，且给出了比官方接口更具体的答案。**n=1，不作规律陈述。**

分析这一条时发现归因器有个盲点：同样的改动若只发生在 `messages` 里会被完全漏掉（规范化会剥掉那里的 `cache_control`）。已修（#34），并以这一对真实数据作回归。

## 因此

- **1.1（成因分布）** 仍无法开始：两份 capture 合计仍是 **0 个有结论的官方 verdict**（capture-02 有 1 个 `unavailable`，属于「API 无法判定」，不计入）。
- **1.2（分叉率）** 可以做：干净 session 是合法数据，正是分叉率的分母。
- **#10（与官方对齐）** 无法开始：参照系只有 `no_divergence` 一个类别，
  恒定回答「没断」的空函数也得 100%。`scripts/calibrate_attributor.py`
  现在会拒绝在这种数据上给出一致率。

## 一个结构性偏差，必须随分布一起引用

即使拿到了样本，**1.1 的分布也不会包含 compaction 造成的 miss**。代理靠 `messages[0]`
认对话，而 `/compact` 会开出一段首条消息全新的对话，压缩后第一个请求因此拿不到任何
裁决。2026-08-19 决定接受这个盲点（#42），因为两条修法各自带来的正确性风险，都大于
一个尚未证明有价值的观测。

这意味着 1.1 给出的是**条件分布**——「在非 compaction 的断裂里，谁占大头」。而
1.3 实测显示，survey 列的六个「缓存杀手」里真实出现过的**只有 compaction 一个**。
所以这个条件很可能把最大的一类排除在外了。任何引用 1.1 的地方都必须带上它。

## 前置条件

需要一份**缓存真的断过**的 capture。要制造的是 reason 枚举的并集，不是「多用一会儿」：
切模型、`/compact`、增删 MCP server、改 system prompt、闲置超过 cache TTL。

在那之前，本章的任何数字都会是无样本推断。
