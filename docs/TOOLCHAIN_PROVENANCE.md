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

## 6. 入库哈希清单

见 `cybergym-agent/scripts/SHA256SUMS.tools`（随本提交生成）。
