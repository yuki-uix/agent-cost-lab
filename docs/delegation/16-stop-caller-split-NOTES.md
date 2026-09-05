# 委托 16 执行笔记 — 守卫分档之后，体检闸在「代理已经没了」时继续跑

执行方记录，供验收方核对。六条交付要求逐条写，贴实际数字与输出，未测如实写「未测」。

---

## 1. 三条测试的实际输出

三条新增测试都在 `tests/test_proxy_guard.py`，跑真实的 `scripts/capture.sh stop`
（不是 source 出守卫函数手抄逻辑），用真实 socket + 真实进程，不 mock。全量：

```
tests/test_proxy_guard.py::test_capture_stop_runs_health_gate_on_a_clean_stop PASSED
tests/test_proxy_guard.py::test_capture_stop_runs_health_gate_when_proxy_is_gone PASSED
tests/test_proxy_guard.py::test_capture_stop_skips_health_gate_when_a_foreign_process_holds_the_port PASSED
```

三条的断言都不靠退出码：判「跑了体检闸」是断言 `"supports:" in r.stdout`，
判「没跑」是断言 `"supports:" not in r.stdout`——`supports:` 只有 `capture_health.py`
会打，守卫和 capture.sh 都不打它。下面用与测试同构的场景直接跑 `capture.sh stop`，
贴出三条的实际输出与断言结果：

### 1a. 正常停（守卫返回 0）→ 跑了体检闸

```
===== 1 clean stop -> health gate runs =====
exit=0  health_gate_ran=True
STDOUT:
stopped.

  PASS  12/12 requests reached upstream
  PASS  usage recorded on 12/12 served requests (pre-stream_complete capture, judged by fraction)
  ...
  supports:
    YES  E1 1.2  divergence rate            11 threaded turns; a clean session is legitimate data
    YES  E1 1.1  miss-cause distribution    needs >=1 conclusive official verdict, has 1
    YES  #10     calibration vs official    needs >=1 conclusive official verdict, has 1

USABLE for E1 1.2, E1 1.1, #10
```

`health_gate_ran=True`，退出码 0。

### 1b. 端口空 + PIDFILE 残留（守卫返回 2）→ 告警在 stderr，体检闸仍跑

```
===== 2 stale pidfile, port free -> warning + health gate =====
exit=0  health_gate_ran=True  bystander_alive=True
STDOUT:
stopped.

  PASS  12/12 requests reached upstream
  ...
USABLE for E1 1.2, E1 1.1, #10
STDERR:
pidfile /tmp/tmp9dhnmjb6/capture.pid named pid 54720, but port 50745 is free; not killing (proxy already gone)
```

这条的要害是两条同时成立：`health_gate_ran=True`（体检闸跑了，不是靠退出码推断），
且 stderr 里守卫的告警 `not killing (proxy already gone)` 就在体检闸输出的上面一行。
测试里 `bystander_alive=True`——被 PIDFILE 误指的进程仍活着，没被 kill。

### 1c. 端口被外来进程占着（守卫返回 1）→ 没跑体检闸，非零退出

```
===== 3 foreign process on port -> abort, no health gate =====
exit=1  health_gate_ran=False  foreign_alive=True  pidfile_kept=True
STDOUT:
(empty)
STDERR:
pidfile /tmp/tmpanf3amw9/capture.pid named pid 54737, but port 50746 is held by pid 54736; not killing
```

`health_gate_ran=False`（stdout 全空，体检闸一行都没打），退出码 1。
`foreign_alive=True`（占端口的外来进程没被动）、`pidfile_kept=True`（见第 5 条 AC4）。

---

## 2. 两条变异守护各自的失败信息

### 变异一：`capture.sh stop` 改回 `acl_proxy_stop || exit 1`

```
FAILED tests/test_proxy_guard.py::test_capture_stop_runs_health_gate_when_proxy_is_gone
E   AssertionError: health gate must run after a stale-pidfile stop:
E     pidfile .../capture.pid named pid 55552, but port 50837 is free; not killing (proxy already gone)
E   assert 'supports:' in ''
E    +  where '' = CompletedProcess([...]).stdout
1 failed in 0.10s
```

守卫返回 2，`|| exit 1` 把非零当中止，体检闸没跑，`supports:` 不出现——断言崩在最该崩的地方。

### 变异二：守卫「端口被别人占着」也返回 2

```
FAILED tests/test_proxy_guard.py::test_capture_stop_skips_health_gate_when_a_foreign_process_holds_the_port
E   assert 'supports:' not in 'stopped.\n...USABLE for E1 1.2, E1 1.1, #10\n'
E   'supports:' is contained here:
E     ...   supports:
E           YES  E1 1.2  divergence rate  ...
1 failed in 0.34s
```

「端口被别人占着」变成 2 后，capture.sh stop 把它当告警继续跑体检闸，
`supports:` 出现在了本不该出现的地方，`not in` 断言崩掉。

两条变异都真跑过，各贴一次失败信息如上。

---

## 3. 四份 capture 的断裂数（独立复算）

不经过 `capture_health` 主流程，逐条找 `injected_previous_message_id` 指到的前驱、
调 `ledger.broke_cache(prev, curr) is True` 计数：

```
capture        : 75  records, breaks=0 -> []
capture-02     : 66  records, breaks=1 -> ['msg_011C']
capture-03     : 162 records, breaks=3 -> ['msg_011C', 'msg_011C', 'msg_011C']
capture-calib-1: 28  records, breaks=1 -> ['bae15c3e']
```

红线 **0 / 1 / 3 / 1**，对上。

四份 capture 的判决用改动后的 `scripts/capture_health.py` 各跑一遍复核：

- `capture` / `capture-02` / `capture-03`：`PASS  0 campaign arms in one capture: {}`，
  无 FAIL，`USABLE`。碰撞闸没有误伤这三份。
- `capture-calib-1`：`PASS  1 campaign arms in one capture: {'i0': 28}`；仍
  `FAIL  lineage d11e8801 splices two conversations: record 13->14 messages 28->2
  <- collision polluted the break count`（外加既有的 beta-header 见证 FAIL），
  `NOT USABLE`。点名 `d11e8801`、13→14、28→2 不变。

---

## 4. 测试数变化

基线 **344 passed**。本次 +3 条新测试（都是 `test_proxy_guard.py` 里的
`test_capture_stop_*`），并把既有的 `test_stop_will_not_kill_a_stale_pidfile`
断言从 `!= 0` 收紧成 `== 2`、`test_stop_times_out_when_the_process_ignores_sigterm`
从 `!= 0` 收紧成 `== 1`——收紧是改断言，不是增删测试。改动后全量：

```
347 passed in 125.49s (0:02:05)
```

344 → 347，只增不减。

---

## 5. AC4 — 失败时该不该删 PIDFILE：选了「留」

选了**留着**（`acl_proxy_stop` 在「端口被别人占着」这一档**不再** `rm -f "$PIDFILE"`；
「端口空 + PIDFILE 残留」那一档仍然删，因为那才是残留、要清掉）。

理由：

1. 这一档的 PIDFILE 不是残留垃圾，是**证据**——它记着我们以为的属主是谁。
   端口被一个不是我们记录的进程占着，正是 #73 要排查的场景；把「我们以为的属主」
   删掉，等于把解释这场碰撞的唯一状态扔掉。「删掉更干净」在这里是错觉：问题不在
   PIDFILE，在占端口的外来进程，删 PIDFILE 只是藏起了自己的记录。
2. 留着不影响安全：这一档本来就 `return 1` 中止、不 `kill` 任何东西，留着 PIDFILE
   不会让下一次 stop 多做一个动作。

下一次 `stop` 会走到哪一档：**还是「端口被别人占着」这一档，返回 1**。
因为 PIDFILE 还在（内容仍是记录的 pid Y），端口还被外来进程 X 占着，下一次 stop
进 `listener != pid` 且 `_port_free` 为假 → `return 1`，消息仍是
`pidfile ... named pid Y, but port ... held by pid X; not killing`。
区别只是：若删掉 PIDFILE，下一次会落进「no pidfile, but port held」那一档
（也返回 1），但消息变成 `refusing to kill an unrecorded process`，丢了「我们以为
Y 拥有它」这半句——留着正好保住它。

测试里用 `assert pidfile.exists()` 把「留」钉死成断言，不是 checklist。

---

## 6. 规格不对、或没能验证的地方

1. **为测临时端口，动了 capture.sh 中 stop 分支以外的三行。** 规格说「不要重写
   capture.sh 中 stop 分支以外的部分」，但 `capture.sh` 顶部硬编码
   `PORT=8787` / `PIDFILE=.capture.pid` / `LOG=data/raw/proxy.log`，而本机 8787 被
   昨天残留的真实代理占着，测试必须用临时端口，环境变量进不去（脚本里的赋值会覆盖
   env）。最小改法是把这三行改成 `${PORT:-8787}` / `${PIDFILE:-.capture.pid}` /
   `${LOG:-data/raw/proxy.log}`：默认值与原行为逐字节一致，只多了「可用 env 覆盖」。
   这跟脚本里已经存在的 `${ACL_PYTHON:-.venv/bin/python}` 是同一套约定，
   `proxy_guard.sh` 自己也是这么写的。特此说明。

2. **Kill criterion 分析：前提成立，没有「返回 2 时体检闸读到正在写入的文件」的场景。**
   返回 2 的唯一条件是 `_port_free` 为真（端口无 LISTEN）+ PIDFILE 残留。
   而 capture 文件的唯一写入方是代理进程，代理写文件只发生在它监听端口、
   转发请求的过程中；端口没有 LISTEN ⟹ 代理不处于「接受请求并写文件」的状态 ⟹
   没人在写。最接近的边界是「代理刚崩、LISTEN 已关但最后一笔写了一半」——那是
   一条 torn 记录，会让 `capture_health.py` 的 `load()` 在 `json.loads` 上抛
   `JSONDecodeError`（响亮失败，不是误导性 PASS），且这是既有行为，不是本次分档
   引入的。整条裁定「代理已经没了 ⟹ 没人在写」成立，无需重做。

3. **未测：端起真实 `agentcostlab.proxy` 的端到端。** 与上一轮 NOTES 相同：三条测试
   用真实 socket（`_listener_holder` 真绑端口）+ 真实进程（sleeper / listener），但
   没有起真实代理进程（那需要真实上游）。守卫返回 0 的「正常停」一档，测试里用一个
   真监听进程模拟代理，被 kill 后端口释放、返回 0，路径与真实代理一致；但
   `acl_proxy_stop` 对「uvicorn 单进程不 fork」的依赖未在本次端到端复验。

4. **「collision 闸 / arm 闸」与 `acl_proxy_start` 未动，`src/agentcostlab/` 与
   `scripts/score_injection.py` 一个字节未动。** 本次 `git status` 只有三个文件：
   `scripts/capture.sh`、`scripts/proxy_guard.sh`、`tests/test_proxy_guard.py`。
