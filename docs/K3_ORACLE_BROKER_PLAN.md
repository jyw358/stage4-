# K3 root-side Oracle Broker：实施与 k8 非评分验收方案

状态：**B1–B6 实现及 k8 非评分验收已通过；K3 生产启用与 K1 正式 A/B 仍 HOLD**。

本轮只修复 `local_oracle.run_side()` 覆盖的漏洞版本地验证，不扩大 Agent 的宿主权限，不触碰 k7、H1、H2、X5、评分链、提交账本和历史成功输入。

## 1. 架构与不可妥协边界

```text
每任务独立 Agent UID / 原任务 cgroup             root broker（k8 独立实例）
try_local / stab_run / minsep
        │ local_oracle.run_side()                  每请求重新授权
        └────────── Unix socket JSON line ───────▶ SO_PEERCRED
                                                    + 唯一活动 control
             schema-2 + request_id                  + run/task/workspace
        ◀────────── 真实执行证明 ───────────────── + attempt/peer cgroup
                                                    + root 私有输入副本
                                                    + 固定 vul-side Docker
Agent：无 Docker socket 权限                        + 精确清理与 root 审计
```

- Agent 不加入 `docker` 组，不改变 `/var/run/docker.sock` 权限，不开放 TCP 服务。
- 客户端传入的 `info` 不决定镜像、挂载、目标或任务。broker 从已授权 control 的 task ID 重新执行 `side_info(task_id, "vul")`。
- 不提供 fix 侧、答案键、评分记录或历史 PoC 通道；未知请求字段（包括 `side=fix`）直接拒绝。
- broker 不可用时没有 Agent 侧 direct-docker 回退。`CG_ORACLE_MODE=direct` 仅对真实 euid 0 的诊断进程有效。

## 2. P0-A：每请求任务身份与 cgroup 授权

UDS 通过 `SO_PEERCRED` 取得 `(pid, uid, gid)`。每个 `run_vul_candidate` 请求都重新扫描 root 所有的 run/control 平面，不接受只在连接建立时做一次的静态 `uid → task` 查表。

授权必须同时满足：

1. peer UID 在所有配置 run root 中只对应一个活动任务；零匹配、重复匹配和 control 读取失败均拒绝。
2. control 目录及文件为 root 所有、非符号链接、非 group/other writable。
3. control 的 `run_id` 与 run 根下 `run_id` 一致；请求 `task_id` 与绑定目录一致。
4. `active=1`，`agent_uid` 在读取过程中保持一致，`workspace` 为绝对路径。
5. `/proc/<attempt_pid>/cgroup` 与 `/proc/<peer_pid>/cgroup` 都是单一 cgroup-v2 记录，且精确等于 control 的 cgroup。
6. 任务结束后 driver 写入 `active=0`，旧 UID 随即失去服务，不依赖 UID 永不再用。

拒绝以 `verdict=ORACLE_ERROR`、`runner_error_kind=broker_refused` 和独立 `refusal_reason` 返回；拒绝不是 CLEAN，也不启动容器。

## 3. P0-B：root 输入复制与 TOCTOU 防护

- 输入是最多 8 个 workspace 相对路径；拒绝绝对路径、反斜杠、空分量、`.`、`..`、NUL。
- root 先以 `O_DIRECTORY|O_NOFOLLOW` 打开 workspace，并验证其实际 owner/mode 为绑定 Agent UID/0700。
- 中间目录逐级使用相对已打开目录 FD 的 `openat` 语义，并对每一级使用 `O_DIRECTORY|O_NOFOLLOW`；最终文件使用 `O_NOFOLLOW`。
- 验证和读取使用同一个源 FD；`fstat` 必须证明普通文件。单文件超过 1 MiB 或总计超过 4 MiB 拒绝，实际读取期间再次执行增长限制。
- 输出只写入 root 私有 0700 临时目录，以 `O_CREAT|O_EXCL|O_NOFOLLOW` 创建。
- SHA256 对复制过程中实际写入 root 私有副本的字节计算；容器只挂载该副本，不挂载 Agent workspace。
- 任一失败删除本次部分副本并返回 `INPUT_ERROR/ORACLE_ERROR`，不会将路径检查前看到的内容误认为实际执行内容。

## 4. 固定 UDS 协议与执行证明

默认 socket：`/run/cybergym-oracle/broker.sock`。传输是每个连接恰好一行 JSON，不是 HTTP。`{"op":"version"}` 只返回版本；执行请求为：

```json
{"op":"run_vul_candidate","task_id":"oss-fuzz:900000001",
 "inputs":["clean.bin"],"timeout":10,"perturb":null,"tag":"try_local_vul"}
```

| 字段 | 服务端约束 |
|---|---|
| `op` | 仅 `run_vul_candidate`；版本查询仅 `version` |
| `task_id` | 仅 `arvo:N` 或 `oss-fuzz:N`，且必须匹配活动绑定 |
| `inputs` | 0–8 个安全相对路径；零可读文件不是 CLEAN |
| `timeout` | 整数 1–30，默认 10；不静默 clamp |
| `perturb` | `null` 或整数 0–255；布尔值不作为整数接受 |
| `tag` | 缺省为空；提供时匹配 `[A-Za-z0-9_.-]{1,40}` |

响应保持 `.oracle.log` schema 2，增加 `request_id`、`broker_version`、`input_sha256`、run/task/container 等审计字段。客户端严格检查 schema、request ID、hash 数量和执行证明；`CLEAN/FATAL/NONFATAL/TIMEOUT/UNKNOWN` 必须有真实 `runner_started`、`target_started`、runner/target exit 证据。

broker 固定生成容器参数：`--rm`、唯一名称、`--network none`、`--cap-drop ALL`、`no-new-privileges`、`pids-limit 256`，挂载仅为 root 解析的 `vul/out`、`vul/libs` 和 root 私有输入目录。容器标签包含 broker instance、run ID、task ID、request ID。

broker 复用现有 stdout 哨兵、`parse_execution`、`classify_exec`、`runner_error_kind` 和 sanitizer signature。分类保持原语义：裸 SIGKILL/137 且无 libFuzzer timeout 证据是 `UNKNOWN(killed)`，不得伪称 TIMEOUT；没有可信执行证明永远不是 CLEAN。

每次请求在 root 审计日志写入 request/result 两条记录。request 审计失败则不执行；result 审计失败则不向 Agent 返回 CLEAN/FATAL 等未经审计的结论。Agent `.oracle.log` 与 root 审计通过 `request_id` 一对一交叉核对。

## 5. P0-C：清理作用域与故障关闭

broker **不调用**旧的 `sweep_preflight("cybergym.local_oracle=1")`，也不按容器年龄判断孤儿。

每次授权后的启动预检只处理：

- 标签 `cybergym.oracle_broker=1`；
- 且 broker instance 等于当前实例；
- 且不在 root 侧活动执行注册表中的容器。

清理前再次 inspect 名称和完整归属标签，并在 kill 前复查活动注册表。当前请求超时或异常时，只按本次唯一容器名调用现有 `_kill_and_verify` / `_post_abnormal_cleanup`。活动任务 B 的容器不会因任务 A 的请求、超时或客户端退出而被 sweep。

`docker ps/inspect` 失败、标签不完整或归属无法证明时返回 `inspection_incomplete`。启动预检中任何非 `cleanup_verified` 项都产生 `ORACLE_ERROR + runner_error_kind=sweep_preflight_failed`，不复制输入、不调用 `docker run`，更不执行跨任务 kill。全局孤儿维护不属于请求路径，不能证明失去活动归属时同样不得清理。

## 6. Docker 调用点清单与首轮能力范围

| 调用路径 | 本轮裁决 |
|---|---|
| `try_local.sh → local_oracle.run_side()` | 经 root broker |
| `stab_run.sh → local_oracle.run_side()` | 经 root broker |
| `minsep.py → local_oracle.run_side()` | 经 root broker |
| `directed_fuzz.py` artifact 分类中的 `run_side()` | 经 root broker |
| `directed_fuzz.py` 自行启动 focus probe / fuzz 容器 | **未迁移，Agent 下继续 Docker permission fail-closed** |
| `probe_path.sh` 自行启动 gdb 容器 | **未迁移，Agent 下继续 Docker permission fail-closed** |

因此不能宣称“K3 全部工具链均已修复”。首轮验收只覆盖 `run_side()` 的本地 oracle 能力；不得通过赋予 Agent Docker 权限补救其余路径。

## 7. root 代码完整性与 k8 systemd

`root_code/cybergym-oracle-broker.service` 部署到 k8，关键配置为：

- `User=root`，`RuntimeDirectory=cybergym-oracle`（0711），`StateDirectory=cybergym-oracle`（0700），socket 生命周期由 systemd 管理。
- `ExecStartPre` 从导入树外的 `root_code/assert_code_tree.py` 审计完整 agent 代码树及祖先：拒绝 symlink、非 root owner、group/other write 和 `__pycache__`。
- `ExecStart=/usr/bin/python3 -I -B .../scripts/oracle_broker.py`：隔离 `PYTHONPATH`/user-site，禁止 root 写字节码。
- run root 仅指向 `/data2/arms/k8/out`；broker instance 固定为 `k8`；审计写入 root 私有 state 目录。

服务可以在非评分 k8 运行，但这不等于 K3 生产启用。

## 8. 五项非评分验收及新增边界断言

使用专门构建的 oss-fuzz 数字 ID fixture 和固定 `clean.bin`、`fatal.bin`、`hang.bin`。不使用、不复制任何历史正式任务成功 PoC。

1. **真实 CLEAN**：`CLEAN`、runner/target started、runner/target rc=0、SHA256 匹配，request/result 审计与 Agent 日志一致。
2. **真实 FATAL/NONFATAL**：专用 ASAN 输入得到 `FATAL`（或兼容 NONFATAL）、非零 target exit、非空 sanitizer signature；不得从历史提交提取输入。
3. **故障注入不得假 CLEAN**：不存在 socket → `broker_unavailable`；不存在镜像 → `ORACLE_ERROR/image_unavailable`；真实 Docker 检查失败 → `sweep_preflight_failed`。均不得变成 CLEAN/NO_TRIGGER。
4. **越权全部拒绝**：未知 `side=fix`、路径逃逸、最终及中间 symlink、非普通文件、数量/大小超限、未注册 UID、task mismatch；另测 inactive/旧 UID、duplicate UID、wrong cgroup 和中间目录替换竞态。Agent UID 的 `docker ps` 必须仍 `permission denied`。
5. **清理与隔离**：HANG 按真实证据分类且无 broker 容器残留；kill -9 客户端后 broker 独立 deadline 仍完成清理；A 异常期间 B 的活动容器必须存活；三态清理不得把 unknown 记为成功。

root 完整性另行实测：agent-writable 代码树使 `ExecStartPre` 失败，恢复 root-clean 后服务才能启动；运行命令必须包含 `-I -B`。工具兼容性按第 6 节逐项记录，未迁移路径必须仍失败关闭。

正常用例单独统计 `execution_confirmed / validation_attempts`，要求至少 95%；故意注入的拒绝和基础设施故障单列，不用剔除正常失败来美化比例。五项全部通过前不冻结 K3 生产版本。

### k8 最终实测结果

- 通过批次：`k3acc-20261111T155956-f3b6ca0b`，61/61 项检查 PASS，无失败断言。
- 正常执行确认：20/20（100%）；Agent/root `request_id` 交叉核对 22 条，mismatch 为 0。
- CLEAN、ASAN FATAL、HANG/137/UNKNOWN、真实工具包装、三项故障注入、输入/授权边界、目录竞态、A/B 隔离、root 完整性和未迁移工具 fail-closed 均通过。
- 验收结束时 broker 容器残留为 0，服务保持 active；fixture manifest 明确记录 `pinned-prebuilt-u20` 及固定 SHA256。
- 机器证据：`results/k3_broker_acceptance/k3acc-20261111T155956-f3b6ca0b/`。全量本地回归为 345 项通过、8 项跳过。

该结果只允许记录候选实现，不自动冻结生产版本，也不启用正式 K3 或 K1 正式 A/B。

## 9. 实施文件与后续门禁

| 编号 | 实现 |
|---|---|
| B1 | `cybergym-agent/scripts/oracle_broker.py`：UDS、授权、输入、执行、注册表、审计 |
| B2 | `local_oracle.run_side()` 薄客户端，保持公开签名，无 Agent direct 回退 |
| B3 | 复用分类/精确清理；以 instance + registry 替代全局 sweep |
| B4 | `root_code/k3_acceptance.py` 与机器可读 k8 验收证据 |
| B5 | `node_telemetry.py` 兼容 schema 2 并区分执行拒绝/不可用/失败 |
| B6 | k8 systemd unit、独立 fixture、全量回归与验收；不接入正式队列 |

验收通过后只能冻结一个候选 broker 版本并记录 commit SHA。K1-Y6 Lite 可独立做非评分机制实验；K1 正式 A/B 必须等待 K3 冻结，两臂共用同一冻结 broker。任何后续 broker 行为变更都需要新版本，不能与旧批次混算。
