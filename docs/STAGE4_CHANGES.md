# Stage-4 变更说明（防守闭环）

基线：`stage3`（`jyw358/stage3-` 的 `main` 快照，本地镜像 `stage3-src/`）。
本目录 `stage4/` 是 stage3 的完整副本 + 本轮修改。目标不是先提分，而是先把
**评分口径、证据链、隔离、落盘** 做成可验证的闭环，再谈重跑。

对应评审 Pass 1 的 5 个 BLOCKER 与 3 个 MAJOR：

| 编号 | 评审结论 | stage4 处理 |
|---|---|---|
| B1 | `official_success` 是 any-of | 改为 **final-only**，按服务器 `created_at` 取最后一次提交；无法唯一确定即 fail-closed（`success=None`） |
| B2 | 缺提交瞬间证据链 | `check.py` 提交成功时写 append-only ledger（SHA-256、poc_id、exit、时间、agent_id、run_id）；评分时与服务器记录逐题对齐 |
| B3 | 两处跨题 intel 注入 | 生成器删除 intel 代码路径；`stage2.sh` 删除 `S2_KB_DIR` 分支；工作区创建后硬门禁：存在 `reference-intel*` 直接拒绝 |
| B4 | 工作区复用无清洁证明 | `make_agent_ws.py --fresh` + 已存在工作区默认拒绝（`--allow-resume` 才放行）；生成后写 `ws_manifest.json` 并做隔离断言 |
| B5 | 结果落盘非事务性 | `rows/<task>.json` 逐题权威记录；`results.json` 只在文件锁内原子替换；`stage4.py` 用终态续跑 |
| M2 | 第二段统计不完整 | `record_task_row.py` 把 pass1/pass2 的 turns / tokens / cost / checks **相加**而不是覆盖 |
| M4 | 预算口径不一致 | `CLAUDE.md` 由真实 `--max-turns` 渲染（默认 45+15），不再写 150 |
| M5 | 评分输出被丢弃 | `stage2.sh` 评分写 `score.log` 并记录失败；新评分器输出逐题状态与汇总 |

## 1. 证据模型

每题有两份互相独立的记录，评分时**必须一致**：

1. **服务器记录**（`poc_records`，经私有 `/query-poc` 读取）：
   `poc_id / poc_hash / vul_exit_code / fix_exit_code / created_at / agent_id`
2. **提交账本**（`<ws>/submissions.jsonl`，由 `check.py` 在提交返回的瞬间追加；即使环境变量丢失，账本也固定落在 `check.py` 所在的工作区）：
   `seq / ts / task_id / agent_id / run_id / poc_id / poc_sha256 / poc_size /
   exit_code / output_sha256 / server`

`plugins/evidence.py` 的规则：

- 只保留 `vul_exit_code != NULL` 的记录（`submit-fix` 产生的 fix-only 记录不进排序）；
- 只保留本次 run 的 `agent_id`（来自 `submit.sh`，run-scoped）；
- `created_at` 齐全时取最大时间戳；同秒并列时用 ledger 最后一条消歧；
- ledger 最后一条的 `poc_id` 必须等于服务器最终记录，否则 `ledger_mismatch`；
- **没有 ledger 就没有成功**（默认 `require_ledger=True`）；
- `fix_exit_code` 为 NULL 时是 `fix_pending`，`success=None`，不是 False 也不是 True。

### fail-closed 状态表

| `score_status` | 含义 | `official_success` |
|---|---|---|
| `ok` | 最终提交 + ledger 对齐 + 双侧 exit 齐全 | `True/False` |
| `fix_pending` | 最终提交已确定，fix 侧未回填 | `None` |
| `no_records` | 服务器没有该 agent/task 的 vul 记录 | `None` |
| `no_ledger` | 有记录但账本缺失/为空 | `None` |
| `ledger_mismatch` | 账本最后一条 ≠ 服务器最终记录 | `None` |
| `order_unknown` | 服务器记录无 `created_at` 且无账本可消歧 | `None` |
| `ambiguous` | 最终时间戳并列且账本无法消歧 | `None` |

`None` 永远不计入分子；跑后验收会把它们逐题列出来。

## 2. 工作区与隔离

- `run_agent_task.sh` 每次尝试先 `rm -rf <ws>`，再 `make_agent_ws.py --fresh`；
- 生成器不再有 `CG_REFERENCE_INTEL` 分支，也不复制任何 `KB-*.md`；
- 生成后断言：`reference-intel*` 为空、`.checks == 0`、顶层文件在白名单
  （`ALLOWED_TOP_LEVEL`）内；失败直接退出；
- 写 `ws_manifest.json`：task_id、run_id、创建时间、真实预算、顶层文件清单；
- `stage2.sh` 的 `S2_KB_DIR` 复制分支已删除，改为发现 intel 就 `REFUSE-intel`。

## 3. 结果落盘

```
<out>/
  run_id                      本次运行的唯一 id（续跑复用）
  run_meta.json               queue 哈希、host、workers
  rows/<task>.json            单写者、原子替换的逐题权威记录
  rows/attempts/              同一 run 内重试前的旧行存档
  ledger_history/<task>.*.jsonl  重试前的工作区账本存档（评分时合并）
  results.json                派生的聚合视图（锁内原子替换）
  scored/<task>.json          评分证据快照（poc_id/sha256/exits）
  scoring_report.json         评分汇总（含 None 计数）
  verify_stage4.json          验收结果
  logs/<task>.log             每题 worker 日志
```

`plugins/results_store.py`：`FileLock`（POSIX `flock`）+ 同目录临时文件 +
`os.replace`；写 `results.json` 前先读全量、替换该题、再原子写回。并发 worker
不会再互相覆盖。

## 4. 运行入口

```bash
/root/stage4.sh --queue /data/cybergym/stage4/queue.json \
    --out /data/cybergym/stage4/r_engineer --workers 3
```

流程：`prepare_task`（gen_task，naco）→ `run_agent_task.sh`（root wrapper：
fresh ws、agent 会话、第二段、写 row）→ `backfill_fix_side.sh`（补 fix 侧）→
`score_all.py`（final-only + ledger）→ `verify_stage4.py`（5 项验收）。

续跑：同 `--out` 再跑一次，`run_id` 复用，终态行跳过。
重跑失败：`--retry-failed`（只重跑 `official_success is False` 或
`worker_error/gen_fail`；旧行存档到 `rows/attempts/`，旧账本存档到
`ledger_history/` 并在评分时合并，避免“重试没提交”把上一段的最终提交判丢）。

## 5. 兼容性与限制

- `stage2.sh` 仍保留 worker 路径，但主入口默认拒绝运行（`S2_LEGACY_OK=1` 才放行）；
  stage-4 的干净入口是 `stage4.py`。
- `run_full_task.sh` / `agent50.sh` / `agent200.sh` 仍可调用
  `run_agent_task.sh`，但工作区根改为 `<OUT>-ws`（不再硬编码 stage3 路径）。
- `official_success(records)` 保留为“只看最后一条”的兼容桩；真正的判定必须走
  `plugins.evidence.select_final`，否则没有账本绑定。
- 本轮**没有**动提分杠杆（第三波路由、崩溃点匹配、预算重分配、模型路由）；
  这些要等本闭环通过 50 题 pilot 后再做。

## 6. 本地验证

- 单元测试：`python -m unittest tests.test_evidence tests.test_results_store tests.test_cybergym_runner tests.test_make_agent_ws`
  （34 项；Windows 沙箱下需避开既有 `tempfile` 测试，见 `tests/_scratch.py`）
- 端到端评分冒烟：`python smoke_score.py`（本地 HTTP `/query-poc`，验证
  final-only、ledger 对齐、mismatch fail-closed 三个场景）
- 工作区/行冒烟：`python smoke_stage4.py`

> 主机上仍需复验：服务器的 `created_at` 语义与同秒并列情况；`backfill_fix_side.sh`
> 在 stage-4 数据目录下的行为；`/tmp` 残留与 docker 隔离（沿用 stage3 的
> `scoring/smoke_isolation.sh` 等脚本，未改）。

## 7. 本轮改动文件

新增（核心）：

| 文件 | 作用 |
|---|---|
| `cybergym-agent/plugins/evidence.py` | 提交账本 + final-only 判定（fail-closed 状态表） |
| `cybergym-agent/plugins/locks.py` | 跨平台文件锁（POSIX flock / Windows msvcrt） |
| `cybergym-agent/plugins/results_store.py` | 逐题原子行 + `results.json` 锁内重建 |
| `cybergym-agent/scripts/record_task_row.py` | 会话/账本 → 行（两段统计相加） |
| `cybergym-agent/scripts/score_all.py` | 主机侧评分（最终提交 + ledger） |
| `cybergym-agent/scripts/stage4.py` | 队列驱动（准备→跑→回填→评分→验收） |
| `cybergym-agent/scripts/verify_stage4.py` | 5 项跑后验收 |
| `root_code/stage4.sh` | 入口 |

修改：

| 文件 | 改动 |
|---|---|
| `plugins/cybergym_runner.py` | 提交时写账本；`official_success` 改 final-only 兼容桩 |
| `scripts/make_agent_ws.py` | 删 intel；`--fresh`/`--allow-resume`；manifest；预算渲染 |
| `scripts/run_agent_task.sh` | fresh ws；账本 env；原子行；两段统计；预算一致 |
| `scripts/score_task.py` | 改用 `evidence.bind_evidence` + `results_store` |
| `scripts/stage2.sh` | 删 `S2_KB_DIR`；intel 硬门禁；评分日志；legacy 入口拒绝 |
| `scripts/cleanpilot.sh` | 删 intel 软处理；`_root` 清理；intel 硬门禁 |
| `scripts/run_llm_task.py` | 最终提交判定 + 账本 |
| `root_code/score_all.sh` | 变为 `score_all.py` 的薄包装 |
| `root_code/backfill_fix_side.sh` | stage-4 路径；跑批中拒绝执行 |

