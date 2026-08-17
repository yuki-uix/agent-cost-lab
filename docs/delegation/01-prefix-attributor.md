# 委托任务 01 — prefix-diff 归因器

**执行方**：DeepSeek（独立 worktree）
**验收方**：Claude（review 后由验收方 push / 开 PR）
**分支**：`feat/prefix-attributor`

---

## 背景（执行方从零开始读，只需要知道这些）

这个 repo 在测量 coding agent 的 prompt cache 成本。Prompt cache 的规则是：
**请求的前缀必须逐字节相同才命中**，一旦某处不同，从那个位置往后全部按原价重算。

Anthropic 官方提供了一个诊断接口，会告诉你两次请求在哪个**组件**上分叉了
（`model` / `system` / `tools` / `messages` 四选一），但：

- 只报最早一处
- 只到组件级，不告诉你是哪个工具、哪个字段
- 只有 Anthropic 有，DeepSeek / OpenAI 都没有
- 超长对话会退化成 `unavailable`

**本任务：自己实现这个归因，做到跨 provider 且比官方更细。**

---

## 交付物

### 1. `src/agentcostlab/attribute.py`

```python
@dataclass(frozen=True)
class Divergence:
    component: str        # "model" | "system" | "tools" | "messages" | "params"
    detail: str           # 比官方细：具体哪个 tool / 第几条 message / 哪个字段
    path: list[str | int] # 结构化定位，如 ["tools", 3, "input_schema", "properties"]
    bytes_before: int     # 分叉点之前的字节数（前缀保住了多少）
    bytes_after: int      # 分叉点之后的字节数（作废了多少）

def attribute(prev_body: dict, curr_body: dict) -> Divergence | None:
    """返回第一个分叉点；完全相同则返回 None。"""
```

### 2. `tests/test_attribute.py`

### 3. `fixtures/attribution/*.json`

手工构造的请求对，每个只注入一种分叉，覆盖：

- `model` 变了
- `system` 里插了时间戳
- `tools` 增删 / 换序 / schema 键序不同（内容等价）
- `messages` 历史被截断 / 中间被编辑 / `tool_result` 重新序列化
- 完全相同（应返回 `None`）
- 多处同时不同（应只报**最早**那处）

### 4. `scripts/calibrate_attributor.py`

读 `data/raw/capture.jsonl`（里面每条记录都有 `request_body` 和官方返回的
`diagnostics`），对每一对相邻请求跑 `attribute()`，与官方
`cache_miss_reason.type` 比对，输出一致率和不一致明细。

---

## 关键设计约束

### 段顺序是一个假设，不是已知事实

前缀的拼接顺序（`tools` 在 `system` 前还是后）我**没有确认过**。

不要把某个顺序当成已知条件写死。做法：

1. 把顺序写成一个可配置常量
2. 在校准脚本里试所有合理排列
3. **报告哪种顺序与官方一致率最高**，把结论写进 PR 描述

这本身就是一个测量结果，不是实现细节。

### 等价但不同字节 ≠ 相同

cache 匹配的是**字节**。`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 语义等价但字节不同，
**必须判定为分叉**。不要用语义比较（如 `dict ==`）代替字节比较。

这是最容易做错的一点：直觉上会想"内容一样就算没变"，但缓存不这么看。

### 只跑在 raw 数据上

`request_body` 在导出闸（`redact.py`）里是 HASH 策略。归因器是**本地工具**，
跑在 `data/raw/`；只有它的**输出**（组件、字节数）可以导出。
不要修改 `redact.py` 的策略。

---

## 验收标准（AC）

AC 不是"测试通过"，是可核对的外部事实。

| # | 标准 | 怎么核 |
|---|---|---|
| **AC1** | 在留出集上，与官方 `cache_miss_reason.type` **分类一致率 ≥ 90%** | 跑 `calibrate_attributor.py`，看输出 |
| **AC2** | 每一条不一致都有归类解释 | 已知合理差异：官方只报最早一处、`unavailable`、`previous_message_not_found`。这些不算错，但必须分开统计 |
| **AC3** | 比官方更细：`tools_changed` 时能指出**具体哪个 tool 及原因**（增删/换序/schema 变） | 看 `detail` 和 `path` 字段 |
| **AC4** | 测试**调用** `attribute()`，不在测试里重写一遍 diff 逻辑 | review 读测试代码 |
| **AC5** | 单元测试不联网、可重复、不依赖 `data/raw/` 是否存在 | 断网跑一遍 |
| **AC6** | `bytes_before + bytes_after` 等于序列化后的总字节数 | 属性测试 |

**AC1 是硬门槛。** 低于 90% 不合并——但如果原因是段顺序假设错了，
改假设重跑，把这个过程写进 PR。

---

## 明确不要做的事

- ❌ 不要 `git push`，不要开 PR（验收方来做）
- ❌ 不要改 `proxy.py` / `redact.py` / `providers.py` / `pricing.py`
- ❌ 不要碰 `predictions.md`
- ❌ 不要往 `fixtures/pricing.json` 里填价格
- ❌ 不要引入新依赖（标准库 + 已有依赖足够）
- ❌ 不要为了让 AC1 达标而在校准脚本里做特判

---

## 提交要求

- commit message 全英文，格式 `type: description`
- PR 描述里必须包含：
  - 段顺序实验的结果（哪种顺序一致率最高，各是多少）
  - AC1 的实测一致率
  - AC2 的不一致分类统计
- 不要声称"全部通过"——把实际数字贴出来
