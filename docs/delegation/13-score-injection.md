# 委托任务 13 — 打分脚本在真实数据上崩溃

**执行方**：DeepSeek（独立 worktree `.claude-worktrees/score`，分支 `fix/score-injection`）
**验收方**：Claude
**对应 issue**：#68

---

## 背景：E4 花了 $0.54 采到数据，才发现打分工具是坏的

`scripts/score_injection.py` 是 E4 三方对照打分的唯一入口。**它从未在真实数据上跑过**——
`docs/e4-tasks.md` §0 自己写着「没做的：一次真实采集都还没跑」，#57 的规格也把
「离线跑通它」列为花钱前该做完的事。没做。

于是 B2 采集（I0 + I3，$0.54）拿到数据之后，打分第一步就炸：

```
TypeError: 'Divergence' object is not iterable
  line 133: divs = [d for d in getattr(result, "divergences", result) if not d.suppressed]
```

`attribute()` 的签名是 `-> Divergence | None`，返回**单个** `Divergence`。
脚本期望可迭代集合，`getattr(result, "divergences", result)` 找不到属性就回落到
`result` 本身，迭代即抛错。**E4 全部五个 arm 的打分都走这条路径。**

---

## 已经给你准备好的

`fixtures/injection/` 下有 **B2 真实采到的两份注入 capture**——这是本仓库第一份带
`injection` 字段的数据：

| fixture | 记录数 | 内容 |
|---|---|---|
| `capture-inject-i0.jsonl` | 3 | baseline，`applied: false`，1 对可配对 |
| `capture-inject-i3.jsonl` | 4 | I3 arm，**故障从未开火**（turn 最高 3，阈值是 4） |

`data/raw/` 下有三份既有 capture（**无** `injection` 字段），用于验证拒绝闸。

---

## 关键设计约束

### 一、不要改 `attribute()` 的返回类型去迁就脚本

**单个 `Divergence` 是 #51 确立的契约**：`candidates` / `order_stability` 都挂在它上面，
`analysis.cost_by_cause` 依赖这个形状。改它会波及整条计价链。

**要改的是脚本，不是被调用方。**

### 二、拒绝闸不许弱化

无 `injection` 字段的 capture 必须**如实拒绝**（现有行为，实测正常）：

```
data/raw/capture-02.jsonl: no `injection` field on any record. This capture does
not declare a campaign arm and cannot be scored as one.
```

这道闸防的是「把没声明 arm 的 capture 当成某个 arm 打分」。修 bug 时不许把它
弱化成静默跳过。

### 三、三方对照的语义不许改

脚本 docstring 写明的设计意图：

> Agreement across all three is strong evidence. Any pairwise disagreement is a
> finding in its own right and is printed rather than reconciled.

- **A** = 注入的原因（构造出来的，最强）
- **B** = `ledger.broke_cache`（守恒式，与归因器不共享推理）
- **C** = 官方 `cache_miss_reason`（只有 Anthropic 有）

**任何两方分歧要打印出来，不许调和掉。** `suppressed` 的分歧仍排除在归责之外。

### 四、「故障从未开火」是一种合法状态，不是错误

`capture-inject-i3.jsonl` 里所有记录 `applied: false`——因为 turn 数没达到阈值（#69）。
脚本应当**如实报告「这个 arm 的故障一次都没开火」**，而不是崩溃、也不是假装打了分。
这是 E4 会真实遇到的情形。

---

## 验收标准（AC）

### AC1 — 两份真实 fixture 都能跑通

- `capture-inject-i0.jsonl` → 不抛异常，报告 baseline 的对照结果
- `capture-inject-i3.jsonl` → 不抛异常，**明确报告「故障未开火」**
- 把两份的**实际输出全文**贴进 NOTES

### AC2 — 拒绝闸仍然生效

三份既有 capture（`capture-02` / `capture-03` / `capture`）各跑一次，
断言都被拒绝且信息不变。**变异守护**：把拒绝改成静默跳过，测试必须失败。

### AC3 — 回归测试

- 喂一对产生单个 `Divergence` 的记录，断言归责正确且不抛
- 喂一对 `attribute()` 返回 `None`（无分歧）的记录，断言归责为 `None` 且不抛
- 喂一个 `suppressed=True` 的分歧，断言它**不**被计入归责

### AC4 — 三方对照的输出结构

- A/B/C 三方各自的统计仍分开呈现
- 至少一条测试断言「pairwise 分歧被打印而非合并」

### AC5 — 不许弱化既有闸

`ledger` / `attribute` / `analysis` / `pricing` / `providers` **一个字不许动**。
基线测试数 **319**，只增不减。红线：`capture-02` 断裂 1、`capture-03` 断裂 3、
`capture` 断裂 0。

---

## Kill criterion

若修复需要改 `attribute()` 的返回契约才能干净实现：**停下来如实报告**，
不要单方面改契约。那是跨模块的决定，由验收方裁定。

## 明确不要做的事

- 不要 `git push` / 开 PR / 用 `gh`（验收方来做）
- 只改 `scripts/score_injection.py` + 测试
- 不要动 `src/agentcostlab/` 下的任何模块
- 不要碰 `predictions.md` / `fixtures/pricing.json`，不要引入新依赖
- **不要为了让 I3 那份 fixture「有分数」而伪造开火状态**

## 交付要求

**做完后 `git add -A && git commit`**（英文，`fix: description`）。

写 `docs/delegation/13-score-injection-NOTES.md`：

1. 两份真实 fixture 的**输出全文**
2. 三份既有 capture 的拒绝信息（证明闸未弱化）
3. 测试数变化
4. 你认为规格不对或没能验证的地方

**不要声称「全部通过」。把数字贴出来。**
