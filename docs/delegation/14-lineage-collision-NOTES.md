# 委托任务 14 交付说明 — 两条会话被并成一条 lineage，闸门看不见

改动落点：

- `scripts/capture_health.py` — 新增闸门 A（#72，同一 lineage 内消息条数严格变短）与
  闸门 B（#73，一份 capture 只许声明一个 `injection.id`）
- `scripts/proxy_guard.sh`（新增）— 端口守卫 `acl_proxy_start` / `acl_proxy_stop`
- `scripts/capture.sh` — start/stop 改走这两个函数，其余未动
- `scripts/reduce_collision_fixture.py`（新增）— 可重跑的 fixture 缩减脚本
- `fixtures/lineage/collision-calib.json`（新增）— 缩减后的回归 fixture
- `tests/test_capture_health.py` — 新增闸门 A/B 与 fixture 的 9 条测试
- `tests/test_proxy_guard.py`（新增）— 端口守卫的真实 socket 测试 2 条
- `docs/e4-injection-campaign.md` — 采集侧 nonce 规避说明

`src/agentcostlab/` 与 `scripts/score_injection.py` 一行未动。

---

## 1. 四份 capture 的 `capture_health.py` 输出全文

以下为 `.venv/bin/python scripts/capture_health.py <path>` 的完整 stdout，每份末尾附退出码。

### `data/raw/capture.jsonl`（exit 0）

```
  PASS  74/75 requests reached upstream
  PASS  usage recorded on 73/74 served requests (pre-stream_complete capture, judged by fraction)
  PASS  0 instrument failures (parse / encoding)
  PASS  0 transport errors, tolerable up to 7
  PASS  0 requests threaded across lineages
  PASS  0 requests name a predecessor outside this capture
  PASS  63 requests carried previous_message_id
  PASS  0 campaign arms in one capture: {}
  PASS  0/63 threaded turns came back previous_message_not_found / unavailable / no reason returned
  PASS  largest lineage has 46 turns
  ----  beta effectiveness UNDECIDABLE on 62/62 answerable turns: they predate diagnostics_present, and a null diagnostics is indistinguishable from an absent one

  lineages=12  main_lineage_turns=46
  official verdicts obtained: 0 of 63 threaded turns
      zero verdicts is consistent with a clean session AND with a dead beta header.
      A turn that lost the cache and still carried no diagnostics key would settle it;
      this capture has none, so it stays open.

  supports:
    YES  E1 1.2  divergence rate            63 threaded turns; a clean session is legitimate data
    NO   E1 1.1  miss-cause distribution    needs >=1 conclusive official verdict, has 0
    NO   #10     calibration vs official    needs >=1 conclusive official verdict, has 0

USABLE for E1 1.2  —  not for E1 1.1, #10
```

### `data/raw/capture-02.jsonl`（exit 0）

```
  PASS  65/66 requests reached upstream
  PASS  usage recorded on 65/65 served requests, 0 client-aborted
  PASS  0 instrument failures (parse / encoding)
  PASS  0 transport errors, tolerable up to 6
  PASS  0 requests threaded across lineages
  PASS  0 requests name a predecessor outside this capture
  PASS  49 requests carried previous_message_id
  PASS  0 campaign arms in one capture: {}
  PASS  beta header alive: a diagnostics key came back on 49/49 answerable turns
  PASS  1/49 threaded turns came back previous_message_not_found / unavailable / no reason returned
  PASS  largest lineage has 33 turns

  lineages=17  main_lineage_turns=33
  official verdicts obtained: 0 of 49 threaded turns  {'unavailable': 1}
      zero verdicts is consistent with a clean session AND with a dead beta header.
      A turn that lost the cache and still carried no diagnostics key would settle it;
      this capture has none, so it stays open.

  supports:
    YES  E1 1.2  divergence rate            49 threaded turns; a clean session is legitimate data
    NO   E1 1.1  miss-cause distribution    needs >=1 conclusive official verdict, has 0 (1 inconclusive)
    NO   #10     calibration vs official    needs >=1 conclusive official verdict, has 0 (1 inconclusive)

USABLE for E1 1.2  —  not for E1 1.1, #10
```

### `data/raw/capture-03.jsonl`（exit 0）

```
  PASS  158/162 requests reached upstream
  PASS  usage recorded on 158/158 served requests, 0 client-aborted
  PASS  0 instrument failures (parse / encoding)
  PASS  2 transport errors, tolerable up to 16
  PASS  0 requests threaded across lineages
  PASS  0 requests name a predecessor outside this capture
  PASS  133 requests carried previous_message_id
  PASS  0 campaign arms in one capture: {}
  PASS  beta header alive: a diagnostics key came back on 131/131 answerable turns
  PASS  3/133 threaded turns came back previous_message_not_found / unavailable / no reason returned
  PASS  largest lineage has 46 turns

  lineages=28  main_lineage_turns=46
  official verdicts obtained: 0 of 133 threaded turns  {'unavailable': 3}
      zero verdicts is consistent with a clean session AND with a dead beta header.
      A turn that lost the cache and still carried no diagnostics key would settle it;
      this capture has none, so it stays open.

  supports:
    YES  E1 1.2  divergence rate            133 threaded turns; a clean session is legitimate data
    NO   E1 1.1  miss-cause distribution    needs >=1 conclusive official verdict, has 0 (3 inconclusive)
    NO   #10     calibration vs official    needs >=1 conclusive official verdict, has 0 (3 inconclusive)

USABLE for E1 1.2  —  not for E1 1.1, #10
```

### `data/raw/capture-calib-1.jsonl`（exit 2）

```
  PASS  28/28 requests reached upstream
  PASS  usage recorded on 28/28 served requests, 0 client-aborted
  PASS  0 instrument failures (parse / encoding)
  PASS  0 transport errors, tolerable up to 2
  PASS  0 requests threaded across lineages
  PASS  0 requests name a predecessor outside this capture
  PASS  26 requests carried previous_message_id
  PASS  1 campaign arms in one capture: {'i0': 28}
  PASS  0/26 threaded turns came back previous_message_not_found / unavailable / no reason returned
  PASS  largest lineage has 26 turns
  FAIL  lineage d11e8801 splices two conversations: record 13->14 messages 28->2  <- collision polluted the break count
  FAIL  1 turns lost the cache and still carried no diagnostics key  <- beta header never took effect

  lineages=2  main_lineage_turns=26
  official verdicts obtained: 0 of 26 threaded turns
      zero verdicts is consistent with a clean session AND with a dead beta header.
      A turn that lost the cache and still carried no diagnostics key would settle it;
      this capture has none, so it stays open.

  supports:
    YES  E1 1.2  divergence rate            26 threaded turns; a clean session is legitimate data
    NO   E1 1.1  miss-cause distribution    needs >=1 conclusive official verdict, has 0
    NO   #10     calibration vs official    needs >=1 conclusive official verdict, has 0

NOT USABLE — do not spend analysis time on this
```

闸门 A 的 FAIL 只在 `capture-calib-1.jsonl` 上出现，点名 `d11e8801`、记录 13→14、
28→2，与规格一致。注意 `capture-calib-1` 有**两条** FAIL：第二条是既有的 beta-header
见证闸（见第 7 节）。

---

## 2. 独立算出的四个断裂数 + 闸门 A 碰撞条数

断裂数独立复算：逐条记录找 `injected_previous_message_id` 指到的前驱，调
`ledger.broke_cache(prev, curr) is True` 计数（与 `_broke_cache` 同法，不经过
capture_health 的主流程）：

```
capture.jsonl        : 75 records, breaks=0 -> []
capture-02.jsonl     : 66 records, breaks=1 -> ['msg_011C']
capture-03.jsonl     : 162 records, breaks=3 -> ['msg_011C', 'msg_011C', 'msg_011C']
capture-calib-1.jsonl: 28 records, breaks=1 -> ['edb41217']
```

红线 0 / 1 / 3 / 1，对上了。

闸门 A（同一 lineage 组内 `len(messages)` 严格变短）独立复算：

```
capture.jsonl        : 75 records, 12 lineages, shrinks=0
capture-02.jsonl     : 66 records, 17 lineages, shrinks=0
capture-03.jsonl     : 162 records, 28 lineages, shrinks=0
capture-calib-1.jsonl: 28 records, 2 lineages, shrinks=1
    ('d11e8801', 13, 14, 28, 2, True)
```

三份历史 capture 闸门 A 碰撞 0 条；`capture-calib-1` 碰撞 1 条、`harmful=True`。
与规格判据丙的「0 误报 / 1 真阳性」一致。

---

## 3. fixture 缩减规则 + 缩减前后判决一致的证据

缩减脚本 `scripts/reduce_collision_fixture.py`（可重跑，读
`data/raw/capture-calib-1.jsonl`、写 `fixtures/lineage/collision-calib.json`）规则：

- 保留原样：`response_id`、`injected_previous_message_id`、`status_code`、
  `usage`、`injection`。`usage` 是 `ledger.broke_cache` 要读的；id / threading
  让 fixture 仍是忠实的记录形状，其余闸门行为不漂移；`injection` 是 `i0` 且
  `detail: null`，不含自由文本。
- 精确保留 `len(messages)` —— 接缝就是「28 → 2」。
- 每条消息的 `content` 换成占位符：`messages[0]` 按**首次出现顺序**赋
  `lineage-<n>`（让原本共用首消息的两条记录仍塌进同一条 lineage，两个 lineage 保持
  区分），其余消息 `msg-<pos>`，`role` 保留。
- 丢掉一切自由文本字段（`system` / `tools` / `metadata` / `model` /
  `system_prompt` / 时间戳 / 字节数 / `diagnostics`）。

判决一致的证据（脚本 `_tmp_verify_fixture.py` 对原始 28 条与 fixture 各跑一次
`_lineage_collisions`）：

```
original: 1 splice(s)
    {'lineage': 'd11e8801', 'prev_i': 13, 'curr_i': 14, 'prev_n': 28, 'curr_n': 2, 'harmful': True}
fixture: 1 splice(s)
    {'lineage': '82c5e41f', 'prev_i': 13, 'curr_i': 14, 'prev_n': 28, 'curr_n': 2, 'harmful': True}
fixture contains '<transcript>'?  False
fixture contains 'claudeMd'?  False
```

lineage 哈希不同（占位符替换了原始提示词，`_lineage_key` 的输入变了），但**判决结构
相同**：同一个接缝位置 13→14、同样的 28→2、同样的 `harmful=True`，且无自由文本残留。
这条已固化为 `tests/test_capture_health.py` 的 `test_reduced_fixture_reproduces_the_calibration_splice`
与 `test_reduced_fixture_fails_the_health_gate_on_the_splice`。

---

## 4. AC5 两条测试的实际退出码和输出

真实 socket、不 mock lsof。两条都用随机空闲端口 + 覆盖 `PORT`/`PIDFILE` 环境变量
（守卫用 `PORT="${PORT:-8787}"` 等默认值，测试时覆盖，不与真实 8787 冲突）。

**场景一：端口已被占用 → `acl_proxy_start`**

```
exit_code = 1
stderr = 'port 58584 already in use (listener pid 40410); refusing to start a second proxy'
stdout = ''
```

（先起一个真实 bind+listen 的进程占住端口，再调 `acl_proxy_start`；非零退出，
且守卫在起任何东西**之前**就拒绝，未写 pidfile。）

**场景二：SIGTERM 免疫的进程 → `acl_proxy_stop` 超时**

```
exit_code = 1
stderr = 'port 58587 still in use after kill (listener pid 40417); aborting'
stdout = ''
```

（`signal.signal(SIGTERM, SIG_IGN)` 的进程 hold 住端口，写 pidfile 后调
`acl_proxy_stop`；kill 无效，轮询 40×0.25s 后超时非零退出。）

对应测试：`tests/test_proxy_guard.py` 的
`test_start_fails_when_port_is_already_in_use` 与
`test_stop_times_out_when_the_process_ignores_sigterm`，`pytest` 下 2 passed。

---

## 5. 测试数变化

规格给的基线「311 passed, 16 skipped」是**干净 checkout（无 data）**的数字。本
worktree 里真实数据已就位，改动前的基线是 **327 passed / 0 skipped**；改动后
`pytest` 全量：

```
338 passed in 101.97s (0:01:41)
```

即 **327 → 338，+11**（闸门 A/B + fixture 共 9 条，端口守卫 2 条），skipped 0。
只增不减。若验收方用干净 checkout 复跑，11 条新增测试与其余测试在无 data 时的
行为未单独核（见第 7 节），此处如实标注。

---

## 6. 三条变异守护的失败信息

### AC2 — 判据改成恒假（`if False:` 替代 `if nb < na:`）

`pytest tests/test_capture_health.py -k "calib or collision or harmful_splice or shrink"`：

```
FAILED tests/test_capture_health.py::test_collision_is_detected_without_a_threading_pointer
FAILED tests/test_capture_health.py::test_a_shrink_that_does_not_break_the_cache_is_reported_not_failed
FAILED tests/test_capture_health.py::test_a_harmful_splice_fails_the_capture
FAILED tests/test_capture_health.py::test_reduced_fixture_reproduces_the_calibration_splice
================== 4 failed, 2 passed, 36 deselected in 0.13s ==================
```

关键失败：`test_reduced_fixture_reproduces_the_calibration_splice`
`assert len(findings) == 1` 得到 `[]`（恒假后 collision 永不被检出）。

### AC3 — 判据放宽成「非追加」（`pb[:na] != pa` 替代 `nb < na`）

`pytest tests/test_capture_health.py -k "real_messages_changed or shrink or harmful_splice or collision_is_detected or fixture"`：

```
FAILED tests/test_capture_health.py::test_a_real_messages_changed_break_is_not_a_collision
================== 1 failed, 6 passed, 35 deselected in 0.11s ==================
```

关键失败：`test_a_real_messages_changed_break_is_not_a_collision`
`assert health._lineage_collisions([prev, curr]) == []` 得到一条
`{'lineage': '074883bf', 'prev_i': 0, 'curr_i': 1, 'prev_n': 2, ...}` —— 非追加判据
把「前缀被改过、条数仍在增长」的真实断裂误报成拼接，正是判据乙被否的原因。

### AC4 — 有害拼接 FAIL 改成静默跳过（`bad.append(...)` → `pass`）

`pytest tests/test_capture_health.py -k "harmful_splice or fixture_fails"`：

```
FAILED tests/test_capture_health.py::test_a_harmful_splice_fails_the_capture
FAILED tests/test_capture_health.py::test_reduced_fixture_fails_the_health_gate_on_the_splice
======================= 2 failed, 40 deselected in 0.12s ======================
```

关键失败：两条都 `assert any(... in b for b in bad)` 但 `bad` 里不再有拼接 FAIL 行。

三次变异后均已还原；`grep -r MUTATION` 无残留，全量 42 条 capture_health 测试
恢复通过。

---

## 7. 我认为规格不对、或没能验证的地方

**（一）AC1 表里「capture.jsonl 不 FAIL；d725e692 那类无害碰撞报告出来」与判据丙
自相矛盾。** 判据丙是「消息条数严格变短」，而 `d725e692` 组（19 / 16 / 18 条记录）
每条都是 **2 条消息、条数恒定**，从不变短——所以判据丙**不可能**报告它。我的实现
在 `capture.jsonl` / `capture-02` / `capture-03` 上闸门 A 碰撞 0 条，没有任何
`----` 无害碰撞行输出。我选择保住规格自己钉死的那条硬约束（「判据丙的 0 误报 /
1 真阳性是硬的，碰撞计数不是」，见规格「开放 AC」注），如实报告：无害碰撞
`d725e692` 确实存在（19 / 16 / 18 条，独立脚本量得），但对 FAIL 闸门**设计上不可见**。
若验收方坚持 AC1 表要「报告出来」，那需要另一条不同于判据丙的检测（例如按
`messages[0]` 相同 + 组内记录来自多次独立会话去判），本次未实现——那是判据甲/乙
的领域，已在规格里被否。

**（二）`capture-calib-1.jsonl` 有两条 FAIL，不是一条。** 除闸门 A 的
`d11e8801` 外，既有的 beta-header 见证闸也 FAIL：
`1 turns lost the cache and still carried no diagnostics key <- beta header never took effect`。
两条 FAIL 的根因是同一个假断裂：碰撞让 record 14 的 `cache_read` 归零，
`ledger.broke_cache` 判 True，既喂给了闸门 A（正确地报「collision polluted the
break count」），也喂给了 beta-header 闸（误判成「beta 头没生效」）。后者是既有行为，
不在本次改动内；但它说明**同一次假断裂会同时污染两道闸的结论**，将来若有人只修
闸门 A 不看 beta-header 闸，calib 这行 FAIL 仍在。

**（三）基线「311 passed / 16 skipped」在本 worktree 无法复现。** 那是无 data 的
干净 checkout 数字；本 worktree data 就位，改动前实测是 327/0（见第 5 节）。我按
327→338 报告增量；311/16 那个绝对数没有在本 worktree 复跑核验。

**（四）`report/99-open-questions.md` 的「三份 capture、57 个 lineage 组零碰撞」
仍是错的**（规格第 72 行已点名）。`d725e692` 组在三份 capture 里都存在。本次未改
该文件（不在改动范围），仅在此记录。

**（五）`acl_proxy_start` 的「监听者 PID == 刚起的 PID」检查依赖 uvicorn 不 fork。**
`src/agentcostlab/proxy.py` 是 `uvicorn.run(app, host="127.0.0.1", port=8787)` 单进程、
无 `--reload`、无 `workers`，所以 `$!` 就是监听者。这是读了 `proxy.py:350-353` 后
确认的，但没有起一次真实代理做端到端验证（起真代理需要真实后端，超出本次范围）。
