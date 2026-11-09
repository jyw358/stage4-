# r17 动态安全验收报告（v2，含 r16 复审整改）

时间：2026-11-09T08:15Z→08:33Z；主机 nacocom（root，非评分）；
验证对象：两棵 /opt 树（SHA256SUMS.tools 15/15 双树，本节末哈希）。

**BATTERY_V2_PASS 12/12**；原始数据 `battery_v2.json` + harness 脚本
`r17_battery.py`（均已入仓，可审计调用参数）。

## r17 P0 追溯：r16 报告 L4 的"15 分钟门 vs 10 秒孤儿"矛盾

结论：**测试 harness 显式传参 `min_age_min=0` 的机制性验证**，
与默认 15 分钟门无代码矛盾；缺陷在于 r16 未披露该参数且报告措辞
（"15 分钟契约 L4 实证"）过度声明。本节以默认参数实测补齐（见 L4b），
并把 harness 入仓使所有调用参数可审计。

## 12 项结果

local_oracle：F1 正常退出 CLEAN ✅；F2 外层超时（真实容器+真实
kill 验证）cleanup_verified/残留 0 ✅；F3 daemon 不可达
ORACLE_ERROR ✅；F4 检查失败 inspection_incomplete ✅；F5 kill 失败
cleanup_failed 传播 ✅。

directed_fuzz：L1 正常 rc=0/残留 0 ✅；L2 注入超时走自身 fail-closed
rc=7+真实验证 kill ✅；L3 SIGINT 守卫清理（rc=-2，输出含 cleanup）✅；
**L4b 默认 15 分钟收敛（r17 核心要求）** ✅；L4m 机制验证
（**显式 OVERRIDE min_age_min=0**，已标注）✅；L5 外部强杀 rc=7 ✅；
L6 focus-probe SIGKILL 遗留**带标签**孤儿（dfzp-*）被同契约收敛 ✅。

## L4b 证据链（默认参数，无任何覆盖）

- 孤儿：`dfz-r17-orphan-proof`，ID `225c8f99d3a7`，标签
  `cybergym.directed_fuzz=1`，创建于 **2026-11-09T08:15:03Z**；
- 等待 15.5 分钟后实测 `docker ps` 年龄：**"15 minutes ago"**；
- 触发：真实短 df 运行（rc=0），其启动清扫以**默认 15.0** 门槛执行；
- `.oracle.log` sweep 行（08:30:36Z）：`{"id":"225c8f99d3a7","age":
  "15 minutes ago","cleanup":"cleanup_verified"}`；
- 同批**年轻兄弟容器存活**（young_survived=true）——默认门不误杀
  活跃兄弟的直接证据；清扫后标签残留=1（仅年轻兄弟，随后按测试
  计划移除）。

## 收敛契约（修正后的准确表述）

- SIGINT/超时/异常退出：**当场**验证清理（L2/L3）；
- SIGKILL（不可捕获）：遗留**带标签**孤儿，由**同工具的**下一次
  调用按默认 ≥15 分钟年龄门收敛（L4b 实证）；run_side 与 df 各扫
  自己的标签（本次 harness 触发器错误即因混用标签，已修正入档）；
- 年龄未知/扫描失败：`inspection_incomplete`，绝不清理（三项 P1
  回归：未知格式/空值/ps 返回码≠0）。

## 本轮代码修复（r17 三项 P1）

1. `_age_minutes` 未知/空文本 → `None`（绝不当作 999 分钟旧容器）；
2. `sweep_label` 检查 `docker ps` 返回码≠0 → `inspection_incomplete`
   （失败扫描绝不伪装成"无孤儿"空列表）；
3. focus-probe 容器纳入统一生命周期（唯一名 dfzp-*、工具标签、
   超时/中断验证清理——L6 实证）；df 清扫结果写入 .oracle.log。

测试：oracle 单测 51/51、全套件 OK（8 skipped）。

## 部署哈希（双树一致、15/15）

```
c5528cdb15a458f3193f81dcff13705dae99916da0e388533f80caf9d3634183  local_oracle.py
c66cf963e347481e1c9f152a7ed2ac13aae76fdff67e83fab55d8fe146ed1341  directed_fuzz.py
```

## 边界声明

非评分动态回归；正式评分跑批维持 HOLD；孤儿 ≤15 分钟暂态已标签
可审计；95% 目标仍需独立冻结队列真实评分证明。

---

# r16 动态安全验收报告（已被上文 v2 取代，保留存档）

时间：2026-11-09T07:11Z；主机 nacocom（root，非评分，无官方 grader 接触）；
验证对象：两棵 /opt 树部署版本（SHA256SUMS.tools 15/15 双树通过）。

**BATTERY_PASS 10/10**；原始数据 `battery.json`（本目录副本）+
`/data2/r16_battery/battery.json`。
**勘误（r17）**：其中 L4 为显式 `min_age_min=0` 的机制验证，
不构成默认 15 分钟收敛的证明；默认门证明见上文 L4b。

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
