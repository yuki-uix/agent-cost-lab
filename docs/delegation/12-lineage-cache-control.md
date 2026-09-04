# 委托任务 12 — lineage key 不该把缓存标记当成对话身份

**执行方**：DeepSeek（独立 worktree `.claude-worktrees/lineage`，分支 `fix/lineage-cache-control`）
**验收方**：Claude
**对应 issue**：#65

---

## 背景：一次 $0.33 的探针拦下了会毁掉整场采集的缺陷

E4（#54，故障注入，要花钱）开跑前跑了一个 5 turn 的小探针。仪器侧全绿——
注入三次全部 `applied: True`、代理成功转发、零错误。**但三条记录的
`injected_previous_message_id` 全是 `None`，一对可比对的 pair 都没形成。**

根因：`proxy._lineage_key` 直接哈希 `messages[0]` 的原文。Claude Code 会
**随对话增长移动 `cache_control` 断点**，于是同一条对话在断点挪动前后被算成两条 lineage：

```
        原始 lineage       剥掉 cache_control 后
  [0]   b2509c965b222dc1   6612cda7d6c8973b
  [1]   6612cda7d6c8973b   6612cda7d6c8973b   <- 与 [0] 合并
  [2]   f05c56de01eddbd8   f05c56de01eddbd8
                           3 条 -> 2 条
```

`messages[0]` 的**唯一**差异：

```
[0]: {"cache_control": {"type": "ephemeral"}, "text": "Answer these five questions..."}
[1]: {                                        "text": "Answer these five questions..."}
```

**后果**：`ledger.broke_cache` 需要 (prev, curr) 才能判断。lineage 一分裂就没有 prev，
那一轮**根本不进入判断**——既不算「断了」也不算「没断」。#54 的 4.1 / 4.5 / 4.6
全部依赖账本配对，配不上对，失败会长得像「故障没产生断裂」——
**仪器缺陷穿着结论的外衣**。

---

## 关键设计约束（已定，不要自行发挥）

### 一、剥离逻辑放 `codec.py`，不许写第三份

`codec.py` 的 docstring 已经写明它存在的理由：

> Both the proxy (which sends them) and the attributor (which diffs them) must
> agree byte for byte... this module exists so there is nothing to keep in sync.

`proxy.py` **已经** `from .codec import serialise`，所以加一个剥离函数进去
**零新增耦合**。新增：

```python
def strip_cache_control(value: object) -> object:
    """Drop every `cache_control` key, at every depth and every block type."""
```

**递归、不看块类型**。这一点与 `attribute` 的做法不同，见约束二。

### 二、**不要动 `attribute._normalise_content`** —— 它的不对称是有意的

`attribute._normalise_content` 只剥 **`text` 块**的 `cache_control`，
`tool_use` 块上的标记会保留。**这不是 bug，不要"顺手统一"**：

- 对**分歧判定**，标记在哪个块上是有意义的——`_cache_directives` 单独比对它，
  标记从 text 块挪到 tool_use 块是一次真实的 `cache_control` 变化
- 对**对话身份**，标记完全无关——同一条首消息挪个断点仍是同一条对话

两者回答不同问题。统一它们会改变 #51 记录在案的 record 61 k=2 结论，
并可能移动红线。**本轮 `attribute.py` 一个字不许动。**

### 三、`_lineage_key` 只改一处：哈希之前剥离

```python
raw = json.dumps(strip_cache_control(first), ensure_ascii=False, sort_keys=True, default=str)
```

其余逻辑（取 `messages[0]`、sha256）不变。

---

## 验收标准（AC）

### AC1 — 真实流量 fixture

`fixtures/lineage/cache_control_moved.json` 是探针采到的三条**真实记录**
（经 DeepSeek 兼容端点，2026-09-04）。断言：

- 记录 [0] 与 [1] 的 lineage key **相同**（它们是同一条对话，只差一个被挪动的标记）
- 记录 [2] 与它们**不同**（确实是另一条对话）
- 三条的 lineage 总数从 **3 变成 2**

### AC2 — 不许弱化 #14 的防线（假合并）

lineage key 仍必须区分**真正不同**的对话。加测试：两条 `messages[0]` **内容**不同
的请求，剥离 `cache_control` 之后仍是不同 lineage。

`report/99` 记过：三份 capture 共 57 个 lineage 组零碰撞。**这条不许退化。**

### AC3 — 剥离是递归且不看块类型的

- 顶层 `cache_control`、嵌套在 `content` 数组里的、`tool_use` / `tool_result` 块上的——
  全部剥掉
- 一条测试构造多种块类型各带标记，断言剥离后完全一致

### AC4 — `attribute` 的行为一个字不变

- `attribute._normalise_content` 仍只剥 text 块（**加一条测试钉住这个差异**，
  说明两者为何不同，防止将来有人统一它们）
- record 61 仍是 **k=2**（`system` 与 `messages` 各当选 60/120）

### AC5 — 防回归红线

- `capture-02`：断裂 **1**、`repaid=2822`、`$0.0050796`–`$0.0055986`、`saved=$7.674973`
- `capture-03`：断裂 **3**、`saved=$37.802785`、`$0.041033`–`$0.044885`
- `capture`：断裂 **0**、`saved=$8.631832`
- **注意**：剥离会改变 lineage 分组，可能让**更多**轮次被正确配对。
  若上述数字发生变化，**停下来如实报告，不要调整实现去迁就红线**——
  红线是在旧的（有缺陷的）分组下算出来的，新分组下的变化可能是修复的正确结果。
  但要能解释清楚每一处变化的来源。
- 基线测试数 313，只增不减

---

## Kill criterion

若剥离后 `capture-02` / `capture-03` 的断裂数发生变化：**报告并解释**，
不要为了让红线不动而收窄剥离范围。红线的价值在于它反映真实数据，
不在于它永远是那个数。

---

## 明确不要做的事

- 不要 `git push` / 开 PR / 用 `gh`（验收方来做）
- 只改 `codec.py`（新增剥离函数）+ `proxy.py` 的 `_lineage_key` + 测试
- **不要动 `attribute.py` / `ledger.py` / `analysis.py` / `pricing.py` / `providers.py`**
- 不要碰 `predictions.md` / `fixtures/pricing.json`，不要引入新依赖
- `proxy` 不许 import `attribute`

## 交付要求

**做完后 `git add -A && git commit`**（英文，`fix: description`）。

写 `docs/delegation/12-lineage-cache-control-NOTES.md`：

1. AC1 的实测：三条 fixture 记录的 lineage key，前后对比
2. AC5 三份 capture 的实测数字；**若有变化，逐处解释来源**
3. AC4：record 61 仍是 k=2 的实测
4. 测试数变化
5. 你认为规格不对或没能验证的地方

**不要声称「全部通过」。把数字贴出来。**
