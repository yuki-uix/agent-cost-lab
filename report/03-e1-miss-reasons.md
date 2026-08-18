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

## 因此

- **1.1（成因分布）** 无法开始：分布需要至少一个 verdict。
- **1.2（分叉率）** 可以做：干净 session 是合法数据，正是分叉率的分母。
- **#10（与官方对齐）** 无法开始：参照系只有 `no_divergence` 一个类别，
  恒定回答「没断」的空函数也得 100%。`scripts/calibrate_attributor.py`
  现在会拒绝在这种数据上给出一致率。

## 前置条件

需要一份**缓存真的断过**的 capture。要制造的是 reason 枚举的并集，不是「多用一会儿」：
切模型、`/compact`、增删 MCP server、改 system prompt、闲置超过 cache TTL。

在那之前，本章的任何数字都会是无样本推断。
