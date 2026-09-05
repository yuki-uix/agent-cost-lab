# 委托 15 验收笔记 — 守卫只在一半的地方验证身份（PR #74 round 2）

执行方记录，供验收方核对。六条交付要求逐条写，贴实际输出，未测如实写「未测」。

---

## 1. 三条新测试的实际输出

三条都已加进 `tests/test_proxy_guard.py` / `tests/test_capture_health.py`，
用真实 socket + 真实进程，不 mock。

```
tests/test_proxy_guard.py::test_stop_will_not_kill_a_stale_pidfile PASSED
tests/test_proxy_guard.py::test_start_failure_leaves_no_child_and_no_pidfile PASSED
tests/test_capture_health.py::test_partial_injection_labels_fail PASSED
3 passed in 12.57s
```

### 1a. 陈旧 PID — 「进程仍活着」是断言，不是靠退出码

测试里 `innocent` 是一个真实 `sleep 60` 子进程，PIDFILE 写它的 PID、端口空闲。
断言是 `assert innocent.poll() is None`（进程仍活着），再加 `returncode != 0` 和
stderr 含 `not killing`。下面用等价场景直接跑守卫，贴出「进程仍活着」的实测：

```
=== scenario 1: stale pidfile names a live bystander (port free) ===
before stop: bystander 49914 alive
pidfile .../pidfile named pid 49914, but port 34212 is free; not killing (proxy already gone)
acl_proxy_stop exit=1
after stop:  bystander 49914 STILL ALIVE
```

退出码非零、被误指的进程 `49914` 仍活着、stderr 明确「not killing」。这正是 P1a 的要害：
旧代码在此会 `kill` 掉 49914 并返回 0。

### 1b. 启动失败残留 — 子进程与 PIDFILE 都被清掉

测试把 `ACL_PYTHON` 指到一个「能起来但不监听端口」的可执行（`exec` python sleep 60），
40×0.25s 确认循环超时后走失败路径。断言：`returncode != 0`、`pidfile` 不存在、
子进程（marker 记下它自己的 `$$`）消失。实测：

```
=== scenario 2: start whose child never takes the port ===
proxy pid 49929 did not take port 34213 (listener pid ); see .../log2
acl_proxy_start exit=1
pidfile absent (good)
child 49929 gone (good)
```

（首行 `Terminated: 15` 是 bash 对自己后台子进程被 SIGTERM 的作业通知，和既有
`acl_proxy_stop` kill 真实代理时同源，非本次新增、不影响退出码。）

### 1c. 部分注入标签 — 两个条数同时出现

`healthy()` 12 条里前 3 条标 `injection.id="i3"`、其余 9 条无标签，`check()` 产出：

```
FAIL 3 records declare an arm, 9 carry none: {'i3': 3}  <- one arm mixed with unlabelled traffic is two proxies in one file
```

「3」和「9」两个条数同时出现。测试另断言 `not any("must declare a single arm" in b)`
——这条 FAIL 与「两个 arm」的 FAIL 措辞不同，一眼可分。

---

## 2. 三条变异守护各自的失败信息

每条把对应修复改回旧行为，跑对应测试，贴实际失败。

### 变异一：`acl_proxy_stop` 去掉身份验证、直接 kill（改回旧行为）

```
FAILED tests/test_proxy_guard.py::test_stop_will_not_kill_a_stale_pidfile
E       AssertionError:
E       assert 0 != 0
E        +  where 0 = CompletedProcess([...], returncode=0, stdout='', stderr='').returncode
tests/test_proxy_guard.py:139: AssertionError
1 failed in 0.09s
```

旧行为 kill 掉了误指的进程、端口本就空闲、返回 0 —— 退出码断言第一个就崩。

### 变异二：`acl_proxy_start` 失败路径不清理（改回旧行为）

```
FAILED tests/test_proxy_guard.py::test_start_failure_leaves_no_child_and_no_pidfile
E       AssertionError: a failed start must not leave a pidfile
E       assert not True
E        +  where True = exists()
tests/test_proxy_guard.py:174: AssertionError
1 failed in 12.40s
```

旧行为留下 PIDFILE（子进程同样还在，断言按序先死在 pidfile 上）。

### 变异三：arm 闸改回只数带标签的记录（改回旧行为）

```
FAILED tests/test_capture_health.py::test_partial_injection_labels_fail
E       AssertionError: []
E       assert False
E        +  where False = any(<generator object ...>)
tests/test_capture_health.py:509: AssertionError
1 failed in 0.13s
```

旧闸对「3 条标 i3、9 条无标签」的混合 capture 判 `len(declared_arms)==1` 放行，`bad` 为空。

三条变异都真跑过，各贴一次失败信息如上。

---

## 3. 四份 capture 的断裂数（独立复算）

不经过 `capture_health` 主流程，逐条找 `injected_previous_message_id` 指到的前驱、
调 `ledger.broke_cache(prev, curr) is True` 计数：

```
capture.jsonl        : 75 records,  breaks=0 -> []
capture-02.jsonl     : 66 records,  breaks=1 -> ['msg_011C']
capture-03.jsonl     : 162 records, breaks=3 -> ['msg_011C', 'msg_011C', 'msg_011C']
capture-calib-1.jsonl: 28 records,  breaks=1 -> ['bae15c3e']
```

红线 **0 / 1 / 3 / 1**，对上了。（`capture-calib-1` 的断裂前驱 id 我截 8 位得
`bae15c3e`；上一轮 NOTES 记 `edb41217` 是另一个截取口径，计数一致，见第 6 节。）

四份 capture 的判决用改动后的 `scripts/capture_health.py` 各跑一遍复核：

- `capture` / `capture-02` / `capture-03`：arm 闸仍输出
  `PASS  0 campaign arms in one capture: {}`，无 FAIL，`USABLE`。
- `capture-calib-1`：arm 闸 `PASS  1 campaign arms in one capture: {'i0': 28}`；
  仍 `FAIL  lineage d11e8801 splices two conversations: record 13->14 messages 28->2
  <- collision polluted the break count`（外加既有的 beta-header 见证 FAIL），
  `NOT USABLE`。点名 `d11e8801`、13→14、28→2 不变。

---

## 4. 测试数变化

基线 **341 passed**。本次 +3 条新测试（2 条 proxy_guard + 1 条 capture_health），
未删改任何既有测试。改动后全量：

```
344 passed in 124.98s (0:02:04)
```

341 → 344，只增不减。

---

## 5. AC4 — 查了哪几处，结论

按「同一类数据的每条出口都过同一道闸，双向检查」，逐处过 `proxy_guard.sh` /
`capture.sh` 里所有用「存下来的值」当身份、或失败路径留未清理状态的地方：

- `scripts/proxy_guard.sh` 的 `_port_free` / `_listener_pid` / `acl_proxy_start` /
  `acl_proxy_stop` 全部四段。
  - `acl_proxy_stop` 原来直接 `kill $(cat PIDFILE)` —— 用存储值当身份，已修。
  - `acl_proxy_start` 失败路径原来留子进程 + PIDFILE —— 已修。
  - `acl_proxy_start` 起之前/起之后的端口占用 + 监听者确认本来就走端口，未动。
- `scripts/capture.sh` 所有 `kill` / `PIDFILE` / `cat` / `lsof` 出现点
  （第 15、39、40、55、56、82、84 行）。
  - 找到一处：`listening()` 是 `_port_free()` 的第二份实现（同一判断两份代码）。
    它的唯一调用方在 `status` 分支，删掉定义并把该调用改为 `_port_free()`。
    —— 注意规格说它「没有调用方」，实测有一个，见第 6 节。
  - `$PIDFILE.file`（记 capture 文件名，与 PID 无关）生命周期一致：start 只在
    `acl_proxy_start` 成功后才写（`|| exit 1` 之前），stop 成功后删；stop 失败时
    故意不删，留给排查。不构成「用存储值当身份」，不修。

结论：除 `listening()` 一处重复实现外，**没有第三处**用存储值代替端口真相，
也没有别的失败路径留未清理状态。找到了 1 处，修了 1 处。

---

## 6. 规格不对、或没能验证的地方

1. **「`listening()` 没有调用方」是错的。** 实测 `scripts/capture.sh:58` 的
   `status` 分支 `if ! listening` 就是它的调用方。若照字面「删掉无人调用的
   listening()」直接删，`capture.sh status` 会调用不存在的函数、恒判
   `not recording`。真正的类别修法是把重复实现收敛成一份：删 `listening()`，
   把唯一调用方改成 `_port_free()`（二者等价：`listening == ! _port_free`）。
   这超出了字面的「只删」，是必要的收敛，特此说明。

2. **Kill criterion 分析结论：未触发「正常场景下 stop 失败」，所以没有加
   「端口空了就当成功」的分支，保持严格。** 「stop 前验证 PID 拥有端口」会让
   `capture.sh stop` 在「端口已释放、PIDFILE 还在」时返回非零。我核过：正常 stop
   流程里代理在 stop 时刻仍监听端口，`listener == pid` 走正常路径；「端口空 +
   PIDFILE 还在」只会来自异常（代理中途崩了 / 被人手动 kill / 上一次 start 失败
   残留）。其中「上一次 start 失败残留」已被类别一的修复 #2 消灭。所以严格返回非零
   不破坏正常 stop，反而是守卫该响的警。若验收方认为「端口空了就当成功」是想要的设计，
   这是一个待裁定的决定，不在本次自行加入。

3. **端到端起真实代理未跑（未测）。** 与上一轮 NOTES 相同：`acl_proxy_start` 的
   「监听者 PID == 刚起的 PID」依赖 uvicorn 单进程不 fork（`proxy.py:350-353`
   确认过），但起真代理需要真实后端，本次未做端到端。守卫函数本身用真实 socket +
   真实进程测过。

4. **`capture-calib-1` 断裂前驱 id 的标签口径与上一轮不同。** 我独立复算按
   `injected_previous_message_id` 截 8 位得 `bae15c3e`；上一轮 NOTES 记 `edb41217`。
   断裂**计数**（1）一致，红线 0/1/3/1 不变，不影响任何判决。

5. **start 失败路径的 stderr 会多一行 bash 作业通知 `Terminated: 15`**（见 1b）。
   与既有 `acl_proxy_stop` kill 真实代理同源，属 bash 对后台子进程被信号的汇报，
   非本次引入的语义变化，不干扰退出码与断言。
