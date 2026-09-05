# 委托任务 16 — 守卫严格化之后，体检闸再也不跑了

**执行方**：DeepSeek（worktree `.claude-worktrees/lineage`，分支 `fix/lineage-collision`，接着 `9dc629e` 往下做）
**验收方**：Claude
**对应 issue**：#73（同一个 PR，不新开分支）

---

## 这是上一轮亲手开的洞

委托 15 把 `acl_proxy_stop` 严格化之后，验收方实测：

```
$ bash scripts/capture.sh stop
pidfile .capture.pid named pid 99999, but port 8787 is held by pid 26179; not killing
capture.sh stop exit=1
```

`capture.sh` 的 `acl_proxy_stop || exit 1` 在这里直接退出，**后面的
`scripts/capture_health.py "$CAPTURE"` 再也不会跑**。

而代理中途崩掉的那次会话，恰恰是最需要体检报告的时候 ——
`capture_health.py` 自己的开头写着：

> Three captures have already been thrown away … Every time it was found by
> reading the file afterwards, after someone had spent real working time
> producing it. This says so immediately.

**守卫返回非零是对的**（E4 的 arm runner 必须中止）。**错的是调用方没分情况。**

委托 15 的 NOTES 第 6 节第 2 条把这个决定留给了验收方，没有自作主张加
「端口空了就当成功」的分支 —— 那是对的做法。**现在裁定下来了，见下。**

---

## 裁定：两种失败不是一回事，退出码要能区分

`acl_proxy_stop` 现在把两种情况打印得很清楚，但**都返回 1**，调用方无从分辨：

| 情况 | 现在 | 应该 |
|---|---|---|
| 正常停掉，或本来就没在跑且无残留 | 0 | **0** |
| 端口空着，只是 PIDFILE 残留（代理已经没了） | 1 | **2 — 没什么可杀的，是告警不是中止** |
| 端口被一个不是我们记录的进程占着 | 1 | **1 — 不能安全动作，必须中止** |

**边界的理由**：体检闸读的是 capture 文件。

- 「代理已经没了」→ **没有任何进程还在写这个文件**，体检结果可信，
  这正是操作者最需要它的时刻
- 「端口被别人占着」→ **可能还有一个代理正在往文件里写**，
  此时的体检结果是误导 —— 一份还在增长的文件上跑体检，比不跑更糟

### `capture.sh stop` 按这三档分支

- `0` → 现有行为不变
- `2` → **打印守卫的告警，继续跑体检闸**，并以**体检闸的退出码**作为
  `capture.sh stop` 的退出码。体检闸才是「这份数据能不能用」的权威裁决，
  守卫的告警在它上面一行印着就够了
- `1` → 中止，非零退出，**不跑体检闸**

### 退出码契约要写进 `proxy_guard.sh` 的头部注释

E4 的 arm 脚本在仓库外，`source` 这个文件时只能读注释。契约必须写在那里：
哪个码是什么意思、哪个码要求中止整个采集。**不要只写在 commit message 或 NOTES 里。**

---

## 验收标准（AC）

### AC1 — 三档各有一条测试，用真实 socket 和真实进程

- **正常停**（守卫返回 0）→ `capture.sh stop` 跑了体检闸
- **端口空 + PIDFILE 残留**（守卫返回 2）→ `capture.sh stop` **跑了体检闸**，
  且告警出现在输出里。**「体检闸跑了」必须断言**（例如断言体检闸的输出片段出现），
  只断言退出码等于没测到点子上 —— 这与上一轮「进程仍活着」是同一个要害
- **端口被外来进程占着**（守卫返回 1）→ `capture.sh stop` **没跑体检闸**，非零退出。
  同样要断言「没跑」，不是只断言退出码

现有 `tests/test_proxy_guard.py::test_stop_will_not_kill_a_stale_pidfile`
断言的是 `returncode != 0`。**收紧成断言具体的码**，否则 1 和 2 在测试眼里是一回事，
这次的区分明天就能被改回去而测试不响。

### AC2 — 变异守护

- 把 `capture.sh stop` 的三档改回 `acl_proxy_stop || exit 1` → 「返回 2 仍跑体检闸」
  那条必须失败
- 把守卫的「端口被别人占着」也返回 2 → 「不跑体检闸」那条必须失败

两条各贴一次实际失败信息。

### AC3 — 不许弱化已经通过的部分

- 四份真实 capture 判决不变：`capture` / `capture-02` / `capture-03` 不因碰撞闸 FAIL；
  `capture-calib-1` 仍 FAIL 并点名 `d11e8801`、记录 13→14、条数 28→2
- 断裂红线不变：**0 / 1 / 3 / 1**，跑完独立核一遍
- `src/agentcostlab/` 下**一个字不许动**，`scripts/score_injection.py` 不许动
- 基线 **344 passed**，只增不减
- `acl_proxy_start` 一个字不用改 —— 它的失败语义没有分档需求

### AC4 — 开放：失败时该不该删 PIDFILE

`acl_proxy_stop` 在「端口被别人占着」这一档里现在会 `rm -f "$PIDFILE"`。
留着它对排查更有用（记录了我们以为的属主是谁），删掉它更干净。

**两种做法都是合格交付。** 选一个，在 NOTES 里写清理由，并说明你选的那种
在下一次 `stop` 时会走到哪一档。不要因为这条而改动别的行为。

---

## Kill criterion

若「返回 2 时继续跑体检闸」会在某个场景下让体检闸读到一个**正在被写入**的文件，
**停下来报告那个场景**。整条裁定的前提就是「代理已经没了 ⟹ 没人在写」，
这个前提若不成立，裁定本身要重做，不是加个补丁绕过去。

---

## 明确不要做的事

- 不要 `git push` / 开 PR / 用 `gh`
- 不要新开分支，接着 `9dc629e` 往下 commit
- 不要动 `src/agentcostlab/`、`scripts/score_injection.py`
- 不要动 `acl_proxy_start`
- 不要动碰撞闸和 arm 闸（上一轮已验收）
- 不要重写 `capture.sh` 中 `stop` 分支以外的部分
- 不要引入新依赖
- 不要把 `data/raw/` 下任何文件加进 git

---

## 交付要求

**做完后 `git add -A && git commit`**（英文，不允许出现中文字符）。

写 `docs/delegation/16-stop-caller-split-NOTES.md`：

1. 三条测试的**实际输出**（两条「跑了/没跑体检闸」的断言结果要贴出来）
2. 两条变异守护各自的失败信息
3. 四份 capture 的断裂数，你独立算的
4. 测试数变化（基线 344）
5. AC4 你选了哪种，理由，以及下一次 `stop` 会走到哪一档
6. 你认为规格不对、或没能验证的地方

**不要声称「全部通过」。把数字贴出来。**
