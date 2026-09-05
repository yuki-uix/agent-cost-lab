# 委托任务 15 — PR #74 review 第二轮：守卫只在一半的地方验证身份

**执行方**：DeepSeek（worktree `.claude-worktrees/lineage`，分支 `fix/lineage-collision`，接着 `5779a51` 往下做）
**验收方**：Claude
**对应 issue**：#72、#73（同一个 PR，不新开分支）

---

## 三条 review 意见都成立，我已逐条核过

| | 位置 | 结论 |
|---|---|---|
| P1a | `scripts/proxy_guard.sh:53-59` | 成立 |
| P1b | `scripts/capture_health.py:200-205` | 成立，且不是假想场景 |
| P2 | `scripts/proxy_guard.sh:45-50` | 成立，且与 P1a 叠加放大 |

**但不要按三条各修一处。** 它们是两个类别，各修类别，不修实例——
本仓库前几轮 review 反复出现同一类问题，原因正是「修了报告的实例加邻居，
没有推出类别边界」。

---

## 类别一：PIDFILE 是缓存，端口才是真相（覆盖 P1a + P2）

`acl_proxy_start` 已经做对了一半 —— 它起完之后**用端口确认监听者就是自己刚起的
那个 PID**。`acl_proxy_stop` 没有：它直接 `kill` 了 PIDFILE 里的数字。

于是：

- **P1a** — PIDFILE 陈旧（代理早死了、PID 被系统回收给别的进程）时，
  `acl_proxy_stop` 会杀掉一个**毫不相干的进程**。之后端口本来就是空的，
  函数还会返回 0，看起来一切正常。
- **P2** — `acl_proxy_start` 的监听检查超时后 `return 1`，但**那个 nohup 起的子进程
  还活着，PIDFILE 也已经写下去了**。下一次 `acl_proxy_stop` 读到的就是这个
  未经验证的 PID —— 直接喂给 P1a。而那个子进程随后仍可能抢到端口，
  正是 #73 的成因本身。

### 要确立的不变量

> **PIDFILE 里只允许出现「已被端口证实拥有该端口」的 PID。
> 任何作用于代理身份的操作，身份都必须从端口取得，不能从存下来的值取得。**

按这条推，三处都得改，不是两处：

1. `acl_proxy_stop` 在 kill 之前**先确认 PIDFILE 里的 PID 就是当前监听者**。
   不是 → **不许 kill**，如实报告（端口空着是一种情况，被别人占着是另一种，
   两者信息要能分开），返回非零
2. `acl_proxy_start` 失败路径（监听检查超时）**必须清掉自己起的子进程和 PIDFILE**，
   再返回非零。不许留下任何未经验证的痕迹
3. `acl_proxy_start` 起之前的端口占用检查已经有了，保持

**注意 `capture.sh` 的 `$PIDFILE.file`**：它记的是 capture 文件名，与 PID 无关，
但 start 失败时同样不该留下。检查一遍两个文件的生命周期是不是一致。

**`capture.sh` 里那个残留的 `listening()` 函数**现在没有调用方了——
它是「同一类判断的第二份实现」，删掉，别留着。

---

## 类别二：arm 闸只清点了带标签的记录（覆盖 P1b）

```python
declared_arms = collections.Counter(
    r["injection"]["id"] for r in rows
    if isinstance(r.get("injection"), dict) and "id" in r["injection"])
gate(len(declared_arms) <= 1, ...)
```

**没有 `injection` 字段的记录被完全排除在计数之外**，于是「一半记录标着 i3、
另一半根本没标签」的混合 capture，`len(declared_arms) == 1`，**闸门放行**。

### 这不是假想场景

`inject.apply()` 的契约（`src/agentcostlab/inject.py:232-248`）：

> Returns the record annotation, or ``None`` when nothing is armed.

`AGENTCOSTLAB_INJECT` 没设 → 返回 `None` → 记录里 `injection` 为 None；
设了 → 每条记录都有。**所以在同一个代理进程内，这个字段要么条条都有、
要么条条都没有。**

推论，而且是硬的：

> **一份 capture 里 `injection` 字段「有的记录有、有的没有」，
> 只可能是两个代理进程写进了同一个文件。** 那正是 #73。

### 要改成的判据

闸门必须**划分全部记录**，不是只数标签种类：

- 没有任何记录带 `injection` → 闸门不适用，**通过**（三份历史 capture 的情形）
- 所有记录都带 `injection` 且 `id` 只有一种 → **通过**
- 所有记录都带 `injection` 但 `id` 有多种 → **FAIL**（现有行为，保留）
- **部分记录带、部分不带 → FAIL**，点名有多少条带、多少条不带

报告要能让人区分这两种 FAIL：「两个 arm」和「一个 arm 混进了无标签流量」
成因不同，读的人要能一眼分开。

**不要动 `scripts/score_injection.py`。** 那道闸问「有没有声明 arm」，
这道问「声明的是不是同一个 arm、且是不是每条都声明了」，是姊妹不是替代。

---

## 验收标准（AC）

### AC1 — 三个最小复现都要有测试，且都要真的跑

review 方明确说了「现有测试均未覆盖这些情况」。每条都要有一个**会因为旧代码
而失败**的测试：

- **陈旧 PID**：PIDFILE 里写一个不拥有该端口的 PID（例如一个活着的
  `sleep` 进程），调 `acl_proxy_stop` → 非零退出，且**那个进程仍然活着**。
  「进程仍然活着」必须断言，这才是这条的要害
- **启动失败残留**：制造监听检查超时（例如把 `ACL_PYTHON` 指向一个能起来
  但不监听端口的命令）→ 非零退出，且**没有子进程残留、PIDFILE 不存在**
- **部分注入标签**：一份 capture 里 3 条带 `injection.id = "i3"`、2 条不带
  → FAIL，信息里同时出现两个条数

用真实 socket 和真实进程测，不要 mock —— 现有 `tests/test_proxy_guard.py`
的两条已经是这个路子，照着写。

### AC2 — 变异守护

每个类别一条，**改回旧行为，测试必须失败**：

- `acl_proxy_stop` 去掉身份验证、直接 kill → 陈旧 PID 那条测试必须失败
- `acl_proxy_start` 失败路径不清理 → 启动失败残留那条必须失败
- arm 闸改回只数带标签的记录 → 部分标签那条必须失败

三条各贴一次实际失败信息。

### AC3 — 不许弱化已经通过的部分

- 四份真实 capture 的判决不许变：`capture` / `capture-02` / `capture-03`
  仍然不因碰撞闸 FAIL；`capture-calib-1` 仍然 FAIL 并点名 `d11e8801`、
  记录 13→14、条数 28→2
- 断裂红线不许变：**0 / 1 / 3 / 1**。跑完独立核一遍
- `src/agentcostlab/` 下**一个字不许动**，`scripts/score_injection.py` 不许动
- 基线 **341 passed**，只增不减

### AC4 — 顺手核一遍同类出口

按仓库纪律「同一类数据的每条出口都要过同一道闸，双向检查」：

`proxy_guard.sh` / `capture.sh` 里**还有没有第三处**在用存下来的值代替端口
真相、或者在失败路径上留下未清理的状态？有就一起修，没有就在 NOTES 里
写明「查过，没有」并列出你查了哪几处。

**这一条是开放的**：找到 0 处是合格交付，找到 3 处也是。
不要为了「有产出」而把不属于这个类别的东西也改了。

---

## Kill criterion

若「stop 前验证 PID 拥有端口」在某个正常场景下会让 `capture.sh stop` 失败
（例如代理自己正常退出后端口已释放、PIDFILE 还在），**停下来报告这个场景**，
不要靠加一个「端口空了就当成功」的分支蒙混过去 —— 那个分支本身可能是对的，
但它是个设计决定，由验收方裁定。

---

## 明确不要做的事

- 不要 `git push` / 开 PR / 用 `gh`
- 不要新开分支，接着 `5779a51` 往下 commit
- 不要动 `src/agentcostlab/`、`scripts/score_injection.py`
- 不要重写 `capture.sh` 中 start/stop 以外的部分（删掉无人调用的
  `listening()` 除外）
- 不要引入新依赖
- 不要把 `data/raw/` 下任何文件加进 git

---

## 交付要求

**做完后 `git add -A && git commit`**（英文，不允许出现中文字符）。

写 `docs/delegation/15-guard-round2-NOTES.md`：

1. 三条测试的**实际输出**（陈旧 PID 那条要贴出「进程仍活着」的断言结果）
2. 三条变异守护各自的失败信息
3. 四份 capture 的断裂数，你独立算的
4. 测试数变化（基线 341）
5. AC4 你查了哪几处，结论是什么
6. 你认为规格不对、或没能验证的地方

**不要声称「全部通过」。把数字贴出来。**
