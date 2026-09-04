# 委托任务 11 交付说明 — 让缓存断裂守恒式跨 provider

一个 commit：`fix: judge cache breaks by shortfall, normalized and shape-gated`。

改动只落在 `src/agentcostlab/ledger.py` + 三个调用点（`scripts/calibrate_attributor.py` 的
docstring、`tests/test_attribute.py` 里两个读真实 capture 的回归测试）+ 测试。
`attribute / proxy / redact / cli / analysis` 一行未动。

判据从「不等」改成「短缺」，且 `broke_cache` 现在消费 `providers.normalise()` 出来的
`Usage`，不再直接读 Anthropic 原始键：

```
break  ⟺  curr.cache_read  <  prev.cache_read + prev.cache_write
```

其中 `prev.cache_write = cache_write_5m + cache_write_1h + cache_write_unspecified`
（`Usage.cache_write` property，三个写入桶都在里面）。

---

## 一、AC1 三份 capture 的实测数字（`cost_by_cause` 在 `data/raw/` 上跑的真实输出）

| capture | 判定的断裂（priced） | `not_measured` | `disputed` | 命中省下 | 金额区间 / repaid |
|---|---|---|---|---|---|
| `capture` | **0**（不变） | 1（不变） | 0（不变） | $8.6318316 | — |
| `capture-02` | **1**（不变） | 0（不变） | 0（不变） | $7.6749732 | ambiguous repaid=**2822**，$0.0050796 – $0.0055986 |
| `capture-03` | **3**（旧 4） | **3 → 2** | **0 → 1** | $37.8027855 | ambiguous repaid=**8884**，$0.041033 – $0.0448855 |

红线全部保住：capture-02 的 2822 / $0.0050796–$0.0055986，capture-03 的三轮
$0.041033–$0.0448855，capture 的 $8.631832，一个字没变。Kill criterion 未触发。

capture-03 的三笔 priced 断裂仍是 idx 59（repaid 7532）、idx 65（repaid 571）、
idx 157（repaid 781），三笔都是「短缺」，与旧判据一致。

### record 66 现在落在哪个桶

`capture-03` record 66（`msg_011CeBj2FaTB5QYJAYRhyoD9`）：`expected=92,658`，
`actual=92,756`，读得比预期多 98。

- 旧判据 `!=`：判为断裂 → `_repaid` = 92,658 − 92,756 = **−98** < 0 → 丢进 `not_measured`。
- 新判据 `<`：92,756 < 92,658 为假 → **不是断裂**。走 `broke is False` 分支，
  归因器在同一对上报告 `system` 分歧（`field added: 'ttl'`，未 suppressed），
  于是落入 **`disputed_turns`**（只计数、不计价）。

也就是说它现在落在 **`disputed`（1）**，不是 `not_measured`，也不是 clean。
`not_measured_turns` 从 3 变成 2（剩下的两个是 idx 76、idx 149，`curr.usage` 缺失，
与判据无关）。规格只说「现在应判为没断」，没预测它会进 disputed —— 这里如实报告：
**没断，但归因器和 ledger 在这一对上确实分歧**，被 disputed 计数。

---

## 二、AC2 的 `EXTRACTORS` 迭代测试怎么写的

`tests/test_ledger.py::test_cross_provider_cache_conservation_agrees` 直接
`for provider in providers.EXTRACTORS:` 遍历，覆盖 **3 家**（anthropic / deepseek / openai）。

- `_payload(provider, read, write)` 按各家原生形状拼 usage：
  anthropic 用 `cache_read_input_tokens` / `cache_creation_input_tokens`；
  deepseek 用 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（隐式缓存，无写入）；
  openai 用 `prompt_tokens` + `prompt_tokens_details.{cached_tokens, cache_write_tokens}`。
- 对未知 provider，`_payload` 直接 `raise AssertionError`，而不是 skip —— 新增一家却没给
  等价 payload，这条测试会**失败**而非悄悄漏掉。
- 每一家都跑三个语义等价场景，断言判定一致：
  - 读回全部前缀（`curr.read == prev.read + written`）→ `False`
  - 少读 100（`prev.read + written − 100`）→ `True`
  - 多读 100（`prev.read + written + 100`）→ `False`
- `_written(provider, requested)` 对隐式缓存 provider 返回 0（deepseek 无写入上报），
  所以 deepseek 的「少读 100」是真正的读量下跌（`read−100 < read`）→ 断裂。

AC4 的三个分支另有独立测试（`test_exact_readback_is_not_a_break` /
`test_partial_shortfall_is_a_break` / `test_read_more_than_expected_is_not_a_break`），
其中「多读 → 没断」是 `!=`→`<` 的行为差异点，测试注释直接引用 record 66 的数字。

---

## 三、测试数变化

- 基线：**264 passed**。
- 现在：**272 passed, 0 skipped, 0 failed**。
- 净增 **8** 个，全部在 `tests/test_ledger.py`：
  三个 AC4 分支、一个三写入桶求和（`test_all_three_write_buckets_count_toward_expected`）、
  一个 AC3 形状闸（`test_a_mislabelled_shape_is_not_measured`）、
  一个 AC2 迭代（`test_cross_provider_cache_conservation_agrees`）、
  两个 AC5 隐式缓存（`test_implicit_cache_growth_is_not_a_break` /
  `test_implicit_cache_shrink_is_a_break`）。
- `tests/test_analysis.py` 里 `test_reading_more_than_expected_is_an_anomaly_not_a_negative_loss`
  **改名并改写**（`..._is_not_a_break_nor_an_anomaly`）：旧断言「多读 → not_measured」，
  新断言「多读 → 不是断裂也不是异常，`not_measured_turns == 0`、`disputed_turns == 1`」。
  净测试数不变（−1 旧名 +1 新名）。
- **0 skipped**：所有 `pytest.skip("capture-xx not present")` 守卫都没触发，因为
  `data/raw/` 三份 capture 都在。没有需要点名的缺失数据。

---

## 四、我认为规格不对 / 没能验证的地方

1. **`SHAPE_KEYS` 是 ledger 里对 provider 形状的一份平行复制。** 规格允许改 ledger、
   但不许改 `providers.py`，所以形状键只能写在 `ledger.py`。它和 `providers.py` 的
   三个 extractor 读的键必须保持同步；`broke_cache` 对 `SHAPE_KEYS.get(provider, ())`
   取空时返回 `None`（不可测量）而不是崩溃，因此新增 provider 若没同步形状键，
   `broke_cache` 会静默把它判成「不可测量」。AC2 的 `_payload` 会对未知 provider 抛错，
   能兜住「新 extractor 没有等价 payload」这一侧，但「新 provider 有 extractor 却漏了
   `SHAPE_KEYS` 条目」这一侧只在真的拿它跑 AC2 时才暴露。这是本次 scope 限制下的固有张力，
   记录在此。

2. **`score_injection.py` 的 docstring 仍是旧 `==` 恒等式。** 第 10–11 行还写着
   ``curr.cache_read == prev.cache_read + prev.cache_creation``。它调用
   `ledger_broke(prev, curr)` 传完整 record，签名没变所以功能不受影响；但 docstring
   过期了。它既不在「三个调用点」里，也不在「不要动」清单里 —— 我按严格 scope 没碰。
   建议验收方在 E4 前顺手改掉那一行。

3. **`analysis.py` 的 `_repaid < 0` 闸现在是死代码。** 新判据下 `broke is True` 意味着
   `curr.cache_read < prev.cache_read + prev.cache_write`，所以 `repaid = expected −
   curr.cache_read > 0` 恒成立，`_repaid < 0 → not_measured` 这条再也走不到。规格明确
   「不要动 analysis.py」，我没删；它无害，但将来有人读代码会看到一条不可达分支。
   对应的测试（见第三节）已改成断言新语义。

4. **规格正文内部的一处数字不一致。** 委托文档第 15 行写「cache_read 单调增长
   0 -> 100,480」，但任务正文、fixture 的 `_note` 和 fixture 实际数据都是
   `0 → 90,496`（100,480 是第 11 轮的值，不是末值）。fixture 是权威来源：62 轮、
   峰值 164,352 在第 41 轮，唯一一次下跌在第 41→42 轮（164,352 → 23,040）。
   AC5 的测试断言 `breaks == [41]`，与「真实 DeepSeek 61 pair → 1」一致。这处不一致
   不影响判据，但值得校正文档。

5. **AC3 变异守护已手动验证。** 把 `_normalise` 的形状闸改成「形状不符时按 anthropic
   归一化」会返回 `False`（两个缓存字段读 0），`assert broke_cache(...) is None` 随即
   失败。测试用 `is None` 严格断言，天然把 `False` 挡在外面。
