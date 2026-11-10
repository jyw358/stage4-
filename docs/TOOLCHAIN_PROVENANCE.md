# 工具链恢复溯源（PR-A，r9 P0-2，2026-11-08）

> 审计结论：formal50 工作区**未搭载任何工具**的直接原因是部署树
> `scripts/`（/opt/cybergym-agent-r33 与默认 /opt/cybergym-agent 两棵树）
> 均缺失全部工具文件，而 make_agent_ws.py 旧复制逻辑 `if _s.exists()`
> 静默跳过；`verify_gate.py` 在全主机任何位置都不存在（Stop 钩子从未
> 生效）。本文件记录恢复来源、逐件审计与改动。

## 1. 恢复来源（可审计链）

| 来源 | 性质 | 使用部分 |
|---|---|---|
| `/data/cybergym/stage3/r_engineer-ws/<task>/`（1,507 个 stage3 生产工作区） | 生产运行过的工具副本，**多数决**提取 | 10 件工具的规范版本 |
| `/home/naco/work/cybergym-agent/scripts/local_oracle.py`（sha256 `cd421ec6…`，2026-10-09） | 原始开发树（唯一来源；未随 ws 分发） | local_oracle.py |
| 新写 | 无历史来源 | verify_gate.py |

多数决结果（全量 sha256 扫描，来源 ws 总数 1,507）：

| 工具 | 变体分布 | 取用 |
|---|---|---|
| repo_map.py | 1,506/1,506 同一哈希 | `a41eb783…` |
| reach_graph.py | 1,507/1,507 | `ba0c3167…` |
| probe_path.sh | 1,507/1,507 | `7b2249ba…` |
| try_local.sh | 1,336 vs 171（两波次） | 多数 `e2e40690…`（较 /home/naco 原件 `b2740064…` 更新） |
| stab_run.sh | 1,507/1,507 | `301853fe…` |
| minsep.py | 1,336 vs 171 | 多数 `39b1cb5e…`（同理更新于 naco 原件） |
| mutate_seed.py | 1,507/1,507 | `de8b85b7…` |
| directed_fuzz.py | 1,506 vs 1 | `29394b61…` |
| boundary_synth.py | 1,507/1,507 | `0a4139bb…` |
| hf_mutator.py | 1,336（后期波次新增） | `d8df6a5f…` |

## 2. 逐件源码审计结论（P0-2）

- **repo_map / reach_graph / mutate_seed / boundary_synth / hf_mutator**：
  纯离线、确定性、无网络、无外部路径——原样入库。
- **local_oracle.py**：fix 侧在代码层已被"D4 strip"阻断（`side!='vul'`
  返回 None；`run_side` 拒绝 `/fix/` 路径；`gate_take` 恒 False）；docker
  只读挂载预构建 vul（`DATA_DIR=/data/cybergym/cybergym-server-data`）；
  无网络。**合规**。
- **minsep.py / directed_fuzz.py**：脚本相对导入 local_oracle（自包含）；
  minsep docstring 的"fix-side check"是 stage-1 陈旧描述，**代码只跑
  vul 侧**（FAQ 合规注释在 L97-100）。原样入库。
- **try_local.sh / stab_run.sh / probe_path.sh**：原版硬编码
  `/home/naco/work/...` 导入路径（R3 违例类）——**打补丁后入库**（见 §3）。

## 3. 恢复时施加的补丁（3 个文件，语义不变）

1. `try_local.sh` / `stab_run.sh`：heredoc Python 的
   `sys.path.insert(0, "/home/naco/...")` 改为 bash 侧解析
   `SCRIPT_DIR="$(dirname "$0")"` 传入 argv——local_oracle 随工作区分发
   后自包含。
2. `probe_path.sh`：删除 `plugins.fuzz_local`（部署树插件，仓库中亦
   不存在）导入，改用 `local_oracle.side_info`——`parse_arvo_script`/
   `DEFAULT_DATA_DIR` 与其逻辑逐字一致（同 `_TARGET_RE`/`_ASAN_RE`/
   同 DATA_DIR），且 side_info 额外支持 oss-fuzz 任务（原版只支持 arvo）。
3. `make_agent_ws.py`：REQUIRED_TOOLS 增加 `local_oracle.py`（工作区
   必须自带 oracle 库）。

## 4. 新增文件（无历史来源，新写）

- `verify_gate.py`：Stop 钩子（设计语义来自 make_agent_ws.py 原注释
  "a session may not end before its first check.py run"）。放行条件：
  `.checks>0` 或 `SOLUTION.md` 存在或 `S4_HOOK_RELAX=1`；否则 exit 2
  阻止结束并输出指引。5 项单测覆盖。

## 5. 运行环境前提（Y1 将逐项验证）

- 预构建 vul 覆盖：formal50 全部 45 arvo + 5 oss-fuzz 任务在
  `DATA_DIR` 均有 `vul/out`（已核验）。
- docker 镜像：`cybergym/oss-fuzz-base-runner:latest` 与
  `cybergym/gdb-runner:latest` 在主机在位（已核验）。

## 5a. P0-6 勘误与再补丁（2026-11-08，r10 审计后）

上述"docker 镜像在位"前提**不充分**：agent 每题唯一 uid 对
`/var/run/docker.sock`（0660 root:docker，docker 组为空）无访问权，
配对基线 42/42 次 oracle 调用实为 docker CLI 权限拒绝被旧
`classify()` 误报为 CLEAN（详见
`results/pair_baseline/toolcall_analysis.md`）。据此对恢复件施加第二批
补丁（语义修复，不再是字节一致恢复件）：

| 文件 | 原 hash（恢复件） | P0-6 变更 |
|---|---|---|
| local_oracle.py | `cd421ec6…` | `classify_exec` 状态优先 + 哨兵 + schema-2 结构化结果；删除未用 `import fcntl`；`RUNNER` 增加仅供诊断的 `ORACLE_RUNNER` env 覆盖 |
| try_local.sh / stab_run.sh | `e2e40690…` / `301853fe…` | 错误 verdict → VERIFICATION-UNAVAILABLE，exit 7 |
| probe_path.sh | `7b2249ba…` | gdb runner 加哨兵；runner 未启动 → ORACLE_ERROR，exit 5 |
| minsep.py | `39b1cb5e…` | r0 错误 verdict → VERIFICATION-UNAVAILABLE exit 7；still_crashes 遇错误 verdict 返回 None（不再当"未崩"） |
| directed_fuzz.py | `29394b61…` | fuzz 容器加哨兵；runner 未启动 → ORACLE_ERROR exit 7（"no artifacts"不再可能被误读） |

SHA256SUMS.tools 已更新为补丁后哈希；主机两棵 /opt 树按新清单校验
通过；Y1 v2（verdict=PASS，`results/y1v2_report.json`）验证错误分类
不再可能产生假 CLEAN。

> 勘误：本文件早期版本的前提"agent uid 需可访问 docker（Y1 验证）"
> 是错误的（见 §5a）——agent uid 无 socket 权限；正确语义是 oracle
> 必须**诚实报告**执行不可用。

## 5b. r14 外审再补丁（2026-11-09，对抗性外部评审后）

外审（用户委托的独立评审 AI）确认两条可复现假负反馈路径与多个
fail-closed 漏点，全部修复并滚动部署两棵 /opt 树（备份
`/data2/backup/pre-r14-20261109T034620Z/`，`sha256sum -c` 15/15 OK）：

| 文件 | r14 变更 |
|---|---|
| local_oracle.py | **A1**：哨兵改 stdout 专属解析（首行=runner 证明；退出标记取最后一个全行匹配；docker rc≠0 时解析结果作废→ORACLE_ERROR）——目标回显字面量无法再伪造 CLEAN/FATAL；**A2**：124=TIMEOUT、137 无超时证据=UNKNOWN(killed)、libFuzzer timeout/OOM 文本分家、FATAL 需 sanitizer 文本∧非零退出；**B1**：>8 输入显式 INPUT_ERROR（不再静默截断）、open+fstat 拒非普通文件、bytes 读取（二进制输出不再炸 oracle）、docker CLI 缺失→ORACLE_ERROR(docker_missing)；**B2**：容器 --name 唯一化+外层超时 docker kill、阶段耗时入记录；**F1**：fix 侧守卫加 realpath 分量检查；**D2**：日志 O_APPEND 单次完整写（UTF-8）；NO_INPUT/INPUT_ERROR/FIX_SIDE_BLOCKED 也入 .oracle.log；删除 legacy `classify()` shim |
| try_local.sh / stab_run.sh | **C1**：默认分支 fail-closed（NO_INPUT/INPUT_ERROR/FIX_SIDE_BLOCKED/未来枚举→VERIFICATION-UNAVAILABLE exit 7）；stab_run 只聚合已验证 run，0 验证→exit 7；CLEAN 是唯一 NO_TRIGGER 出口 |
| minsep.py | **C2**：中途 oracle 失败≠预算用尽（-3/-1 分离）；中途失败不再发布候选（exit 7）；基线 TIMEOUT 独立状态（exit 6）不再误判 NOT-CRASHING；私有 mkdtemp 替换可预测 /tmp 文件名 |
| probe_path.sh | **C3**：容器 rc≠0 / gdb 断点错误 → VERIFICATION-UNAVAILABLE exit 5；NOT-REACHED 仅在会话正常完成时输出；try/finally 清理 |
| directed_fuzz.py | **C4**：修复 -dict 参数前插导致 rc=127 的真 bug（字典参数移到目标之后）；rc 126/127→TARGET_LAUNCH_ERROR exit 7；artifact 按本轮 mtime 过滤（不再把旧轮产物当本轮样本）；rc≠0 无 artifact 不再输出"keep iterating" |
| toolcall_report.py | **D1**：requests/attempts/confirmed 三层分离（TARGET_LAUNCH_ERROR 不再计入 confirmed；旧 schema FATAL/NONFATAL 记 unverified 不记 confirmed；CLEAN-rc1 降级为 suspect）；**E1**：错误后事件与总量同单位并内置断言；坏行计数 |
| pass2_triage.py | **A1 同源修复**：_run_once 复用 parse_execution/classify_exec 新语义 |
| tests | test_local_oracle_classify 重写：25 项（含哨兵伪造 4 场景、假 FATAL 退出 0、137 语义、realpath 守卫 mock） |

主机复算（新口径）：after 臂错误后事件 **702/1296（54%）**、B 臂
**270/1127（24%）**——"702>674"矛盾系单位混用，已在
`results/pair_baseline/summary.md` 勘误。

## 6. 入库哈希清单

见 `cybergym-agent/scripts/SHA256SUMS.tools`（随本提交生成）。
