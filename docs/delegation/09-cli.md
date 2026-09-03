# 委托任务 09 — 逐条费率解析 + 两条命令

**执行方**：DeepSeek（独立 worktree `.claude-worktrees/cli`，分支 `feat/cli`）
**验收方**：Claude
**对应 issue**：#59（第一步）与 #52（第二步）

**分两个 commit 交付，顺序不许颠倒。**

---

## 背景

`direction.md` 定位这个仓库是**仪器**，不是报告。但今天它装不上：
`pyproject.toml` 里没有 `[project.scripts]`，用法是手打
`.venv/bin/python scripts/xxx.py --path ...`，四个脚本各说各话，
没有任何地方告诉用户「采完之后先跑哪个」。

`cost_by_cause()`（#50/#51 已合并）能说「因为什么原因、亏了多少钱，以及我有多确定」。
把它包成一条命令，这个仓库才第一次是个能给别人用的东西。

---

# 第一步（commit 1）— #59：逐条解析费率

## 问题

`cost_by_cause(records, model_key)` 用**一个**费率给整份 capture 定价，混模型就整份拒绝。

实测三份真实 capture：

```
capture-02:  66 条  model={'claude-sonnet-5': 66}                      -> 可出数
capture-03: 162 条  model={'claude-sonnet-5': 77, 'claude-opus-5': 85}  -> 整份被拒
capture   :  75 条  model={'claude-sonnet-5': 75}                      -> 可出数
```

`capture-03` 是 E3 用的那份，本仓库最大、含唯一一次 `/compact`。**工具对它一个字说不出来。**

`fixtures/pricing.json` 里 sonnet 与 opus 的费率**都在、都 verified**。
缺的不是费率，是逐条解析的能力。

## 要求

每条记录用它自己的 `provider` + `request_body["model"]` 解析费率。

**闸不许弱化，只是判据从「与传入 key 一致」改为「能解析且已核实」**：

| 情形 | 行为 |
|---|---|
| provider/model 在 `pricing.json` 里查不到 | 抛出，点名第几条记录与它的 provider/model |
| 查到但 `verified: false` | 仍抛 `UnverifiedRate` |
| provider 非 `anthropic` | 仍然拒绝（守恒式限制，见 #57） |
| 记录没有 `model` 字段 | **不许猜**。抛出，或要求调用方显式给兜底 key——二选一，在 NOTES 里说明理由 |

混模型 capture 的美元是**各轮按各自费率算出后相加**，不是先合并 token 再乘平均费率。

## 第一步的 AC

- **防回归**：`capture-02` 的 `repaid_tokens=2822` / `usd_low=0.0050796` /
  `usd_high=0.0055986` 逐位不变
- **新锚点**：`capture-03` 能出数。报告它的 `by_cause` / `ambiguous` / `unattributed`
  分布与总金额，并**手算至少一轮 opus 的损失**贴进 NOTES（费率来源、`retrieved_at`、算式）
- 新增测试：混模型 capture 出数正确；未知 model 抛出并点名记录；未核实费率仍抛
- #50/#51 建立的所有闸不许弱化（单位闸、桶容量、`AmbiguousCacheWrite`、三桶穷尽性、
  枚举验证的 `order_stability`）

---

# 第二步（commit 2）— #52：`record` 与 `diagnose`

## 目标形态

```bash
pip install -e .
agentcostlab record      # 起代理，把 agent 指过来照常干活
agentcostlab diagnose    # 打印诊断
```

## 关键设计约束

以下六条是**已定的判断，不要自行发挥**。有异议写进 NOTES。

### 一、以钱为主，成因为辅

**这条决定整个布局。** 在唯一的真实数据上，`by_cause` 是**空的**：

```
capture-02:  by_cause 空 / ambiguous 1 轮 $0.0051–$0.0056 / hit_usd_saved $7.6750
```

围绕「这是你的成因分解」设计的界面，真实用户打开看到一张空表，工具像坏了。

所以头条永远是**钱**：缓存省了多少、未命中多花了多少。成因分解是**其下的一节**，
可以为空、可以全是 ambiguous。

### 二、零断裂时不许空手

健康的 agent 不打断自己的前缀。一个人正常干活一小时跑 `diagnose`，很可能零 miss。
**这时必须仍然有内容**：命中 token 数、按费率折算省下的钱、
以及「若前缀断一次，按你自己这份 capture 的典型前缀长度，要多付多少」。

先给钱的量级，再谈有没有病。

### 三、百分比永远与它的分母同行

`capture-02` 只有 **1 次**断裂。印一句「100% 的损失来自 ambiguous」是灾难——
本仓库已经有过一次「裸的 100% 被脱离上下文引用」。

**规则**：任何百分比，同一行里必须出现它的计数。
`"100% (1 of 1 break)"` 可以，`"100%"` 不行。**写一条测试守住它**：
扫描 `diagnose` 的输出，任何含 `%` 的行必须同时含一个整数计数。

这与 #44 是同一条纪律：分布必须打印自己的条件。

### 四、三档结论视觉上必须可区分，且 ambiguous 绝不冒充成因

`by_cause[X]` / `ambiguous` / `unattributed` 三者在输出里必须一眼分得开。
`ambiguous` 那行必须**列出候选并标明是候选**，
措辞不许让读者以为它是一个已确定的成因。

### 五、`record` 复用 `scripts/capture.sh`，不重写

已有的 start/status/stop 语义、「绝不覆盖已有 capture」的保护、
以及 nohup 脱离终端的处理都要保留。`record` 是它的入口，不是它的替代品。

### 六、退出码

- `diagnose` 成功（**包括零断裂**）→ `0`
- 无法出数（费率未核实、model 解析不了、非 anthropic provider 等）→ 非 `0`，
  并且**打印拒绝的理由和下一步怎么办**，不是一句报错了事

## 第二步的 AC

- `[project.scripts]` 提供 `agentcostlab`；`pip install -e .` 后两条命令可用
- `diagnose` **不需要用户传 model key**（第一步已让它可推断）
- 零断裂的 capture → 输出非空且含金额（测试：喂一份零断裂数据，断言输出含 `$`）
- 百分比闸有测试（约束三）
- 三档在输出里可区分（测试：ambiguous 那行含候选名与「候选」字样，
  且不出现在 `by_cause` 的区块里）
- `capture-02` 与 `capture-03` 都能跑通 `diagnose`，把**两份的真实输出贴进 NOTES**
- 旧脚本保留可用，本次不删
- 归因功能在 #54（故障注入验收）跑完之前，输出里要标注它**未在真实断裂上验证过**

---

## 明确不要做的事

- 不要 `git push`，不要开 PR，不要用 `gh`（验收方来做）
- 不要改 `ledger.py` / `attribute.py` / `providers.py` / `proxy.py` / `redact.py`
- `pricing.py` 只许做第一步逐条解析所需的最小改动；若能不改就不改
- 不要碰 `predictions.md` / `fixtures/pricing.json`（费率是核实过的，不许动）
- 不要引入新依赖（**CLI 用标准库 `argparse`，不要 click/typer**）
- 不要为了让输出好看而编造数字
- commit message 全英文，格式 `type: description`

## 交付要求

**做完后 `git add -A && git commit`**，分两个 commit（第一步、第二步各一个）。
前两轮有一轮跑完忘了提交，改动散在工作区——这次请显式提交。

写 `docs/delegation/09-cli-NOTES.md`：

1. 第一步：`capture-03` 的分布与总金额，**至少一轮 opus 损失的手算过程**
2. 第一步：`capture-02` 三个红线数字的实测值
3. 第二步：`capture-02` 与 `capture-03` 的 `diagnose` **真实输出全文**
4. 测试数变化（`N passed / M skipped`），skip 的每条点名缺什么
5. 你认为规格里不对、或没能验证的地方，如实写出来

**不要声称「全部通过」。把数字贴出来。**
