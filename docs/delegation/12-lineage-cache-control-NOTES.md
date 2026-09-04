# 委托任务 12 — 交付记录

实现：`codec.py` 新增 `strip_cache_control`（递归、不看块类型），`proxy._lineage_key`
只在哈希前剥离。`attribute.py` / `ledger.py` / `analysis.py` / `pricing.py` /
`providers.py` 一个字未动。测试新增 4 条。

---

## 1. AC1 实测 — fixture 三条记录的 lineage key 前后对比

`fixtures/lineage/cache_control_moved.json`（真实 DeepSeek 兼容端点流量，2026-09-04）。

| 记录 | 旧（哈希 messages[0] 原文）                                    | 新（剥离后哈希）                                                |
|------|----------------------------------------------------------------|-----------------------------------------------------------------|
| [0]  | `b2509c965b222dc1badbd3687d15c540779acd6682e6fa648533fd5fe72cffba` | `6612cda7d6c8973be1d0ad3a5dfd8554ab54e5e5d10c3366a9547bdac1b893f1` |
| [1]  | `6612cda7d6c8973be1d0ad3a5dfd8554ab54e5e5d10c3366a9547bdac1b893f1` | `6612cda7d6c8973be1d0ad3a5dfd8554ab54e5e5d10c3366a9547bdac1b893f1` |
| [2]  | `f05c56de01eddbd850a2251291c1a042515fd17265aa6e007b0ab6abda7297cb` | `f05c56de01eddbd850a2251291c1a042515fd17265aa6e007b0ab6abda7297cb` |

- [0] 与 [1] 同 lineage，[2] 不同：**成立**。
- 总数 3 → 2：**成立**（旧 distinct=3，新 distinct=2）。
- 与规格里给出的前后哈希逐字一致。

## 2. AC5 实测 — 三份 capture 的红线数字

用 `analysis.cost_by_cause` 跑 `data/raw/` 下三份真实 capture：

| 指标 | capture-02 | capture-03 | capture |
|------|-----------|-----------|---------|
| 断裂轮数 | **1** | **3** | **0** |
| repaid_tokens | **2822** | 8884 | 0 |
| usd_low / usd_high | **0.0050796** / **0.0055986** | **0.041033** / **0.044885** | — |
| hit_usd_saved（原始值） | 7.674973200000001 | 37.8027855 | 8.6318316 |
| hit_usd_saved（CLI `%.6f` 打印） | **$7.674973** | **$37.802785** | **$8.631832** |
| by_cause | 空 | 空 | 空 |
| ambiguous（turns/repaid/candidates） | 1 / 2822 / `('system','messages')` | 3 / 8884 / `('system','messages')` | 0 / 0 / 空 |
| disputed / not_measured | 0 / 0 | 1 / 2 | 0 / 1 |

**红线数字全部未变。** 解释：

`cost_by_cause` 配对靠的是 capture 里**已经写死**的 `injected_previous_message_id`
（`analysis._pair_by_previous`），不是重算 `_lineage_key`。`_lineage_key` 只在
proxy 的**录制路径**上生效，改它不会改变对既有 capture 的离线分析。

补充：我还按 `_lineage_key` 重数了三份 capture 的 lineage 分组（AC2 那条 #14 防线），
旧键与新键结果**完全相同**：17 / 28 / 12，合计 **57 组**。也就是说，这三份历史
capture 里没有任何一条 `messages[0]` 内发生过 `cache_control` 断点挪动——缺陷只出现
在新采集的探针流量里，历史数据不受影响。所以「剥离会让更多轮次被正确配对」在
**既有三份 capture 上不成立**，它只在重新录制时才会显现。

（一处既有事实、非本次引入：capture-03 里存在 1 个「同一 lineage 多条互不相连链」的
情形，即 record 27↔28 的模型切换——`_lineage_key` docstring 里已记录
"exactly 1 in the third, which is the model switch this fixes"。旧键下同样存在，与本次
剥离无关，是**正确的合并**而非假合并。）

## 3. AC4 实测 — record 61 仍是 k=2

`test_capture_02_break_is_ambiguous_not_system`（及全套 suite）通过。capture-02 的
唯一一次断裂仍落在 `ambiguous`，`candidates == ('system', 'messages')`，即 **k=2**，
`order_stability == 1/2`。`attribute.py` 未动，结论不变。

新增测试 `test_normalise_content_still_only_strips_text_blocks` 钉住了
`attribute._normalise_content` 只剥 text 块、保留 tool_use 块上标记的不对称，
防止将来有人统一两者。

## 4. 测试数变化

- 基线：**313 passed**
- 现：**317 passed，0 skipped**（新增 4 条：AC1 fixture 合并、AC2 不同对话仍区分、
  AC3 递归/块类型无关剥离、AC4 `_normalise_content` 不对称钉死）

只增不减。

## 5. 规格里我认为不对 / 没能验证的地方

1. **AC2 的「57 个 lineage 组零碰撞」措辞需要加限定。** 实测三份 capture 里
   capture-03 存在 1 个「同一 lineage 多条互不相连链」的情形（record 27↔28 模型切换）。
   `_lineage_key` docstring 自己就写明了这是那次模型切换修出来的**正确合并**。所以
   「零碰撞」指的应是「零**假**碰撞」（真正不同的对话被合并），而不是字面的「零条
   互不相连链」。本次剥离没有引入任何新的合并——新旧键分组逐字节相同。
2. **「剥离会改变 lineage 分组」对既有三份 capture 不成立。** 这三份里没有任何
   `messages[0]` 内的 `cache_control` 挪动，所以红线数字不可能因本次修复而变。
   这是事实，不是迁就红线。真正会变的是**重新录制**的新采集。
3. **E4 故障注入的端到端「配对上对」没有联网验证**（会花钱）。我只用
   `fixtures/lineage/cache_control_moved.json`（探针采到的真实记录）离线验证了 AC1，
   未实际重跑注入。
