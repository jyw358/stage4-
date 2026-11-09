# r16 动态安全验收报告（local_oracle 故障注入 + directed_fuzz 五路径生命周期）

时间：2026-11-09T07:11Z；主机 nacocom（root，非评分，无官方 grader 接触）；
验证对象：两棵 /opt 树部署版本（SHA256SUMS.tools 15/15 双树通过）。

**BATTERY_PASS 10/10**；原始数据 `battery.json`（本目录副本）+
`/data2/r16_battery/battery.json`。

## local_oracle 故障注入（F1–F5）

| 路径 | 注入方式 | 结果 | 判定 |
|---|---|---|---|
| F1 正常退出 | 真实容器执行（arvo:1348 vul） | verdict=CLEAN | ✅ |
| F2 外层超时 | mock 包装：先起**真实** sleep-120 容器再抛 TimeoutExpired | ORACLE_ERROR/runner_hang + **cleanup_verified**（真实 kill+验证）+ 残留 0 | ✅ |
| F3 daemon 不可达 | DOCKER_HOST=tcp://127.0.0.1:1 | ORACLE_ERROR/daemon_unreachable | ✅ |
| F4 检查命令失败 | mock `_container_gone→None` | **inspection_incomplete**（绝不冒充成功） | ✅ |
| F5 kill 失败 | mock `_kill_and_verify→cleanup_failed` | cleanup_failed 如实传播 | ✅ |

## directed_fuzz 生命周期（L1–L5，X3′(b) 五场景）

| 路径 | 方式 | 结果 | 判定 |
|---|---|---|---|
| L1 正常退出 | 真实 30s fuzz | rc=0，容器自清，残留 0 | ✅ |
| L2 超时 | 注入 TimeoutExpired + 真实 sleep 容器 | 走自身 fail-closed 分支：RESULT: ORACLE_ERROR、rc=7、`_kill_and_verify` 真实 kill，残留 0 | ✅ |
| L3 中断 | 运行中 SIGINT | BaseException 守卫触发验证清理（输出含 cleanup），残留 0 | ✅ |
| L4 worker 崩溃 | SIGKILL（不可捕获） | 孤儿容器如期出现（1 个）→ `sweep_label` 收敛：cleanup_verified，残留 0 | ✅ |
| L5 强制 kill | 外部 docker kill 容器 | df 以 rc=7 fail-closed 退出，残留 0 | ✅ |

## 残留与部署核查

- 终态残留：`docker ps -aq` 按 `cybergym.local_oracle=1` /
  `cybergym.directed_fuzz=1` 标签计数 = **0**；
- 两棵 /opt 树 `sha256sum -c SHA256SUMS.tools` = **15/15 / 15/15**；
- 部署哈希：local_oracle `c965e2770bf0…`、directed_fuzz `1beb7057…`，
  minsep `ca792ee3…`（与本报告同 commit 源）。

## 验收驱动的两个真实修复（本轮发现并已修）

1. `_container_gone` 原用 `--filter name=`，**按容器 ID 查验永不匹配**
   → kill-by-ID 验证为假阳性（L4 首跑暴露）；已改为 ID/名字双匹配
   （新增 `TestContainerGoneMatching` 回归）。
2. SIGKILL 不可捕获 → 新增标签化 `sweep_label`（年龄过滤 ≥15 分钟，
   不误杀并发兄弟容器）+ run_side/directed_fuzz 的 BaseException 守卫；
   收敛契约：崩溃遗留 ≤15 分钟内被下一次工具调用清理（L4 实证）。

## 边界声明

- 本验收为非评分动态回归；正式评分跑批仍 HOLD，待批；
- SIGKILL 后 15 分钟窗口内的孤儿容器是可接受暂态（已标签化、可审计、
  必然收敛），已在报告与本-commit 注释中明示；
- 95% 成功率仍为研究目标，本验收仅为必要条件。
