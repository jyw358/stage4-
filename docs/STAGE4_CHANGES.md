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

## 0. 第二轮评审修复（B7–B12 + MAJOR/MINOR）

第二轮静态审查（REVIEW_PROMPT.md 第 6 节，范围 3.3–3.6）新增 6 个 BLOCKER
候选，全部处理如下；逐条可由 `tests/`（85 项）与两个 smoke 复验。

| 编号 | 评审结论 | stage4 处理 |
|---|---|---|
| B7 | 评分/回填/验收失败，主驱动仍可 exit 0 | `stage4.py` 逐阶段捕获退出码（`phase_exit_codes`）；任一阶段失败或**脚本缺失**（rc=127）→ 整轮 exit 1；`batch_summary.json` 三类计数（execution_failures / scoring_pending / phase_failures） |
| B8 | 双文件写崩溃窗口；`rebuild()` 复活旧成功 | `merge_row` 先写权威 `rows/` 再写派生聚合；`rebuild` **只从 rows/ 重建**（legacy 聚合行一律丢弃）；聚合始终由 rows/ 派生，崩溃最多使聚合暂时陈旧 |
| B9 | `submitted` 即终态，评分未决被跳过 | 终态拆分 `is_execution_terminal` / `is_scoring_terminal`（`SCORE_INCOMPLETE={None,pending,query_error,no_task_dir}`）；续跑时 submitted+未决进 `scoring_recovery`（评分阶段恢复），**绝不重跑 agent**；`--retry-failed` 同样不重跑 |
| B10 | 全 gen_fail 可 0 violations | 验收拆为**数据完整性**与**执行覆盖率**：`gen_fail/worker_error/api_error` 逐题 `coverage_infra` violation；`run_mixed`（run 混批）violation；聚合按字段（status/submissions/cost/score_status/run_id）比对；`scored/` 与行交叉核对；缺 `cost_usd` 为 violation |
| B11 | `chmod 666` + `chattr +a` ≠ 可信账本 | 受保护账本改为 **root:root 0644**（agent 无任何写路径）；唯一写入者是 root 侧 `scripts/promote_ledger.py`（pass1/pass2 后由 wrapper 调用，按 `poc_id` 去重追加）；`evidence.record_submission` 删除镜像路径，`CG_PROTECTED_LEDGER` 出现在 agent 环境即告警忽略；`make_agent_ws --protected-ledger` 废弃；check.py 烘焙时主动 `pop` 该变量 |
| B12 | 外层超时只杀直接子进程 | 每题 `start_new_session=True` 独立进程组；超时先 `killpg(SIGTERM)`、宽限后 `SIGKILL`，确认退出后才写行/重试 |

第二轮 MAJOR：`stage2.sh`/`cleanpilot.sh` 的 `S2_LEGACY_OK` 门禁移到 **worker
子命令之前**（直接 `bash stage2.sh worker <t>` 也被拒）；task_id→文件名映射
碰撞启动即拒（`assert_unique_task_ids`）；损坏行文件 strict 读取显式失败
（`ResultsIntegrityError`）；locks 增加 `fs_type`/`assert_local_lockable`
（NFS 等网络 FS 拒绝多 worker）；归档名加 UUID 且归档-清零全程持锁；
`<out>/.run.lock` run 级独占锁（第二个 driver 直接拒绝）；验收器
`agent_id` 双侧必须非空且严格相等；intel 审计递归且覆盖
`reference_intel*`/`ref-intel*` 等改名拼写；权威账本必须存在、非空、可解析
（工作区账本不再作为验收等价物）；`run_meta.json`/`run_id`/`rows_index.json`/
`batch_summary.json` 全部原子写；`.stage4_run_id` marker root 属主校验。
MINOR：`_chown_root` 失败显式告警；缺成本不再按 0 汇总（单列 violation）。

## 0.5 第三轮评审修复（专用用户隔离实施）

评审对《专用用户隔离提案》（commit f622b7c）的裁决：T1（树信任）成立 P0；
专用用户方向 APPROVE；但 3.2 补丁 REQUEST CHANGES——单一 UID 不批准作为唯一
隔离措施、gen_task 不得与 agent 同 UID、root marker 现有检查不充分、账本
信任描述失实。本轮按裁决**真实落地**（部署路径 `/opt/cybergym-agent`，
见 STAGE4_DEPLOY.md）：

| 评审阻断点 | 处理 |
|---|---|
| 信任必须在 Python 启动前建立 | 新增 root 侧可信启动器：`root_code/stage4.sh`（部署 `/root/`）在 exec python 前 ①清掉树内 `__pycache__` ②调用 `root_code/assert_code_tree.py`（部署 `/root/`，stdlib-only，不从被审计树 import 任何东西）审计全树+全部祖先（符号链接/go-w/非 root 属主/`__pycache__`/prep·agent 用户可写祖先全部拒绝）。`stage4.py` 内保留**防御性**早断言（仅 lstat 即将 import 的面），明确注释它不是信任来源 |
| 单一 UID 不能隔离同批工作区 | 每题分配**唯一数字 uid**：driver 在控制面锁内从 `S4_AGENT_UID_BASE`（默认 30001）池取号（跳过 passwd 已有 uid），run 内不复用；wrapper 以 `sudo -u '#<uid>'` 跑 agent，显式 env（无 `--preserve-env`）：私有 `HOME/.tmp/XDG_*/CLAUDE_CONFIG_DIR`（全新 0700）+ umask 077；ws 属主=该 uid、0700；ws 根 root:root 0711 |
| gen_task 不得与 agent 同 UID | 身份三分离：root（驱动/评分/晋升）/ `S4_PREP_USER`（默认 naco，仅 gen_task+题目录）/ 每题 uid（仅本工作区）；`chown_prep` fail-closed（旧版吞异常） |
| marker 绑定不可替换 | 控制面 `<out>/control/<task>/` root:root 0700：`run_id`/`agent_id`/`submit.sh` sha256/`agent_uid`，O_NOFOLLOW+原子替换写入；wrapper 消费前校验目录属主、run_id 一致、submit.sh 哈希一致、agent_id 一致，任一不符 REFUSE；复用题目录必须过 `validate_reuse`（哈希+agent_id），`.stage4_run_id` marker 降级为遗留提示 |
| 账本信任描述失实 | `promote_ledger.py` docstring 按 R3 信任模型重写：受保护账本 = **root 复制并封存的不可信提交声明**，root 落盘不是独立见证；评分权威 = 服务器 final 记录 + 哈希门禁；可信提交代理列为分阶段目标 |
| promoter 保留不可信预写值 | `setdefault` 改为 root 提供值**强制覆盖**（run_id/task_id/agent_id），预写值不一致直接 rc=5 拒绝；坏行/非 dict/缺 poc_id 一律硬失败 rc=2（不再 WARN+跳过）；同 poc_id 不同哈希 → 单列到 `<protected>.conflicts.jsonl` + rc=4，验收报 `ledger_conflict` |
| 路径硬编码 `/home/naco/...` | 部署根改为 `/opt/cybergym-agent`（`S4_AGENT_HOME` 默认值）；`make_agent_ws.py` 的 gate/工具源/check.py 烘焙路径全部由脚本自身位置派生（`AG_HOME`）；agcli 改 `/usr/local/bin`（`S4_AGCLI`，免开 /home/naco 遍历）；`run.env` 的 CG_SERVER 走 env |
| 验收未覆盖新防线 | `verify_stage4.py` 新增：控制面绑定核对（`control_missing`/`control_mismatch`，`<out>/control` 存在即强制）、`ledger_conflict` violation、控制面整体缺席 warning（`control_plane_absent`） |

工作区生成器（`make_agent_ws.py`）改由 **root 直接运行**（读完题目录、产出
新树后整体 chown 给每题 uid），准备身份只保留 gen_task；部署检查清单
（`sudo -l -U s4agent`、`namei -l`、`find \( -perm /022 -o -type l \)`、
每题 uid 负向读测试等）见 STAGE4_DEPLOY.md 第 2/4/9 节。

## 0.6 R3 复审修复（跨题准备目录隔离等）

第三轮复审（R3 修订复审）裁决 REQUEST CHANGES，核心确认 R3 三项阻断点已修，
但发现准备目录仍可跨题读取。本轮按下表落地：

| 编号 | 问题 | 处理 |
|---|---|---|
| R3-B1（P0） | 准备目录 0755（naco 所有）→ uid_A 可读 task_B 的 description.txt/submit.sh | `harden_task_dir()`：prep 期间 0700 naco（gen 写），prep 结束后整树 chown 给**本题 uid**、目录 0700；`task_base` 改 root:root 0711（可遍历不可列）；wrapper 启动前复核 `mode=700` 且属主 = 控制面 uid，不符 REFUSE；verify 新增 `taskdir_open`/`taskdir_unbound` 验收 |
| R3-B2（P1） | uid 池只保证 run 内不复用，跨 run 可能重发 30001 读到旧 0700 文件 | 新增**宿主机级只增不减 UID 注册表**（`S4_UID_STATE`，默认 `/var/lib/cybergym/stage4/uid_registry.json`，root 0700/0600）：已发号永不再发，跨 run 隔离由构造保证；注册表损坏 fail-closed |
| R3-M1 | 控制面只校验最后一级目录，祖先链可被替换；树审计不覆盖 `--out` | `ancestors_trusted()`：从 `/` 到 `--out` 的每一级必须 root 所有、非 group/other 可写、无符号链接；`init_control_plane`、`control_write`、verify 控制面检查全部复用 |
| R3-M2 | wrapper 用 `${S4_AGENT_UID:-control}`，只校验"是数字"，可跑未签发的 uid | 新增 `scripts/validate_agent_uid.py`（可单测的纯函数）：**控制面 uid 为准**，环境变量存在时必须严格相等；拒绝 uid 0、prep 身份 uid、池外 uid；wrapper 以它为准 |
| R3-M3 | promoter 失败被降级 WARN，仍跑第二段、照常写行 | `promote_or_fail`：失败立即记录 `evidence_error` 行并以 rc=6 终止，**不跑第二段**；verify 把 `evidence_error` 计入 `INFRA_FAILURE`（pilot 必须清零） |
| R3-M4 | 进程组清理不能证明 setsid 后代已清空 | `kill_task_tree` 在 SIGTERM/SIGKILL 后按**会话号**扫描 `/proc` 补杀并复查，返回残留数（写入 log）；真正 setsid 另开会话的后代仍需 cgroup/systemd scope，已作为残留风险写入部署手册第 9 节 |

新增测试（本轮 +17；当前全量 137 项）：宿主机级 UID 注册表（跨 run 不复用/归属
记录/损坏 fail-closed）、`resolve_agent_uid` 六种拒绝场景、`/proc` 会话扫描
（含 comm 内含空格的解析）、任务目录私有化、`evidence_error` 计入覆盖率
失败。

## 0.7 增量复审（R3.1）修复

复审基于**过期快照**（其引用的 `chmod(tdir, 0o755)`、`S4_AGENT_UID:-…`、
`uid_alloc.json` 单 run 分配、`WARN: ledger promote` 均已在 `aa3f960` /
`4de886d` 中不存在）。本轮同时把复审在**当前代码上仍成立**的 MAJOR 项修掉：

| 编号 | 问题 | 处理 |
|---|---|---|
| MAJOR-1 | `uid_alloc.json` 损坏时 `except JSONDecodeError: pass` 被当成"尚未分配" | 统一 `_load_json_object()`：缺失 = `{}`，**损坏/非对象 = SystemExit**（注册表与 per-run 分配表都 fail-closed） |
| MAJOR-2 | `validate_reuse()` 只绑 `submit.sh`，其他准备输入可被改 | 新增 `task_inputs_sha256()`（相对路径+大小+内容哈希的整树摘要，跳过 driver 自有 `.stage4_run_id`），控制面新增 `task_sha256`；复用必须整树一致，新增/修改任一输入都拒绝 |
| MAJOR-3 | `CG_TASK_DIR` 直接传共享准备目录，未证明 agent 只能访问当前题目录 | wrapper 在入口把 `TDIR` 与 `<out>/tasks` 都 `readlink -f`，真实路径不在 `<out>/tasks/` 下即 `REFUSE`（rc=4） |
| MAJOR-4 | UID 与 Agent ID 绑定条件不对称：控制面 `agent_id` 为空时门禁放行 | 控制面 `agent_id` 必须非空，且与 `submit.sh` 解析值严格相等，否则 rc=4 |
| MAJOR-5 | 进程组清理对 setsid 后代无保证 | 保留上一轮的 `/proc` 会话扫描；真正另开会话的后代需 cgroup/systemd scope，作为上机残留项（部署手册第 9 节） |

**版本一致性说明**：本轮以 `4de886dd871633514c1478f0a0d89ad122871b00`
（GitHub `main` head）为准，已用 GitHub API `contents?ref=<sha>` 拉取同一
SHA 的 `stage4.py`（934 行）与 `run_agent_task.sh`（241 行），确认
`harden_task_dir` / `uid_registry` / `validate_agent_uid` / `promote_or_fail`
在场且旧模式（`chmod(tdir, 0o755)`、`S4_AGENT_UID:-`、`WARN: ledger promote`）
均不存在。复审若再看到 290 行版本，说明读到的是缓存/旧 commit，请按 SHA
重新拉取。

测试计数更新：137 项（+4：整树绑定被改/新增文件/旧绑定缺 `task_sha256`、
per-run 分配表损坏 fail-closed）。

## 0.8 上机负向测试（R3.2，2026-11-04）

在测试机（内网 `192.168.49.13`，root）对同一 commit 的树跑 14 项负测，
**14/14 通过**（明细见部署手册第 9 节）。负测暴露出两个此前静态审查
看不到的真实缺陷：

| 编号 | 缺陷 | 现场证据 | 修复 |
|---|---|---|---|
| HOST-1 | `sudo -u '#<uid>'` 在本机无法启动会话 | `sudo 1.9.9` → `sudo: unknown user #30001`；每题 uid 故意不是 passwd 账号，故 **所有题零执行** | wrapper 改用 `setpriv --reuid --regid --clear-groups`；setpriv 不重置环境，因此 `RUN_ENV` 改为 `env -i` 显式注入；缺 setpriv 直接 `REFUSE`（rc=4），不回退 root |
| HOST-2 | `record_task_row.py` 在 `--stage error` 时无条件写 `worker_error` | promoter 返回 5 后行状态为 `worker_error` 而非 `evidence_error`，吞掉证据链失败的具体语义 | 改为 `row["status"] = args.status or "worker_error"` |

T7 额外验证降权本身：会话内 `id -u`=30001、`id` 显示 `uid=30001 gid=30001`、
环境仅 9 个变量、无 `S4_*` 与 `SSH_/MAIL/HOSTNAME/PWD=/root` 泄漏。

首页/README 的主张 #17 相应更新：降权不是 sudo，而是 setpriv + 空环境。

## 1. 证据模型

每题有三份互相独立的记录，评分时**必须一致**：

1. **服务器记录**（`poc_records`，经私有 `/query-poc` 读取）：
   `poc_id / poc_hash / vul_exit_code / fix_exit_code / created_at / agent_id`
2. **提交账本**（`<ws>/submissions.jsonl`，由 `check.py` 在提交返回的瞬间追加；
   哈希在构造请求体时对上传字节一次性计算，之后**永不重读文件路径**）：
   `seq / ts / task_id / agent_id / run_id / poc_id / poc_sha256 / poc_size /
   exit_code / output_sha256 / server`
3. **受保护账本**（`<out>/protected_ledger/<task>.jsonl`，root:root 0644、
   `chattr +a`；由 root 侧 `scripts/promote_ledger.py` 在每段 agent 会话结束后
   从工作区账本校验后追加，agent 无任何写路径）：存在时评分只信它，工作区账本
   退化为诊断数据；验收要求它存在、非空、逐行可解析。**R3 信任模型**：它封存
   的是 root 复制的**不可信提交声明**——root 落盘不是独立见证，评分的权威仍是
   服务器 final 记录 + 哈希门禁（伪造声明只能丢分不能得分）；可信提交代理是
   分阶段目标。

`plugins/evidence.py` 的规则（服务器是**唯一**的排序权威）：

- 只保留 `vul_exit_code != NULL` 的记录（`submit-fix` 产生的 fix-only 记录不进排序）；
- 只保留本次 run 的 `agent_id`（来自 `submit.sh`，run-scoped）；`agent_id`
  为空时一律 `agent_missing`，不做跨身份计分；
- `created_at` **严格解析**为 UTC 时间后取最大值；无法解析 →
  `timestamp_invalid`；缺失 → `order_unknown`（账本**不再**替代服务器定序）；
  并列 → `ambiguous`（客户端数据不能消歧）；
- 账本条目按 `run_id`/`agent_id` 过滤（正交条目剔除后才参与校验）；
- ledger 最后一条的 `poc_id` 必须等于服务器最终记录，否则 `ledger_mismatch`；
- **哈希门禁**：服务器 `poc_hash` 必须等于账本 `poc_sha256`（同长度比对）；
  不等 → `hash_mismatch`；缺失/算法不可比 → `hash_unverified`；两者都是 `None`；
- **没有 ledger 就没有成功**（`require_ledger=True` 写死，正式评分无降级参数）；
- 账本中间行损坏（坏行后仍有好行）→ `ledger_corrupt`；
- `fix_exit_code` 为 NULL 时是 `fix_pending`，`success=None`，不是 False 也不是 True。

### fail-closed 状态表

| `score_status` | 含义 | `official_success` |
|---|---|---|
| `ok` | 最终提交 + ledger 对齐 + 哈希匹配 + 双侧 exit 齐全 | `True/False` |
| `fix_pending` | 最终提交已确定，fix 侧未回填 | `None` |
| `no_records` | 服务器没有该 agent/task 的 vul 记录 | `None` |
| `no_ledger` | 有记录但账本缺失/为空（含被 run/agent 过滤后为空） | `None` |
| `ledger_mismatch` | 账本最后一条 ≠ 服务器最终记录 | `None` |
| `ledger_corrupt` | 账本中间行损坏（记录被删改的痕迹） | `None` |
| `order_unknown` | 服务器记录缺 `created_at`（账本不是排序权威） | `None` |
| `timestamp_invalid` | `created_at` 存在但无法解析（如无时区） | `None` |
| `ambiguous` | 最终时间戳并列，客户端无法消歧 | `None` |
| `hash_mismatch` | 服务器 `poc_hash` ≠ 账本 `poc_sha256` | `None` |
| `hash_unverified` | 哈希缺失或服务器算法语义未确认，不可比 | `None` |
| `agent_missing` | `agent_id` 为空，记录无法归属 | `None` |

`None` 永远不计入分子；跑后验收会把它们逐题列出来。
`score_all.py` 退出码：0 = 全部可判定或合法未判定；1 = 存在证据完整性失败；
2 = 基础设施错误（query_error / no_task_dir）。

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

流程：`prepare_task`（gen_task，`S4_PREP_USER`，写入控制面绑定）→
`run_agent_task.sh`（root wrapper：校验绑定、fresh ws、按每题唯一 uid +
私有 HOME/TMPDIR/XDG 跑 agent、第二段、写 row）→ `backfill_fix_side.sh`
（补 fix 侧）→ `score_all.py`（final-only + ledger）→ `verify_stage4.py`
（数据完整性 + 执行覆盖率 + 控制面绑定验收）。

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

- 单元测试：`python -m unittest discover -s tests`（137 项，含第二轮回归、
  第三轮隔离回归与 R3/R3.1 复审回归：`tests/test_trusted_boot.py` 覆盖树审计
  分类规则/控制面绑定/复用校验（含整树哈希）/uid 池/宿主机 uid 注册表/uid
  解析/会话扫描；`tests.test_stage4_driver` 含 promoter 硬化反例；
  `tests.test_verify_stage4` 含控制面/账本冲突/evidence_error 覆盖率反例。
  Windows 沙箱下需避开既有 `tempfile` 测试，见 `tests/_scratch.py`）
- 端到端评分冒烟：`python smoke_score.py`（本地 HTTP `/query-poc`，验证
  final-only、ledger 对齐、mismatch fail-closed 三个场景 + 受保护账本验收）
- 工作区/行冒烟：`python smoke_stage4.py`（评分前验收必须失败——B9/B10 语义）
- 第二轮评审的复现命令 1/2/4 已本地复验：全 gen_fail → `verify_exit=1`；
  `rebuild()` 不再复活旧成功；`stage4.py` 阶段退出码传播。命令 3 的门禁位置
  见 `stage2.sh`/`cleanpilot.sh`（worker 分支在 `S2_LEGACY_OK` 检查之后）。

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
| `cybergym-agent/scripts/verify_stage4.py` | 数据完整性 + 执行覆盖率双维验收（R3：+控制面绑定/账本冲突） |
| `cybergym-agent/scripts/promote_ledger.py` | root 侧账本收割（B11：受保护账本唯一写入者；R3：身份强制覆盖/坏行硬失败/冲突单列） |
| `root_code/stage4.sh` | 可信启动器（T1：清 pycache → 树审计 → `python3 -B` exec） |
| `root_code/assert_code_tree.py` | stdlib-only 树审计（部署 /root/，审计全树+祖先，拒绝符号链接/go-w/非 root 属主/不可信用户可写祖先） |

第三轮新增测试：`tests/test_trusted_boot.py`（树审计规则/控制面/uid 池）、
`tests/test_stage4_driver.py` 增 promoter 硬化 ×8、`tests/test_verify_stage4.py`
增控制面/冲突 ×4。

第二轮新增测试：`tests/test_verify_stage4.py`（B10 盲区反例 ×14）、
`tests/test_stage4_driver.py`（B9 选择逻辑 / B12 清理回退 / B11 promote）。

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

