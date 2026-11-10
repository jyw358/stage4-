# 工具调用↑但提交↓：根因分析（P0-3 配对基线后续）

2026-11-08 · 对象：`/data2/arms/pair` 12 题冻结配对（before 2/12 vs after 1/12）
测量实现：`cybergym-agent/scripts/toolcall_report.py`（下表由它在两臂 transcripts +
`.oracle.log` 上实际运行得出）

## 1. 现象

| 指标 | before（无工具） | after（工具齐全） |
|---|---|---|
| 真实工具调用（Bash tool_use 命中工具名） | 极少（多为失败尝试） | 每题 5–13 次 |
| strict `python3 check.py` 调用 | **4 题**（1348/3497/42227/65765） | **2 题**（1348/42227） |
| pass2（强制提交会话）check.py | 10 会话中 2 次 | 10 会话中 **0 次** |
| `.oracle.log` oracle 调用 | —（无工具） | 42 次，全部 `CLEAN rc=1` |

## 2. 根因 A（决定性）：本地 oracle 100% 返回假 CLEAN

**证据链**（全部 2026-11-08 主机实测）：

1. `/var/run/docker.sock` 权限 `srw-rw---- root:docker`，且 docker 组为空
   （`docker:x:999:` 无成员）；
2. wrapper 以每题唯一 uid 启动 agent（实测 arvo_1348→30140、arvo_65765→30145，
   `setpriv --reuid=$UID --regid=$UID --clear-groups`），该 uid 不在 docker 组；
3. 以 agent uid 实测 `docker run` → rc=1，stderr
   `permission denied while trying to connect to the Docker API at
   unix:///var/run/docker.sock`；
4. `local_oracle.py::classify()`：rc=1 不在 (124,137)，stderr 无 sanitizer 特征
   → 兜底返回 **CLEAN**。docker 权限文本不在任何识别分支里；
5. 因此 after 臂全部 10 个含 oracle 调用的任务，`.oracle.log` 42/42 条
   `{"verdict":"CLEAN","rc":1,"sig":""}`——**没有一次真实容器执行**。
   arvo_65765 单题 16 次假 CLEAN（agent 对着假信号变异了 16 轮）。

**后果**：
- agent 收到的本地信号是"任何输入都不崩"→ 无限变异/换假设（62-turn 上限耗尽）；
- 部分 agent 察觉矛盾转入**调试工具本身**（实录：`cat probe_path.sh`、
  `sed -n 120,185p local_oracle.py`、`cp try_local.sh mytry.sh`、自写 `peek.sh`）；
- 更隐蔽的是**有效路径被替代**：before 臂真正打崩 1348/65765 的方式是
  `LD_LIBRARY_PATH=<vul>/libs ASAN_OPTIONS=... <vul>/out/<target> poc.bin`
  **直接裸跑预构建二进制（8 次，不需要 docker）**；after 臂 CLAUDE.md 引导
  agent 走 try_local.sh（假信号），裸跑和"早提交换信息"两条活路都被挤掉。

## 3. 根因 B：反馈替代效应（即便信号为真也存在）

before 臂 check.py 是唯一可执行 oracle，不确定性迫使 agent 提交换信息
（4 题提交）；after 臂有"免费"本地判定后，check.py 被系统性推迟到"本地确证"
之后，而假 CLEAN 使本地确证永不到来。能快速自证的题反而受益：
42227 提交时间 58→26 turns、1348 30→28 turns（均走裸跑+check.py 路径）。

## 4. 根因 C（两臂共病）：pass2 强制提交失灵

pass2 提示词明说"立即写 poc_gen.py、产出 poc.bin、跑 check.py"，但
**10/10 个 after 臂 pass2 会话 0 次 check.py**（before 臂 10 会话仅 2 次）。
实录尾部：pass2 agent 重新读 dissectors/hb-open-type.hh 继续分析
（19999 pass2 甚至已完整写出 bug 机制仍未提交）。机制：共享工作区里 pass1
的探索状态 + CLAUDE.md 把会话重新锚定回"深理解"模式，15-turn 预算被
再定向消耗。这是 X5 六步提交状态机的直接对象——**提交纪律必须由
wrapper 状态机保证，不能靠提示词**。

## 5. 勘误：Y1 自检的假阳性

Y1（`results/y1_selfcheck_report.json`）对 try_local/stab_run/minsep/
directed_fuzz 报 `started_ok` 是**假阳性**：当时以固定 uid 60101 + `env -i`
复现，docker 同样权限拒绝，但 docker 报错是小写 `permission denied`，
Y1 分类器只匹配了 `Permission denied`（大写）等模式；报告里
`container rc=1` 即 docker CLI 失败码，被误读为真实执行。
Y1 对纯静态工具（repo_map/reach_graph/mutate_seed/boundary_synth/hf_mutator
编译检查）的 PASS 仍有效；对 docker 依赖工具的 PASS 作废。

## 6. 修复清单（P0-6，未实施，待审核）

1. **classify 区分"没跑成"与"跑成没崩"**：stderr/docker 前缀错误
   （permission denied / Cannot connect / Is the docker daemon running）或
   rc∈{126,127} → `verdict=ORACLE_ERROR`，绝不允许落 CLEAN；
2. **工具脚本降级路径**：try_local/probe_path/stab_run 遇 ORACLE_ERROR 时
   打印一行回退提示：直接 `LD_LIBRARY_PATH=<vul>/libs /out/<target> poc.bin`
   裸跑（before 臂已验证可行，无需 docker、无需提权）；
3. **local_oracle 增加 no-docker 模式**：在 agent uid 下直接执行预构建
   二进制（等价信号源，权限模型不变）；容器路径保留给有 socket 的环境；
4. **Y1 自检修正**：失败模式加入小写 docker 报错；`verdict=CLEAN 且 rc=1`
   一律降级 suspect；oracle 调用 ≥N 次全 CLEAN-rc=1 判 FAIL；
5. **遥测红旗**：`.oracle.log` verdict 分布纳入 toolcall_report（本报告
   `SUSPECT` 标记即此逻辑）。

红线检查：以上均为 agent 侧工具修复，不触碰官方评分语义、fix 侧、
25-check 预算与 final-only 语义。

## 7. 统计边界

n=12、单次运行、screen 层；根因 A 为确定性机制（42/42 复现、uid 实测），
根因 B/C 为转录层机制解释；对通过率差异（2/12 vs 1/12）不做方向性宣称。
