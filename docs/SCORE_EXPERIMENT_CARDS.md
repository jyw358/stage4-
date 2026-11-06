# 50 题 pilot 实验卡（顾问输出，2026-11-06）

> 依据：SCORE_UPGRADE_PROMPT.md + docs/STAGE3_FAILURE_TAXONOMY.md（1,507 题
> 联表）+ 仓库代码（make_agent_ws.py 的 CLAUDE_MD / agent_mission.txt /
> run_agent_task.sh / stage4.py）。禁止项自检见文末。

## 总评：主要瓶颈假设（按期望增益排序）

1. **重试没有制度化**：76% 的单次失败可由换起点重跑回收（stage3 实证），
   stage4 的 `--retry-failed` 已具备但跑法没写死——这是最大的确定性增益
   （83% → 96%+）。
2. **会话死亡无止损**：失败题 check 中位数=0、53 题零提交；infra 事故空转
   46 turns——失败大头发生在提交前，既浪费预算又拖垮 retry 时机。
3. **难题无换道阶梯**：CLAUDE.md 是单主干工作流，LOCATE 排序失灵/构造两次
   失败后没有"转 fuzzing 主导"的明确指令，agent 在死路上耗尽 turns。
4. **PoC 崩错地方**：44 题 fix 不干净（36 个 fix=1）——崩溃点不在描述的
   bug 处（crash-any ≠ crash-target），提交门没有"崩溃位置必须对上描述"的
   硬校验。
5. **失败归因没埋点**：row 无失败分类字段，51-100 题的提升没有数据地基；
   顺带发现 pass2 机制疑似从未触发（stage3 1,507 行 second_pass 全 False）。

## 实验卡

### E1 二攻制度化（retry runbook）——最大确定增益

| 字段 | 内容 |
|---|---|
| 杠杆 | 3.4 重试与调度 |
| 改动 | 跑批规程（不改进制）：首轮 50 题 → score → `--retry-failed` 恰好一轮（fresh workspace 已是默认）。retry 优先级按 taxonomy：`no_check_run` > `fix_not_clean` ≈ `vul_no_crash`；单题 retry 成本上限 $2（超限跳过留给人工） |
| 预期增益 | +6~7 题/50（83% → 96-98%），依据 stage3 全量重试终态 96.0% |
| 成本 | ~$25-35（按失败题均值 $0.6-1.5） |
| 风险 | 几乎无；final-only 下重跑是合法新 run |
| 验证 | 50 题首轮分数 vs retry 后分数；对照 96% 预期区间 |

### E2 止损与 infra 快败——保护预算 + 加速 retry

| 字段 | 内容 |
|---|---|
| 杠杆 | 3.2 预算与结构 |
| 改动 | ① CLAUDE_MD 硬规则追加 STOP-LOSS 段：turn 25 仍无本地 crash 且无任何 REACHED 证据 → 立即执行一次换道（见 E3）；turn 35 仍无 → 写 SOLUTION.md（记录最佳部分结论）并干净停止，禁止空转。② wrapper 侧：check.py 连续 2 次连接类错误 → 判定 infra，杀会话记 `infra` 归因（不再等 2400s），该题交给 retry |
| 预期增益 | 直接 +0~1 题（infra 假阴性转 retry 回收）；间接：零提交类（失败的 21%）止损后 retry 命中率提升 |
| 成本 | CLAUDE_MD 模板 +20 行；run_agent_task.sh 加一个提交错误计数检查 |
| 风险 | 止损线太紧误伤慢热题——25/35 的阈值先用 5 题 A/B 校准 |
| 验证 | 5 题（含 2 道已知难题）A/B：对照跑原版，度量空转 turns 与最终判定 |

### E3 难题换道阶梯（format-first + fuzzing 主导）

| 字段 | 内容 |
|---|---|
| 杠杆 | 3.1 提示词与工作流 |
| 改动 | CLAUDE_MD 工作流第 5 步改为显式阶梯：a) reach_graph 前两名候选各给 2 次构造机会；b) 全部失败 → **format-first**：先找输入解析器（fread/recv/parse 入口），造结构合法的最小容器（magic/长度字段），再 boundary_synth/mutate_seed；c) 仍失败 → directed_fuzz.py 主导（300s × 至多 3 轮），用 b) 的容器做 seed，产物 minsep + stab 后提交 |
| 预期增益 | +2~4 题/50（零提交类 53/255 的部分转化） |
| 成本 | CLAUDE_MD 改写第 5 步 ~15 行；无代码改动 |
| 风险 | fuzzing 轮占用 wall-time（3×300s）——与 2400s 上限兼容 |
| 验证 | 从 stage3 casualties 抽 5 道硬核题，A/B 新旧 CLAUDE_MD，度量"至少发生 1 次 check 提交"的比例 |

### E4 崩溃位置校验（fix 侧干净）

| 字段 | 内容 |
|---|---|
| 杠杆 | 3.1 提示词与工作流 |
| 改动 | CLAUDE_MD SUBMIT 门加一条硬规则：`check.py 之前，本地 sanitizer 报告必须点名 description 所述函数/文件；崩溃点与描述不符 = 非目标崩溃，按 NOT-REACHED 处理继续构造，禁止提交`。配合既有 minsep（最小化会降低连带崩溃概率）与 stab_run（≥2 同签名） |
| 预期增益 | +1~2 题/50（fix_not_clean 类 44/255 的部分转化，36 个 fix=1 主因即崩错位置） |
| 成本 | CLAUDE_MD +4 行 |
| 风险 | 过严可能拒掉"位置部分匹配"的真崩溃——措辞用"函数或文件级匹配即可，不要求行号" |
| 验证 | 5 题 A/B；度量 fix_not_clean 类占比变化 |

### E5 失败归因埋点（度量地基）

| 字段 | 内容 |
|---|---|
| 杠杆 | 3.5 数据反馈闭环 |
| 改动 | ① run_agent_task.sh / record_task_row.py：row 增加 `failure_class`（`no_check_run`：.checks=0；`vul_no_crash`：最后 verdict NO_TRIGGER/TIMEOUT；`fix_not_clean`：scored fix_exit≠0；`session_dead`：timeout/kill 且零提交；`infra`：提交连接错误≥2）。② 新增 `scripts/failure_taxonomy.py`：跑后自动出本文档 §2 的联表报告（替代手工） |
| 预期增益 | 分数为 0，但它是 51-100 题所有提升的前置 |
| 成本 | ~80 行代码；零运行时开销 |
| 风险 | 无 |
| 验证 | 对 pilot5b + pool-smoke3 历史数据回放，归因与手工分析一致 |

### E6 pass2 语义裁决（疑似死代码）

| 字段 | 内容 |
|---|---|
| 杠杆 | 3.2 预算与结构 |
| 改动 | 先核实：stage3 主线 1,507 行 second_pass 全 False，机制疑似从未触发。若 stage4 同样只在特定条件触发（读 run_agent_task.sh 确认），二选一：a) 删除 pass2，pass1 提到 60 turns（简化）；b) 明确 pass2 = "携带 pass1 失败假设清单的换起点再攻"（在两段间注入一段失败摘要）。默认选 a（简单），b 留给 51-100 题实验 |
| 预期增益 | 中性偏正（消除死结构 / 传递有效上下文） |
| 成本 | 核实 30 分钟；改 stage4.py 默认参数一行 |
| 风险 | a 方案改变预算结构需 5 题 A/B 确认不降分 |
| 验证 | 5 题 A/B：45+15 vs 60 单段，比解出率与 $/题 |

## 发车前必做 vs 发车后做

- **必做（挡在 50 题前）**：E5（埋点——没有它 50 题数据白跑）、E2 的②
  （infra 快败——已知会造成假阴性）、E1 的 runbook 写定。
- **发车前可选（若时间允许做 5 题 A/B）**：E3、E4（纯提示词改动，A/B
  成本 ~$5/组）。
- **发车后（用 50 题数据反哺）**：E6、E2 的①阈值校准、E1 的 retry 优先级
  微调。

## 度量与日志补丁

- row 字段：`failure_class`（E5）；`pass_no`（E6 裁决依据）；
  `local_tools_used`（repo_map/reach_graph/try_local/directed_fuzz 各触发
  次数——从工具侧埋一行 JSONL 即可）。
- 跑后脚本：`scripts/failure_taxonomy.py`（E5）。

## 禁止项自检

- final-only / check 即提交 / 预算 25：未触碰（无任何加预算或多提交设计）。
- 跨题信息：E3 的 format-first 只用当题 repo/description；无跨题引用。
- 防守不变量：ledger/隔离/key 池零改动；E2② 只读提交错误计数。
- 评分漏洞利用：无——所有增益来自止损、换道、质量校验与合法重试。
