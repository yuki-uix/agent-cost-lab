# CLI 评审四条修正 — delivery notes

一个 commit：`fix: correct default capture selection, .venv pin, refusal coverage, one-break interval`

改动只落在 `src/agentcostlab/cli.py` + `scripts/capture.sh` + `tests/test_cli.py`。
`ledger / attribute / providers / proxy / redact / analysis / pricing` 一行未动。

---

## 一、四条各自的修法与新增测试

### P1-a · 默认选文件（数字序 + 排除 attempt）

`_default_capture()` 原来 `RAW_DIR.glob("capture-*.jsonl")` 取字典序最后一个，会选中
`capture-attempt3-prelineagefix.jsonl`。改成用正则 `^capture-(\d+)\.jsonl$`
（`re.fullmatch(r"capture-(\d+)\.jsonl", p.name)`）过滤，解析出整数、按**数字大小**取最大，
非纯数字后缀一律不参与；一个 numbered 都没有时退回 `capture.jsonl`（兜底不变）。

新增测试：

- `test_default_capture_prefers_highest_numbered_and_ignores_attempts` — tmp 目录放
  `capture-02` / `capture-10` / `capture-attempt3-prelineagefix`，断言选中 `capture-10`
  （既验数字序 10 > 2，又验 attempt 被排除）。
- `test_default_capture_falls_back_when_no_numbered_capture` — 无 numbered 时退回
  `capture.jsonl`。

### P1-b · `.venv` 硬编码

`capture.sh` 三处 `.venv/bin/python`（41/72/98 行）改成
`"${ACL_PYTHON:-.venv/bin/python}"`，默认值保留，直接跑脚本语义一字不变。
`cli._cmd_record` 转发时传 `env={**os.environ, "ACL_PYTHON": sys.executable}`。

新增测试：

- `test_record_points_capture_sh_at_the_running_interpreter` — monkeypatch
  `subprocess.run`，调 `_cmd_record("status")`，断言 `env["ACL_PYTHON"] == sys.executable`。

### P2-a · 漏捕获的两种定价拒绝

`run_diagnose` 的 try 块补两个 except（逐个类型列，没有用 `except ValueError` 一网打尽）：

- `RepaidExceedsCapacity`（analysis）→ exit 2 + 原因 + next step（守恒式前提不成立，多半
  仪器异常，建议核对该轮 usage）。
- `AmbiguousCacheWrite`（pricing）→ exit 2 + 原因 + next step（无 TTL 写入且两档价不同，
  建议核对 `cache_creation` 明细）。

新增测试：

- `test_diagnose_refuses_repayment_exceeding_capacity` — 构造 prev `cache_read=1000,
  creation=200`、curr `cache_read=0, input_tokens=100`，`repaid=1200 > 容量 100`，断言
  `run_diagnose` 返回 `(2, ...)` 且文本含 `Next step`。
- `test_diagnose_refuses_ambiguous_cache_write` — 构造 curr usage 只有
  `cache_creation_input_tokens=500` 无 TTL breakdown，断言返回 2 而非抛出。

### P2-b · 单次断裂成本改成费率区间

`_model_stats` 里 `one_break` 点估计拆成 `one_break_low` / `one_break_high`：

```
one_break_low  = typical * (input_uncached - cache_read) / 1e6
one_break_high = typical * (cache_write_1h - cache_read) / 1e6
```

输出改成 `one break ~ $LOW – $HIGH (median prefix N tokens)`，与损失行一致；没有编容量
约束、没有保留单点加脚注。

新增测试：

- `test_one_break_is_a_rate_interval_not_a_point_estimate` — 拿 fixtures 真实 opus 费率
  （input_uncached 5.0 / cache_read 0.50 / cache_write_1h 10.0）手算常量对比：low 用
  uncached、high 用 1h，且 `one_break_high > one_break_low`。

### 测试数变化

```
基线        252 passed
本任务后    258 passed   （+6，全部在 tests/test_cli.py，0 skip）
```

---

## 二、修完后 capture-02 / capture-03 的 diagnose 实际输出

`agentcostlab diagnose data/raw/capture-02.jsonl`（exit 0）：

```
Money
  cache hits saved   $7.674973  (4,263,874 read tokens)
  cache misses cost  $0.005080 – $0.005599  (1 turn, 2,822 tokens paid twice)

Read savings by model
  claude-sonnet-5    4,263,874 read tokens -> $7.674973 saved
    one break ~ $0.149378 – $0.315354 (median prefix 82,988 tokens)

Cause breakdown  (attribution NOT yet validated on a real break — #54 fault injection pending)
  by_cause:  none
  ambiguous: 1 turn (100% of 1 breaks), 2,822 tokens, $0.005080 – $0.005599  [candidates, not causes: system, messages]
  unattributed: none
```

红线数字：`repaid=2822`（`2,822 tokens paid twice`）、`usd_low=0.0050796` → `$0.005080`、
`usd_high=0.0055986` → `$0.005599` —— 逐位不变。唯一变化是 `one break` 那行从点估计
`$0.149378` 变成区间 `$0.149378 – $0.315354`（P2-b 预期）。

`agentcostlab diagnose data/raw/capture-03.jsonl`（exit 0）：

```
Money
  cache hits saved   $37.802785  (10,984,678 read tokens)
  cache misses cost  $0.041033 – $0.044885  (3 turns, 8,884 tokens paid twice)

Read savings by model
  claude-opus-5      6,677,913 read tokens -> $30.050609 saved
    one break ~ $0.423027 – $0.893057 (median prefix 94,006 tokens)
  claude-sonnet-5    4,306,765 read tokens -> $7.752177 saved
    one break ~ $0.102568 – $0.216532 (median prefix 56,982 tokens)

Cause breakdown  (attribution NOT yet validated on a real break — #54 fault injection pending)
  by_cause:  none
  ambiguous: 3 turns (100% of 3 breaks), 8,884 tokens, $0.041033 – $0.044885  [candidates, not causes: system, messages]
  unattributed: none
  not measured: 3 turns
```

红线数字：`$37.802785`、断裂 3 轮 `$0.041033 – $0.044885` —— 逐位不变。opus 的 one-break
区间 `$0.423027 – $0.893057` 正是规格里给的那个例子（median prefix 94,006 tokens）。

---

## 三、裸 diagnose 现在选中的文件

修前裸 `agentcostlab diagnose` 会崩（选中 `capture-attempt3-prelineagefix.jsonl`，
`RepaidExceedsCapacity` 逃逸成 traceback）。修后裸 `agentcostlab diagnose`（exit 0）选中
**`data/raw/capture-03.jsonl`**，输出如上（`hit_usd_saved = $37.802785`、opus+sonnet 两个
模型段落），不再是 attempt 废样本。

---

## 四、规格里不对、或没能验证的地方

1. **`bash -n scripts/capture.sh` 被权限闸挡住，没跑成。** 三处替换是标准的
   `${ACL_PYTHON:-.venv/bin/python}` 参数展开，我是通过逐行读文件确认的（41/72/98 行），
   没有 shell 语法检查这一步的机器证据。验收方若有 shellcheck / `bash -n` 权限，值得补跑
   一次。

2. **P2-b 测试把真实 fixtures 的 opus 常量写死在断言里。** 规格要求「拿 fixtures 里的真实
   费率手算一个常量对比」，所以 `test_one_break_is_a_rate_interval_not_a_point_estimate`
   硬编码了 opus 的 `5.0 / 0.50 / 10.0`。一旦 `fixtures/pricing.json` 里 opus 费率变动
   （`test_gates.py` 的 180 天过期闸会强迫这类变动），这个测试会跟着红——这是有意为之的
   耦合，但维护者要知道它。

3. **`_model_stats` 里 one-break 行用 `rates[f"anthropic/{model}"]`，而 read-savings 行用
   `rates[f"{provider}/{model}"]`。** 这是既有代码（本轮只改了 one_break 的计算式，没动这行
   key 的取法）。今天它安全：`cost_by_cause` 在前面已经把非 Anthropic provider 拒绝掉了，
   所以 `provider` 一定是 `anthropic`。但这是潜伏的不一致——若哪天守恒式变得 provider-neutral，
   这行会悄悄按错 key 取费率。本轮按「只改相关处」原则保留，仅在此点名。

4. **两个新 except 的 next-step 文案是我的英文转写。** 规格给的是中文描述，我按既有
   `Next step:` 的全英文风格转写了一遍，语义一致（RepaidExceedsCapacity → 核对这一轮 usage；
   AmbiguousCacheWrite → 核对 `cache_creation` 明细）。验收方可核对措辞是否够准。

5. **零断裂场景** 未被本轮改动触及，`test_diagnose_zero_break_is_non_empty_and_has_money`
   继续绿，且该场景现在打的是区间（`one break ~ $LOW – $HIGH`）而非点估计——测试只断言
   非空含金额，不锁死区间格式，所以没被误伤。
