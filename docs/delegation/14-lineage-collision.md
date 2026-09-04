# 委托任务 14 — 两条会话被并成一条 lineage，闸门看不见

**执行方**：DeepSeek（独立 worktree `.claude-worktrees/lineage`，分支 `fix/lineage-collision`）
**验收方**：Claude
**对应 issue**：#72、#73

---

## 背景

E4 采集要按 arm 逐个跑，每个 arm 一份 capture。#69 的轮次标定跑了两次相同的
任务，暴露出两个互相叠加的缺陷：

1. **#73** — run 1 的代理没退干净，run 2 的代理 `[Errno 48] address already in
   use` 起不来，**流量静默打进 run 1 的 capture**。`capture-calib-2.jsonl` 根本
   没被创建；agent 侧 `is_error: 0`、`completed`，没有任何东西报错。
2. **#72** — 两次会话提示词相同 → `messages[0]` 相同 → `_lineage_key` 判成同一条
   lineage。run 2 的第一条记录 `cache_read = 0`，被串到 run 1 最后一轮
   （`cache_read = 33408`）之后，按短缺判据 `0 < 33408 + 0` → **一次凭空的假断裂**。

`data/raw/capture-calib-1.jsonl`（28 条，已放进本 worktree）就是那份被污染的
capture。这次委托要把两道闸都补上。

---

## 规格作者已经做过的测量（照抄结论前先读完，有几条与 issue 写的不一样）

写规格前我在真实数据上试了三种判据。**前两种都失败了**，这是本任务最容易走错的地方。

### 判据甲：按 threading 指针切链 —— 无效

issue 里建议的「同一 lineage 含多条互不相连的链」，若用
`response_id → injected_previous_message_id` 的连边判断，**两条链是连着的**：
代理的 `LAST_ID` 残留让 run 2 的第一条指向 run 1 的最后一条。图上只有一个连通分量。

### 判据乙：按「后继是否为前驱的追加」切链 —— 误报到不能用

用 `attribute._is_append`（规范化后判断消息列表是否为追加）切链，实测：

| capture | 记录 | lineage 组 | 被切出多链的组 | 「有害接缝」 |
|---|---|---|---|---|
| `capture.jsonl` | 75 | 12 | 2 | 0 |
| `capture-02.jsonl` | 66 | 17 | 2 | 1 |
| `capture-03.jsonl` | 162 | 28 | 5 | 3 |
| `capture-calib-1.jsonl` | 28 | 2 | 1 | 1 |

「有害接缝」= 该接缝上 `ledger.broke_cache(prev, curr) is True`。
**这四个数字正好等于本仓库已知的断裂红线（0 / 1 / 3 / 1）** —— 也就是说，
**每一次真实的缓存断裂本来就是一个「非追加」的接缝**。判据乙分不出「真断裂」和
「假拼接」，把它接进闸门等于把所有真数据判成不可用。

### 判据丙：消息条数在同一 lineage 内严格变短 —— 有效

一次真实的对话只会追加，消息列表**永远不会变短**。变短意味着两段对话被粘在一起。

实测（`len(request_body["messages"])`，同一 lineage 组内按文件顺序相邻比较）：

```
capture.jsonl        严格变短 0 次
capture-02.jsonl     严格变短 0 次
capture-03.jsonl     严格变短 0 次   <- 这份含一次真实 /compact
capture-calib-1.jsonl 严格变短 1 次  ('d11e8801', 记录 13 -> 14, 28 条 -> 2 条, 有害=True)
```

**0 误报 / 303 条历史记录 / 57 个 lineage 组；1 真阳性 / 1 已知污染。**

`/compact` 不会误报的原因是结构性的：compaction 重写了 `messages[0]`，于是它
本来就落到另一个 lineage 组里，组内不存在变短。

**判据丙是本任务要实现的那个。** 甲、乙写在这里是为了让你不要回头去试它们。

### 另外两条需要记进 NOTES 的事实

- `report/99-open-questions.md` 写着「实测三份 capture、57 个 lineage 组零碰撞」，
  **这句话是错的**。三份 capture 里都有一个 `d725e692` 组，19 / 16 / 18 条记录，
  每条都是 2 条消息、内容各不相同 —— 是 Claude Code 的 `<transcript>` 旁路调用，
  每次都是独立的一次性会话，共用同一个 `messages[0]`。**碰撞一直存在**，
  只是这些调用不带缓存，所以从未产生假断裂。
- 因此闸门的严重度必须**按危害分级**，不能按形状。见 AC1。

---

## 交付物

### 一、`scripts/capture_health.py` — 新增闸门 A（#72）

新增一个检查：**同一 lineage 组内，消息条数严格变短**。

- 判据**不许**调用 `proxy._lineage_key` 来判断两条记录是不是真的同源 ——
  `_lineage_key` 对它自己造成的碰撞是盲的，这是整个问题的根源。
  （分组仍然用 `_lineage_key`，那是被检查的对象；**判据本身**必须独立于它。）
- 判据**不许**依赖 `injected_previous_message_id` —— 判据甲失败的原因。
- 报告要点名：哪条 lineage（前 8 位）、在第几条记录、条数从多少掉到多少。

**严重度分级（这是 AC1，不是可选项）**：

- 接缝上 `ledger.broke_cache(prev, curr) is True` → **FAIL**。这条接缝已经污染了
  断裂计数，数据不可用。
- 接缝存在但不构成断裂 → **报告出来，不 FAIL**。`d725e692` 那类无害碰撞若判 FAIL，
  三份历史 capture 全部变成「不可用」—— 一次假 FAIL 会让本文件的用途反过来。
  用与既有 `undecidable` 同类的第三档输出（`----` 行）。

`broke_cache` 只能通过 `ledger.broke_cache` 调用，不许在本文件里复刻判据 ——
`_broke_cache` 的 docstring 记着上次复刻的代价。

### 二、`scripts/capture_health.py` — 新增闸门 B（#73）

**一份 capture 里出现多于一种 `injection.id` → FAIL**，点名有哪几种、各多少条。

- 没有 `injection` 字段的记录（历史 capture 全是）**不参与**这条判断，
  也不因此 FAIL —— 那是 `score_injection.py` 已有的另一道闸的职责。
- 与 `score_injection.py` 那道闸是姊妹关系：那道问「有没有声明 arm」，
  这道问「声明的是不是同一个 arm」。**不要把那道闸搬过来或改动它。**

### 三、`fixtures/lineage/collision-calib.json` — 回归 fixture

从 `data/raw/capture-calib-1.jsonl`（28 条真实记录，已在 worktree 里）**缩减**出来。

- `.gitignore` 里 `*.jsonl` 是全局忽略的，所以**必须是 `.json`**，
  内容是一个 JSON 数组，参照 `fixtures/attribution/*.json` 的风格
- 缩减规则由你写成一个可重跑的脚本（放 `scripts/` 或 `tests/`），并在 NOTES 里说明。
  要求：**保留判据用得到的一切**（`len(messages)`、每条消息在规范化后的相等/不等
  关系、`usage`、`response_id`、`injected_previous_message_id`、`status_code`、
  `injection`），**丢掉一切自由文本**
- 原始 capture 含真实源码路径与提示词，**fixture 里不许残留任何原文**
- **缩减后的 fixture 必须与原始 28 条得出同一个判决**（同一条 lineage、同一个接缝
  位置、同样的有害标记）。把这条写成一个测试或在 NOTES 里贴出两边的输出对比

### 四、`scripts/proxy_guard.sh` — 端口守卫（#73）

供采集脚本 `source` 的三个函数（名字可以调整，功能不可少）：

- `acl_proxy_start` — 起代理**之前**确认端口空闲；起完之后确认**监听这个端口的进程
  就是自己刚起的那个 PID**。任一条不成立 → 非零退出并打印原因
- `acl_proxy_stop` — kill 之后**轮询到端口真正释放**再返回，超时则非零退出。
  不许 `kill` 完 `sleep 1` 就往下走（那正是 #73 的成因）
- 任一检查失败，**调用方必须中止整个采集**，不是跳过这一个 arm。
  在函数的注释里写清这一点，并让返回码支持 `set -e`

把 `scripts/capture.sh` 的 start/stop 改成走这两个函数 —— 现在它自己有一份
`listening()` 轮询，正是「同一类判断的两份实现」。**只改这两处，不要重写
`capture.sh` 的其余部分。**

### 五、`docs/e4-injection-campaign.md` — 采集侧规避

在采集流程里写明：同一个 arm 跑多次时，**每次的任务提示词要带一个运行编号或
随机 nonce**，让 `messages[0]` 天然不同。

一句话说清分工：**nonce 是预防（不产生污染），闸门是兜底（事后告诉你数据废了）。
两个都要。** 不要把闸门写成「加了 nonce 就不需要」。

---

## 验收标准（AC）

### AC1 — 分级正确，历史数据不被误杀

四份真实 capture 各跑一次 `scripts/capture_health.py`，贴**输出全文**：

| capture | 闸门 A 期望 |
|---|---|
| `data/raw/capture.jsonl` | 不 FAIL；`d725e692` 那类无害碰撞报告出来 |
| `data/raw/capture-02.jsonl` | 不 FAIL |
| `data/raw/capture-03.jsonl` | 不 FAIL |
| `data/raw/capture-calib-1.jsonl` | **FAIL**，点名 `d11e8801`、记录 13→14、28→2 |

**红线不许变**：`capture-02` 断裂 1、`capture-03` 断裂 3、`capture` 断裂 0、
`capture-calib-1` 断裂 1。跑完之后独立核一遍这四个数。

> 这是一条**开放 AC**：如果你实测出的「无害碰撞」条数与规格里的 19 / 16 / 18
> 不一致，**照实报，不要改判据去凑**。规格作者的数是用一个一次性脚本量的，
> 与你的实现不必逐字相同。**判据丙的 0 误报 / 1 真阳性是硬的，碰撞计数不是。**

### AC2 — 闸门 A 独立于 `_lineage_key` 和 threading 指针

- 构造一对记录：`messages[0]` 相同、后一条消息条数变短、**不带**任何
  `injected_previous_message_id` → 仍然被检出
- **变异守护**：把判据改成恒假（永不报告碰撞），`capture-calib-1` 那条测试必须失败

### AC3 — 反向防线：真实断裂不被误报成碰撞

- 构造一对「消息条数增长、但前缀内容被改过」的记录（真实的 `messages_changed`
  断裂）→ 闸门 A **不**报告
- 断言三份历史 capture 上闸门 A 的 FAIL 数为 0
- **变异守护**：把判据从「严格变短」放宽成「非追加」（即判据乙），
  三份历史 capture 的测试必须失败

### AC4 — 闸门 B

- 一份 capture 含两种 `injection.id` → FAIL，两种都被点名
- 一份 capture 只有一种 → 不 FAIL
- 一份 capture 完全没有 `injection` 字段 → 不 FAIL（历史 capture 的情形）
- **变异守护**：把 FAIL 改成静默跳过，测试必须失败

### AC5 — 端口守卫真的会拦

用真实 socket 测，不要 mock：

- 先在 8787 上绑一个 socket，再调 `acl_proxy_start` → **非零退出**，
  信息里出现端口被占用
- `acl_proxy_stop` 之后轮询到端口释放才返回；构造一个 kill 不掉的情形 → 超时非零退出
- 把两条测试的**实际退出码和输出**贴进 NOTES

### AC6 — 不许弱化既有闸

`src/agentcostlab/` 下**一个字不许动**，`scripts/score_injection.py` 不许动。
基线：**311 passed, 16 skipped**，只增不减。

---

## Kill criterion

若判据丙在你的实现里在某份历史 capture 上产生了 FAIL，**停下来如实报告是哪一条、
为什么**，不要加阈值、不要加白名单、不要给某个 lineage 开特例。
规格作者量到的是 0 误报；若你量到不是 0，那是规格错了，由验收方裁定。

同样，若 AC1 的红线（0 / 1 / 3 / 1）在你这里对不上，**先报告再动手**。

---

## 明确不要做的事

- 不要 `git push` / 开 PR / 用 `gh`（验收方来做）
- 不要动 `src/agentcostlab/` 下的任何模块
- 不要动 `scripts/score_injection.py`
- 不要重写 `scripts/capture.sh` 中 start/stop 以外的部分
- 不要碰 `predictions.md` / `fixtures/pricing.json` / `fixtures/attribution/`
- 不要引入新依赖
- 不要为了让某份 capture 通过而加阈值、白名单或特判
- 不要把 `data/raw/` 下的任何文件加进 git（`capture-02` / `capture-03` /
  `capture.jsonl` 是符号链接，指向仓库外的真实数据）

---

## 交付要求

**做完后 `git add -A && git commit`**（英文 commit message，`fix: 描述`；
不允许出现中文字符）。

写 `docs/delegation/14-lineage-collision-NOTES.md`：

1. 四份 capture 的 `capture_health.py` **输出全文**
2. 你独立算出的四个断裂数，以及闸门 A 报告的碰撞条数
3. fixture 缩减脚本的规则，以及「缩减前后判决一致」的证据
4. AC5 两条测试的实际退出码和输出
5. 测试数变化（基线 311 passed / 16 skipped）
6. 三条变异守护各自的失败信息
7. 你认为规格不对、或没能验证的地方 —— 特别是判据丙的误报数若不是 0

**不要声称「全部通过」。把数字贴出来。**
