# P0-3 冻结配对基线（12 题 screen 集）

运行时间：2026-11-08T06:58Z → 08:41Z（两臂顺序执行，各 12 题，3 workers）
主机：nacocom；任务目录由 gen_task 重新生成（未复用 formal50 的 control 绑定）

## 实验设计

| 项 | before 臂 | after 臂 |
|---|---|---|
| AG 树 | `/data2/arms/pair/before-agent`（复制自 /opt/cybergym-agent-r33，scripts 替换为部署前备份） | `/data2/arms/pair/after-agent`（复制自 /opt/cybergym-agent-r33，scripts 为 PR-A 新版本） |
| scripts 差异 | 无工具、旧 make_agent_ws（`aa0dd5fa…`，静默跳过缺失工具）、无 verify_gate.py | 13 工具 + 新 make_agent_ws（`1519739f…`，fail-closed）+ verify_gate.py |
| wrapper | `run_agent_task.sh` sha256 `be8f9b8a…`（两者相同，含 keypool） | 同左 |
| 队列 | `/data2/arms/pair/queue12.json` sha256 `572c7163…`（两臂一致） | 同左 |
| 模型/密钥 | keypool active=senary（两臂日志均 `[keypool] active=senary selection=healthy:senary`） | 同左 |
| 出目录 | `/data2/arms/pair/before-run2` | `/data2/arms/pair/after-run2` |

12 题来自 formal50 失败池 + 1 个成功对照（screen 层，非盲测）：
arvo:1348(control) 11033 19999 20131 3497 53631 59931 65765(no_check_attempt)、
arvo:13725 20083(vul_no_crash)、arvo:42227(fix_not_clean)、oss-fuzz:42537670(no_check_attempt)。

## 结果（官方判定 final-only）

| 指标 | before | after |
|---|---|---|
| official_success | **2/12 (16.7%)** | **1/12 (8.3%)** |
| 有提交任务数 | 4 | 2 |
| 有 check.py 任务数 | 4 | 2 |
| 总 turns | 702 | 674 |
| 总成本 | $30.57 | $25.78 |
| execution_failures | 0 | 0 |
| verify 违规 | 0 | 0 |

逐题：

| 任务 | before official / status / turns | after official / status / turns |
|---|---|---|
| arvo:1348 | True / submitted / 30 | True / submitted / 28 |
| arvo:65765 | True / submitted / 56 | None / no_submission / 62 |
| arvo:3497 | False / submitted / 62 | None / no_submission / 62 |
| arvo:42227 | False / submitted / 58 | False / submitted / 26 |
| 其余 8 题 | None / no_submission / 62 | None / no_submission / 62 |

## 工具使用转化（transcript 关键字计数，两臂同口径）

| 工具 | before 引用数 | after 引用数 |
|---|---|---|
| try_local.sh | 285 | 557 |
| probe_path.sh | 97 | 209 |
| local_oracle.py | 14 | 157 |
| reach_graph.py | 111 | 151 |
| stab_run.sh | 97 | 153 |
| minsep.py | 90 | 156 |
| directed_fuzz.py | 80 | 159 |
| boundary_synth.py | 95 | 222 |
| mutate_seed.py | 95 | 200 |
| hf_mutator.py | 22 | 92 |
| check.py | 890 | 831 |

before 臂的计数包含 agent 自行创建的替代脚本（历史证据：formal50 行自述
"missing try_local.sh"），after 臂是真实随工作区下发的工具；after 臂工具
可用且被实际调用（工作区 12/12 含全部工具）。

## 结论

1. **工具可用性本身没有带来分数提升**：after 臂 1/12 < before 臂 2/12，
   提交数 4→2。差异在 n=12 上不显著（单次运行、screen 层、存在任务翻转：
   65765/3497 两题方向相反），不能据此宣称"变好"或"变差"，但可以明确
   **不能宣称变好**。
2. 两臂 9/12 任务都停在 62 turns 上限（45 pass1 + 15 pass2 后仍 saturate），
   说明当前主导失败仍是**预算/提交纪律**，而不是"工具缺失"。
3. 工具引入后 turns 总量与成本略降（$30.57 → $25.78），但探索被工具吸收
   （工具调用更多），转化为提交的比例反而下降——对应 r9 提到的
   "facade 不变 ≠ 语义等价"风险与"工具吸收 turns"效应。
4. arvo:42227 在 after 臂 26 turns 就提交（before 58），说明工具能缩短
   单题时间；但 fix_exit≠0 的 C 桶问题未解决（仍需 Y5/Y6 归因）。
5. verify_gate 在两臂均无违规；after 臂未出现"被 Hook 强制多提交"的证据
   （check.py 计数反而略低）。

## 取样纪律与后续

- 本批为 screen 层（formal50 已用题），结论只能用于淘汰/定向，**不得**作为
  最终提分声明；最终声明仍需 ≥50 题 blind 集。
- 下一步应把提分杠杆从"补工具"转向：X2″ 五态提交机 / X5 六步提交状态机
  （把 turns 转成提交）、Y2 格式感知输入生成（降 time-to-first-input），
  再以同一队列做第二轮配对，评估单变量增量。
- 历史 formal50 的 30% 结果未被覆盖：本批使用的是独立 out/ws/task 目录。
