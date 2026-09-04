# 委托任务 10 — CLI 评审四条修正（PR #60）

**执行方**：DeepSeek（复用 worktree `.claude-worktrees/cli`，分支 `feat/cli`）
**验收方**：Claude
**对应 PR**：#60（changes requested）

代码评审在 PR #60 上提了四条，验收方逐条独立复算，**四条全部成立**。
四条都在 `src/agentcostlab/cli.py` + `scripts/capture.sh` + `tests/test_cli.py` 之内。
**评审明确指出：这四条路径目前一条测试都没覆盖**，所以每条修完都要补一条会咬人的测试。

**做完后 `git add -A && git commit`**（一个 commit 即可，标题带 `fix:`）。

---

## P1-a · 默认 capture 会选中失败样本

### 问题（已复现）

`_default_capture()` 用 `RAW_DIR.glob("capture-*.jsonl")` 再取字典序最后一个。
主仓库 `data/raw/` 里还有：

```
capture-02.jsonl
capture-03.jsonl
capture-attempt1-failed.jsonl
capture-attempt2-nogzipfix.jsonl
capture-attempt3-prelineagefix.jsonl   ← 字典序最后，裸 diagnose 实测选中了它
```

`capture-attempt3-prelineagefix.jsonl` 是显式命名为「失败 / 预修复」的废样本。
**裸 `agentcostlab diagnose` 默认诊断了一份坏数据。**

### 修法

只匹配 `capture-NN.jsonl`（N 为数字），**按数字大小**选最大的，不按字典序：

- 用正则 `^capture-(\d+)\.jsonl$` 筛选，解析出整数，取整数最大者
- `capture-attempt*` / 任何非纯数字后缀一律不参与
- 一个 numbered 的都没有时，退回 `capture.jsonl`（现有兜底不变）

### 测试

在一个 tmp 目录放 `capture-02.jsonl` / `capture-10.jsonl` /
`capture-attempt3-prelineagefix.jsonl`，断言 `_default_capture()` 选中 `capture-10.jsonl`
（既验数字序 10 > 2，又验 attempt 文件被排除）。

---

## P1-b · `record` 依赖固定名称的 `.venv`

### 问题（已复现）

`scripts/capture.sh` 三处硬编码 `.venv/bin/python`（41 / 72 / 98 行），且脚本开头
`cd "$(dirname "$0")/.."`，所以那是「仓库根/.venv/bin/python」。CLI 可以被
`pip install` 进任意环境，但 `agentcostlab record` 转发到 capture.sh 后仍去找
仓库根下的 `.venv`——装在别处就失败，日志留 `nohup: .venv/bin/python: No such file or directory`。

### 修法（照抄 capture.sh 已有的 `ACL_CAPTURE` 环境变量惯例）

- capture.sh 三处改成 `"${ACL_PYTHON:-.venv/bin/python}"`——
  **默认值保留 `.venv/bin/python`，所以直接 `scripts/capture.sh` 的现有语义一字不变**
- `cli.py::_cmd_record` 转发时传 `ACL_PYTHON=sys.executable`：
  `subprocess.run([...], env={**os.environ, "ACL_PYTHON": sys.executable})`

**这是修改 capture.sh，不是重写**——start/status/stop 语义、nohup、绝不覆盖 capture 全部保留。

### 测试

monkeypatch `subprocess.run`，调 `_cmd_record("status")`，断言传入的 `env["ACL_PYTHON"]`
等于 `sys.executable`。

---

## P2-a · 未处理全部定价拒绝路径（违反退出码契约）

### 问题（已复现）

`run_diagnose()` 捕获了 `NonAnthropicProvider` / `RecordRateMismatch` / `MissingModel`
/ `UnverifiedRate`，但**漏了 `RepaidExceedsCapacity`（analysis）和 `AmbiguousCacheWrite`
（pricing）**。实测：容量不足的 capture 让 `RepaidExceedsCapacity` 直接从 `run_diagnose`
逃逸成 traceback，而不是「非零退出码 + 拒绝原因 + 下一步」。

约束 6 要求：无法出数 → 非 0 且打印理由与下一步。这两条破坏了它。

### 修法

在 `run_diagnose` 的 try 块补两个 except，与既有四个同样的形状（exit 2 + 原因 + next step）：

- `RepaidExceedsCapacity` → next step 说明：这一轮重付的 token 超过了它自己 usage 里
  非 cache_read 桶的总量，守恒式前提不成立，多半是仪器异常，建议核对该 capture 这一轮的 usage
- `AmbiguousCacheWrite` → next step 说明：某轮写入 token 没有 TTL 而两档价不同，
  无法定价；建议核对该 capture 的 `cache_creation` 明细

**不要用 `except ValueError` 一网打尽**——那会顺手吞掉不该吞的东西。逐个异常类型列。

### 测试

两条：构造容量不足的 capture → `run_diagnose` 返回 `(2, ...)` 且文本含 "Next step"；
构造无 TTL 写入的 capture → 同样返回 2 而非抛出。

---

## P2-b · 单次断裂成本仍是隐藏自由参数的点估计

### 问题（验收方上轮已标，评审独立复现）

`_model_stats` 里 `one_break = typical * (input_uncached - cache_read)`，是**点估计**，
隐含假定重付 token 落进 1× 的 uncached 桶。opus 实测：显示 `$0.423027`，
若落进 1h 写入桶则 `$0.893057`，差 **2.11 倍**，而 `~` 没说这是下界。

同一屏里真实损失是区间、这个数是点估计，且未声明假设。

### 修法（这条按这个来，不要自行选别的）

**这是一次假想的断裂，没有真实 usage 桶可做容量约束**，所以不能套用 `_loss_interval`
的贪心装填。改成纯**费率区间**：

```
one_break_low  = typical * (input_uncached  - cache_read) / 1e6   # 最便宜的桶
one_break_high = typical * (cache_write_1h  - cache_read) / 1e6   # 最贵的桶
```

输出改成区间，与损失行一致：
`one break ~ $0.423027 – $0.893057 (median prefix 94,006 tokens)`

**不要**编造一个容量约束、**不要**保留单点再加脚注。区间本身就是诚实的表达。

### 测试

断言某模型的 `one_break_high > one_break_low`，且 low 用 uncached、high 用 1h 费率
（拿 fixtures 里的真实费率手算一个常量对比）。

---

## 防回归红线（一条都不许动）

- `capture-02`：`repaid=2822` / `usd_low=0.0050796` / `usd_high=0.0055986` 逐位不变
- `capture-03`：命中省下 `$37.802785`、断裂 3 轮 `$0.041033 – $0.044885` 不变
- 逐条费率解析（#59）、三桶穷尽性、`order_stability` 枚举验证、桶容量约束、
  单位闸、未核实费率闸、`AmbiguousCacheWrite` 传播——全部不许弱化
- 基线测试数只增不减
- 零断裂场景仍非空且含金额

## 明确不要做的事

- 不要 `git push` / 开 PR / 用 `gh`（验收方来做）
- 不要改 `ledger.py` / `attribute.py` / `providers.py` / `proxy.py` / `redact.py` / `analysis.py`
  （本轮修正全在 `cli.py` + `capture.sh` + `tests/test_cli.py`；`pricing.py` 也不用动）
- 不要碰 `predictions.md` / `fixtures/pricing.json`
- 不要引入新依赖
- commit message 全英文，格式 `fix: description`

## 交付要求

写 `docs/delegation/10-cli-fixes-NOTES.md`：

1. 四条各自的修法与新增测试，测试数变化（`N passed`）
2. 四条修完后 `capture-02` 与 `capture-03` 的 `diagnose` 实际输出（证明红线未动）
3. P1-a：贴出裸 `diagnose` 现在选中的文件名（应是 `capture-03.jsonl`）
4. 你认为规格里不对、或没能验证的地方，如实写出来

**不要声称「全部通过」。把数字贴出来。**
