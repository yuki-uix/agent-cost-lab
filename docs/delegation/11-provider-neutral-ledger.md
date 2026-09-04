# 委托任务 11 — 让守恒式跨 provider

**执行方**：DeepSeek（独立 worktree `.claude-worktrees/ledger`，分支 `fix/provider-neutral-ledger`）
**验收方**：Claude
**对应 issue**：#57

---

## 背景：这不是理论问题，是一场采集的阻塞

E4（#54，故障注入）原计划把跑量部分放在 DeepSeek 上省钱。验收方在开跑前用**本仓库
自己 delegate 跑出来的真实 DeepSeek 数据**做了检查，结果：

```
一段完全健康的 DeepSeek 会话（cache_read 单调增长 0 -> 100,480）
ledger.broke_cache 判定：61 个可比对 pair，断裂 61 个 —— 100% 假阳性
```

**原因**：`expected = prev.cache_read + prev.cache_creation` 是 Anthropic 专用式子。
它假设显式 `cache_control` 写入——「这轮写的下轮读回」。DeepSeek 的 Anthropic 兼容端点
（`https://api.deepseek.com/anthropic`）返回 **Anthropic 形状的字段名**，但：

- `cache_read_input_tokens` 有真值
- **`cache_creation_input_tokens` 恒为 0**（自动缓存，无写入计费）
- `cache_creation` 明细缺失（`ephemeral_5m/1h` 均为 `None`）

于是式子退化成「read 必须逐轮不变」，而自动缓存的前缀是**随对话增长的**，每一轮都判断裂。

**没有任何闸挡得住**：`proxy.py` 的 `PROVIDER = os.environ.get("ACL_PROVIDER", "anthropic")`
默认就是 anthropic；`scripts/score_injection.py` 直连 `ledger_broke`，压根不过
`cost_by_cause` 那道 `NonAnthropicProvider` 闸。会静默出数，而我们会把 100% 当数据读。

---

## 验收方已经定死的设计：判据从「不等」改成「短缺」

**不要自行设计跨 provider 规则。** 下面这条已在两边真实数据上验证过。

共同不变量是：**上一轮已缓存的内容，这一轮应该还在。**

```
break  ⟺  curr.cache_read  <  prev.cache_read + prev.cache_write_total
```

- **Anthropic**：写入有上报，所以 `prev.read + prev.write` 就是「应该读回多少」，
  少读即断裂（含**部分**断裂，如 capture-02 记录 61：应读 157,991、实读 155,169）
- **隐式缓存 provider**（DeepSeek/OpenAI）：写入不上报，`write_total = 0`，
  式子成为 `curr.read < prev.read`——前缀**缩水**即断裂，而正常增长不再误报

### 已验证（验收方，2026-09-04，真实数据）

| 数据 | 旧判据 `!=` | 新判据 `<` |
|---|---|---|
| `capture-02` | 1 | **1**（不变） |
| `capture-03` | 4 | **3** |
| `capture` | 0 | **0**（不变） |
| 真实 DeepSeek 会话（61 pair） | **61（100% 假阳性）** | **1** |

`capture-03` 少掉的那个是 **record 66**：`expected=92,658 actual=92,756`，
**读得比预期多 98 个 token**。旧判据把它判成断裂，再因为 `repaid` 为负丢进
`not_measured`。新判据下它根本不是断裂——**同样的结果，少一个特例**。

### 一条必须写进 docstring 的诚实限制

在隐式缓存 provider 上，这条规则只能检出**缩水**，检不出**增长不足**——因为没有
写入上报，我们不知道「本该缓存多少」。这是隐式缓存的固有限制，不是实现缺陷。

E4 的五种注入故障全部改动前缀的**靠前部分**（system / tools / model），
会导致读取量**大幅下跌**而非增长不足，所以这条限制不影响 E4 的可测性——
但必须写明，不能让将来的人以为它是全量检出器。

---

## 交付物

### 1. `src/agentcostlab/ledger.py` — 判据改为短缺，且消费归一化用量

- `broke_cache` 改成读 `providers.normalise()` 出来的 `Usage`，不再直接读 Anthropic 原始键
- `expected = prev.cache_read + prev.cache_write_5m + prev.cache_write_1h + prev.cache_write_unspecified`
  —— **三个写入字段都要算进去**，现在读的单一 `cache_creation_input_tokens` 在 Anthropic 上
  恰好等于前两者之和，换成 `Usage` 之后不要漏掉 `unspecified`
- 判据 `curr.cache_read < expected`
- **`ledger` 仍然不许 import `attribute`**——它是用来检验归因器的 oracle，不能共享被检验者的推理

### 2. 形状闸：不认识的 usage 形状必须走「不可测量」，不是「没断」

这是 issue #57 追加 AC 的核心。**闸要守 usage 的形状，不是守 provider 的标签。**

一条 `provider="anthropic"` 而 usage 键是别家形状的记录（标签错、或代理没设
`ACL_PROVIDER`），必须返回 `None`（不可测量），**不能返回 `False`（没断）**。

### 3. 三个调用点全部更新

- `scripts/calibrate_attributor.py`
- 两个 attributor 回归测试（`tests/test_attribute.py` 里读真实 capture 的那些）

### 4. 测试

---

## 验收标准（AC）

### AC1 — 防回归红线（Anthropic 一个字不许变）

- `capture-02`：断裂 **1** 轮，`repaid_tokens=2822`，`usd_low=0.0050796`，`usd_high=0.0055986`
- `capture-03`：断裂 **3** 轮，命中省下 `$37.802785`，`$0.041033 – $0.044885`
- `capture`：断裂 **0** 轮，命中省下 `$8.631832`
- `capture-03` 的 **record 66 现在应判为「没断」**（不再是先判断裂再丢进 not_measured）。
  如实报告它现在落在哪个桶、`not_measured_turns` 从 3 变成几

### AC2 — 跨 provider 一致性，用 `EXTRACTORS` 迭代驱动

**不要写成三个手抄的用例。** 构造一组语义等价的用量（同样的「读回上一轮全部前缀」
和「少读 N 个」），分别以每个已注册 provider 的 payload 形状表达，断言
`broke_cache` 的判定一致。

**遍历 `providers.EXTRACTORS`**，新增 provider 自动纳入这条测试——
新增一家却没给等价 payload 就该失败，而不是悄悄漏掉。

### AC3 — 形状不认识 → 不可测量

- 构造一条 `provider` 与 usage 形状**不一致**的记录，断言 `broke_cache` 返回 `None`
- **变异守护**：把这条路径改成返回 `False`，测试必须失败

### AC4 — 短缺判据的三个分支各有测试

- 恰好读回（`curr.read == expected`）→ 没断
- 少读（部分断裂，`prev.read < curr.read < expected`）→ 断了
- 多读（`curr.read > expected`）→ **没断**（不是断裂，也不是异常）

### AC5 — 隐式缓存 provider 的行为

- 写入恒 0、read 单调增长 → 全程无断裂
- 写入恒 0、read 下跌 → 断裂

### AC6 — 既有闸不许弱化

三桶穷尽性、`order_stability` 枚举验证、桶容量约束、单位闸、未核实费率闸、
`AmbiguousCacheWrite` 传播、退化输入拒绝——全部不动。基线测试数只增不减。

---

## 本次明确**不做**的事（scope 已收窄，不要扩）

**不要动 `analysis.py` 里的 `NonAnthropicProvider` 闸。** issue #57 的原 AC 提到要撤掉它，
**本轮不撤**，理由：DeepSeek 的费率目前 `verified=False`，`pricing.cost()` 本来就会拒绝，
所以那道闸今天不造成任何损害；而撤掉它会把一条**从未测过的计价路径**放进 E4 的关键依赖里。
等 DeepSeek 费率核实之后单独处理。

E4 需要的只是 **ledger 能在 DeepSeek 上正确判断裂**（`score_injection.py` 直连它），
不需要 `cost_by_cause` 能给 DeepSeek 计价。

其余：
- 不要 `git push` / 开 PR / 用 `gh`（验收方来做）
- 不要改 `attribute.py` / `proxy.py` / `redact.py` / `cli.py`
- 不要碰 `predictions.md` / `fixtures/pricing.json`
- 不要引入新依赖

## Kill criterion

若短缺判据在 Anthropic 真实数据上**改变了红线数字**（capture-02 的 2822 /
capture-03 的三轮金额）：**停下来如实报告，不要调整判据去迁就红线**。
红线是验收方在真实数据上算出来的，判据也是；两者冲突说明有一方错了，
而不是应该把其中一方掰弯。

## 交付要求

**做完后 `git add -A && git commit`。** commit message 全英文，格式 `fix: description`。

写 `docs/delegation/11-provider-neutral-ledger-NOTES.md`：

1. AC1 三份 capture 的实测数字，以及 record 66 现在落在哪个桶
2. AC2 的 `EXTRACTORS` 迭代测试怎么写的、覆盖了几家
3. 测试数变化（`N passed / M skipped`），skip 的每条点名缺什么
4. 你认为规格不对、或没能验证的地方，如实写出来

**不要声称「全部通过」。把数字贴出来。**
