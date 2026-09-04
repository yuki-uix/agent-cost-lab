# 委托任务 11b 交付说明 — 用形状解析，而不是用形状否决

一个 commit：`fix: resolve usage by shape, not by provider label`。

改动只落在 `src/agentcostlab/ledger.py`（`SHAPE_KEYS` + `_normalise` + 两处 docstring/注释）和
`tests/test_ledger.py`。`providers.py` 的三个 extractor、`analysis.py` 的 `NonAnthropicProvider` 闸、
`predictions.md` / `fixtures/pricing.json` 一行未动。

`_normalise` 从「按标签取 `SHAPE_KEYS[label]`，不匹配就否决」改成按形状解析：

```
matches = [p for p in EXTRACTORS if any(k in usage for k in SHAPE_KEYS.get(p, ()))]
len(matches) == 1  -> normalise(matches[0], usage)
len(matches) == 0  -> None（不可测量）
len(matches)  > 1  -> None（歧义，拒绝不猜）
```

provider 标签彻底不参与读数。

---

## 一、AC1 三份 capture 实测（`cost_by_cause` 在 `data/raw/` 上的真实输出）

| capture | 断裂 | repaid | 金额区间 | saved | not_measured | disputed |
|---|---|---|---|---|---|---|
| `capture` | **0** | 0 | — | $8.6318316 | 1 | 0 |
| `capture-02` | **1** | **2822** | $0.0050796 – $0.0055986 | $7.6749732 | 0 | 0 |
| `capture-03` | **3** | 8884 | $0.0410330 – $0.0448855 | $37.8027855 | **2** | 1 |

红线全部保住，与基线 commit `4535a98` 完全一致：capture-02 的断裂 1 / repaid 2822 /
$0.0050796–$0.0055986 / saved $7.674973；capture-03 的断裂 3 / $0.041033–$0.0448855 /
not_measured 2 / saved $37.802785；capture 的断裂 0 / saved $8.631832。Kill criterion 未触发。

---

## 二、AC2 兼容端点：61 个 pair 现在判几次断裂

`fixtures/ledger/deepseek_healthy_growth.json`（62 轮 → 61 个 pair）标 `provider="deepseek"`、
usage 是 Anthropic 形状，逐 pair 跑 `broke_cache`：

- 旧行为（`4535a98` 的形状闸）：**61 个 pair 全返回 `None`**（label=deepseek ≠ shape=anthropic）。
- 新行为：**61 个 pair 全部可测（0 个 None）**，判定为 **1 次断裂**。

这 1 次断裂是 fixture 里真实存在的那次下跌：turn 41（read 164,352）→ turn 42（read 23,040），
对应 pair 下标 41。其余 60 个 pair 都是健康增长，判为没断。

同一份 usage 配 `provider="deepseek"` / `"anthropic"` / 完全缺 provider 字段，三种标签下的
判定**完全一致**（都是 `None=0, breaks=[41]`）——标签不影响读数，在真实 fixture 上也成立。

> 注意：规格 AC2 写「健康增长判定为 0 次断裂」。严格讲，这份 fixture **不是**纯单调增长：
> 它在 turn 41→42 有一次真实下跌。所以「61 个 pair 现在判几次断裂」的正确答案是 **1**，
> 不是 0。0 次断裂只在只看 0→41 的单调段时成立。详见第五节第 1 条。

---

## 三、AC3 互斥性实测：`prompt_tokens` 撞车了吗

**撞了，而且仓库自己的证据就证明了它。** `tests/test_gates.py::test_usage_normalises_to_the_same_shape`
的 DeepSeek 原生样本写的是：

```json
{"prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
 "prompt_tokens": 1000, "completion_tokens": 20}
```

`providers.py` 的 `_deepseek` 注释也写 `prompt_tokens == hit + miss`。也就是说 DeepSeek 原生
usage **确实带 `prompt_tokens`**。用旧的 `SHAPE_KEYS["openai"] = ("prompt_tokens",
"prompt_tokens_details")` 去扫描，这条 DeepSeek 样本同时命中 deepseek（`prompt_cache_hit_tokens`）
和 openai（`prompt_tokens`）两家 → 落进歧义 → `None`。AC3 的怀疑成立。

**改法**：把 openai 的形状键从 `("prompt_tokens", "prompt_tokens_details")` 换成只留
**`("prompt_tokens_details",)`**。`prompt_tokens_details` 是 OpenAI-only 的顶层键（仓库内的
DeepSeek 样本不带它），于是三家键互斥。

实测（脚本对三家代表性样本扫描，见 `_SHAPE_SAMPLES`）：

| 样本 | 旧 openai 键（含 prompt_tokens）命中的形状 | 新键命中的形状 |
|---|---|---|
| anthropic | `['anthropic']` | `['anthropic']` |
| deepseek | **`['deepseek', 'openai']`（歧义 → None）** | `['deepseek']` |
| openai | `['openai']` | `['openai']` |

修正后三家样本各自恰好命中一个形状。对应的测试
`test_each_provider_shape_matches_exactly_one_shape` 由 `EXTRACTORS` 驱动：
遍历真实注册表、从 `_SHAPE_SAMPLES` 取样本，新增 provider 没给样本会 `KeyError` 失败；
样本命中零个或多个形状会断言失败。deepseek 样本故意带 `prompt_tokens`，所以谁把 openai 的键
改回 `prompt_tokens`，这条测试立刻红——互斥性不是写在注释里，是写在测试里。

**歧义拒绝没有放宽。** `test_a_usage_matching_two_shapes_is_not_measured` 构造一条同时带
anthropic 键和 deepseek 键的 usage，断言 `None`（拒绝，不猜）。

---

## 四、测试数变化

- 基线：**272 passed**。
- 现在：**278 passed, 0 skipped, 0 failed**（`pytest -q`）。
- 净增 **+6**，全部在 `tests/test_ledger.py`（14 → 20 个用例）：
  - `test_a_mislabelled_shape_is_not_measured`（上一轮的旧形状闸测试，语义已反转）→
    **改写**为 `test_a_mislabelled_shape_is_read_by_shape_not_refused`（净 0）。
  - 新增：`test_the_label_does_not_change_the_reading`（AC4）、
    `test_a_usage_with_no_known_shape_is_not_measured`（AC5 零匹配）、
    `test_a_usage_matching_two_shapes_is_not_measured`（AC5 歧义）、
    `test_each_provider_shape_matches_exactly_one_shape`（AC3 互斥性）、
    `test_compat_endpoint_is_measured_not_refused`、`test_compat_endpoint_shrink_is_a_break`
    （AC2 兼容端点，共 +2）。
- `_payload("deepseek", ...)` 补上了真实 API 会带的 `prompt_tokens`，让交叉 provider 守恒测试
  也成为「openai 键改回 prompt_tokens 会红」的回归防线之一。

---

## 五、我认为规格不对 / 没能验证的地方

1. **AC2 的「健康增长判定为 0 次断裂」与 fixture 实际内容不符。** fixture 在 turn 41→42 有
   一次真实下跌（164,352 → 23,040），所以「61 个 pair 判几次断裂」的正确数字是 **1**，不是 0。
   规格自己在下半句也说「再造一条 read 下跌的序列断言断裂」——其实 fixture 里已经有这条下跌了，
   不必再造。我按 fixture 权威数字 `breaks == [41]` 写测试，没按「0 次断裂」的字面去断言。

2. **上一轮的 `test_a_mislabelled_shape_is_not_measured` 必须改写，规格没点名它。** 它编码的
   正是这轮要反转的旧语义（label≠shape → None）。规格「改什么」只说改 `_normalise` + 测试，
   但没提这条既存测试会红；我把它改成断言新语义（label=anthropic 的 deepseek 形状现在按
   deepseek 读、判「没断」= False 而非 None）。这不是额外加戏，是旧语义的测试不删就会红。

3. **E4 便宜臂的「测量」通了、「定价」还堵着。** 这轮只解开了 `broke_cache`/`_normalise` 这一层；
   `cost_by_cause`（定价路径）遇到 `provider="deepseek"` 记录仍被 `NonAnthropicProvider` 闸
   直接拒绝。规格明确「不要动 analysis.py 的 NonAnthropicProvider 闸」，所以我没碰。诚实记录：
   E4 现在在 ledger 层不再「不可测量」，但要出钱数字，还得等下一轮打开 analysis.py 的 provider 闸。

4. **DeepSeek 原生是否带 `prompt_tokens_details`，我没法用外部一手文档核。** 本次会话
   WebFetch/WebSearch 无权限。我依赖的是仓库内已有的一手证据（`test_gates.py` 的 DeepSeek 样本
   带 `prompt_tokens`、不带 `prompt_tokens_details`；`_deepseek` 注释 `prompt_tokens == hit + miss`）。
   据此 `prompt_tokens` 撞车是确定的、`prompt_tokens_details` 是 OpenAI-only 也是仓库内成立的。
   但若真实 DeepSeek API 的 usage 也带 `prompt_tokens_details.cached_tokens`（OpenAI 兼容风格），
   那 openai 的 `prompt_tokens_details` 键同样会撞车——这一点我在仓库内**无法验证**。它是
   「没能验证」项，不是「已验证互斥」项。

5. **形状互斥性现在有两处要同步维护的事实来源。** `SHAPE_KEYS`（ledger）和 `_SHAPE_SAMPLES`
   （测试）各自维护一份「原生 usage 长什么样」，且 `test_gates.py` 还另有一份 parametrize 样本。
   新增 provider 时三处都要加。上一轮 NOTES 已经提过 `SHAPE_KEYS` 与 `providers.py` 的同步张力，
   这轮用 `test_each_provider_shape_matches_exactly_one_shape` 的 `set(SHAPE_KEYS) ==
   set(EXTRACTORS)` 断言兜住了「SHAPE_KEYS 与 EXTRACTORS 漂移」这一侧，但「测试样本与实际 API
   形状漂移」这一侧仍然只能靠人眼核对，没有自动兜底。
