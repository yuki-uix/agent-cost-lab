# 委托任务 11b — 用形状**解析**，而不是用形状**否决**

**执行方**：DeepSeek（复用 worktree `.claude-worktrees/ledger`，分支 `fix/provider-neutral-ledger`）
**验收方**：Claude
**承接**：commit `4535a98`

---

## 这一轮修的是验收方规格里的错，不是你的实现

上一轮交付**完全忠于规格**，红线、`cache_write` 三字段、`EXTRACTORS` 真迭代、
`ledger` 不 import `attribute` 全部达标，272 passed。**问题在规格本身。**

规格写的是：「标签与形状不符 → 返回 `None`」。实现照做了。但真实场景是：

```
E4 的便宜臂：Claude Code 指向 DeepSeek 的 Anthropic 兼容端点
  https://api.deepseek.com/anthropic

  provider 标签 = "deepseek"        ← 对的，DeepSeek 才是收钱的那家
  usage 形状   = Anthropic 的        ← 也是对的，兼容端点就返回这个形状
                 (cache_read_input_tokens 有真值，cache_creation 恒 0)
```

**两个都对，却被判成冲突。** 实测：真实 DeepSeek 序列 61 个 pair 全部返回 `None`。
E4 的便宜臂**仍然跑不了**，只是从「61 个假阳性」变成了「61 个不可测量」。

### 正确的语义

**形状决定怎么读数，标签决定按谁的费率计价。这是两个不同的问题。**

- `broke_cache` / `_normalise` 只关心「这堆数字是什么形状」——**不看标签**
- `cost_by_cause` 继续用标签查费率——**本轮不动它**

---

## 要改的（只有 `_normalise` 一个函数 + 测试）

把「按标签取 `SHAPE_KEYS[label]`，不匹配就否决」改成 **按形状解析**：

1. 扫描全部已注册 provider，收集那些 `SHAPE_KEYS` 出现在这条 usage 里的
2. **恰好一个匹配** → 用那家的 extractor 归一化
3. **零个匹配** → `None`（不可测量）
4. **多于一个匹配** → `None`（有歧义。形状本该互斥，撞车说明注册表有问题，
   拒绝而不是猜）

`provider` 标签**不再参与读数**。usage 缺失或为空仍然 → `None`（既有行为不变）。

---

## 验收标准（AC）

### AC1 — 红线一个字不许动

- `capture-02`：断裂 **1**、`repaid=2822`、`$0.0050796`–`$0.0055986`、`saved=$7.674973`
- `capture-03`：断裂 **3**、`saved=$37.802785`、`$0.041033`–`$0.044885`、`not_measured=2`
- `capture`：断裂 **0**、`saved=$8.631832`
- 测试数只增不减（基线 272）

### AC2 — 兼容端点场景必须可测

用 `fixtures/ledger/deepseek_healthy_growth.json`（真实 DeepSeek 会话，62 轮，
read 0→90,496，creation 恒 0）：

- 记录标 `provider="deepseek"`、usage 是 **Anthropic 形状**
- 断言：**不再返回 `None`**，而是按 Anthropic 形状解析，健康增长判定为
  **0 次断裂**（旧判据是 61 次假阳性）
- 再造一条同样标 `deepseek`、read **下跌**的序列，断言判定为断裂

### AC3 — 形状互斥性必须被测试，不能只写在注释里

上一轮的 `SHAPE_KEYS` 注释声称三家形状「disjoint on purpose」。**这是个未经检验的断言，
而且很可能是错的**：`openai` 的键写着 `("prompt_tokens", "prompt_tokens_details")`，
而 DeepSeek 原生 usage **很可能也带 `prompt_tokens`**——那样一条 DeepSeek 原生 payload
会同时匹配两家，落进「歧义 → None」。

要求：

- 写一条**由 `EXTRACTORS` 驱动**的测试，对每家 provider 的**代表性 usage 样本**断言
  「恰好匹配一个形状」
- **如果互斥性实际不成立，如实报告并修正 `SHAPE_KEYS`**（例如给 openai 用更有辨识度的键），
  不要为了让测试通过而放宽判据
- 新增 provider 却没给样本，这条测试应当失败——**不要写成三个手抄用例**

### AC4 — 标签不再影响读数

- 同一份 usage，配 `provider="anthropic"` / `"deepseek"` / 完全缺失 `provider` 字段，
  `broke_cache` 的判定必须**完全一致**
- **变异守护**：把解析改回「按标签取」，这条测试必须失败

### AC5 — 零匹配与歧义各有测试

- usage 里一个已知形状的键都没有（例如 `{"foo": 1}`）→ `None`
- 人为构造一条同时匹配两家形状的 usage → `None`

---

## 明确不要做的事

- 不要 `git push` / 开 PR / 用 `gh`
- **只改 `src/agentcostlab/ledger.py` 的 `_normalise`（及必要的 `SHAPE_KEYS`）+ `tests/test_ledger.py`**
- 不要动 `analysis.py` 的 `NonAnthropicProvider` 闸（scope 仍然收窄，理由见上一轮规格）
- 不要动 `providers.py` 的 extractor 逻辑；若 `SHAPE_KEYS` 需要更有辨识度的键，
  改的是 `ledger.py` 里那张表，不是 `providers.py`
- 不要碰 `predictions.md` / `fixtures/pricing.json`，不要引入新依赖

## Kill criterion

若形状互斥性在实际样本上**不成立**且无法通过换键解决：
**停下来如实报告**，不要为了让流程跑通而把歧义静默解析成某一家。
歧义解析成一家 = 把「读错数」伪装成「读到数」，比拒绝更糟。

## 交付要求

**做完后 `git add -A && git commit`**（英文，`fix: description`）。

写 `docs/delegation/11b-resolve-by-shape-NOTES.md`：

1. AC1 三份 capture 实测数字
2. AC2 兼容端点场景的实测：61 个 pair 现在判定为几次断裂
3. **AC3 的互斥性实测结果**——三家形状是否真的互斥？`openai` 的 `prompt_tokens`
   有没有和 DeepSeek 撞车？如果撞了，你怎么改的
4. 测试数变化
5. 你认为规格不对或没能验证的地方

**不要声称「全部通过」。把数字贴出来。**
